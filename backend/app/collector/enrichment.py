"""Feature enrichment and local safety filtering."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .client import GMGNDataClient
from .constants import FEATURE_SCHEMA_VERSION
from .errors import CollectorError, CollectorNetworkError, CollectorRateLimitError
from .event_features import build_gmgn_event_features
from .filters import FilterDecision, SafetyFilter, canonical_launchpad, first, normalize_token, to_float
from .models import ApiKeyRoles, CollectedSample, Kline, TokenCandidate


def _all_dicts(value: Any, depth: int = 0) -> list[Mapping[str, Any]]:
    if depth > 8:
        return []
    if isinstance(value, Mapping):
        result: list[Mapping[str, Any]] = [value]
        for key, nested in value.items():
            # Private collector metadata (for example server-qualification
            # provenance) must remain nested and must not leak generic keys such
            # as `source`, `predicate` or `limit` into the business namespace.
            if str(key).startswith("_"):
                continue
            result.extend(_all_dicts(nested, depth + 1))
        return result
    if isinstance(value, list):
        result = []
        for nested in value:
            result.extend(_all_dicts(nested, depth + 1))
        return result
    return []


def merge_sources(*values: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        for mapping in _all_dicts(value):
            for key, item in mapping.items():
                if isinstance(item, (Mapping, list)):
                    continue
                if result.get(key) in (None, "") and item not in (None, ""):
                    result[key] = item
        if isinstance(value, Mapping):
            for key, item in value.items():
                result.setdefault(key, item)
    return result


def recursive_find(value: Any, keys: Sequence[str], depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(value, Mapping):
        for key in keys:
            if value.get(key) not in (None, ""):
                return value[key]
        for nested in value.values():
            found = recursive_find(nested, keys, depth + 1)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for nested in value:
            found = recursive_find(nested, keys, depth + 1)
            if found not in (None, ""):
                return found
    return None


def extract_items(value: Any, keys: Sequence[str]) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if not isinstance(value, Mapping):
        return []
    for key in keys:
        nested = value.get(key)
        found = extract_items(nested, keys)
        if found:
            return found
    return []


class EnrichmentProvider(Protocol):
    async def token_bundle(self, address: str) -> Mapping[str, Any]: ...

    async def supplement_missing_facts(
        self, address: str, missing_fields: Sequence[str]
    ) -> Mapping[str, Any]: ...

    async def top_holders(self, address: str, limit: int = 20) -> Sequence[Mapping[str, Any]]: ...

    async def created_tokens(self, creator: str) -> Mapping[str, Any]: ...

    async def klines(self, address: str, from_ts: int, to_ts: int) -> Sequence[Kline]: ...


class GMGNEnrichmentProvider:
    """GMGN endpoint adapter preserving the source script's key roles."""

    def __init__(
        self,
        client: GMGNDataClient,
        roles: ApiKeyRoles,
        *,
        primary_attempts: int = 4,
        primary_retry_seconds: float = 5.0,
        fallback_delay_seconds: float = 10.0,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.roles = roles
        self.primary_attempts = max(1, primary_attempts)
        self.primary_retry_seconds = max(0.0, primary_retry_seconds)
        self.fallback_delay_seconds = max(0.0, fallback_delay_seconds)
        self._sleep = sleeper
        self._realtime_index = 0
        self._kline_index = 0

    async def _realtime_request(
        self,
        path: str,
        *,
        params: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        primary = self.roles.realtime[self._realtime_index % len(self.roles.realtime)]
        self._realtime_index += 1
        last_error: BaseException | None = None
        if self.client.slot_rate_limit_remaining(primary) <= 0:
            for attempt in range(self.primary_attempts):
                try:
                    return await self.client.request(primary, path, params=params)
                except CollectorNetworkError:
                    raise
                except CollectorRateLimitError as exc:
                    last_error = exc
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < self.primary_attempts - 1 and self.primary_retry_seconds:
                        await self._sleep(self.primary_retry_seconds)
        fallbacks = [slot for slot in self.roles.realtime_fallback if slot.index != primary.index]
        delay_before_next = False
        for fallback in fallbacks:
            if self.client.slot_rate_limit_remaining(fallback) > 0:
                continue
            if delay_before_next and self.fallback_delay_seconds:
                await self._sleep(self.fallback_delay_seconds)
            try:
                return await self.client.request(fallback, path, params=params)
            except CollectorNetworkError:
                raise
            except CollectorRateLimitError as exc:
                # A rate-limited key is immediately skipped. Sleeping between
                # known 429 slots only stretches a failed cycle and does not
                # improve the server-side reset condition.
                last_error = exc
                delay_before_next = False
                continue
            except Exception as exc:
                last_error = exc
                delay_before_next = True
        raise CollectorError(f"Realtime enrichment failed for path={path}") from last_error

    async def _parallel_bundle_requests(
        self,
        requests: Sequence[tuple[str, str]],
        *,
        params: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        async def fetch(name: str, path: str) -> tuple[str, Any]:
            try:
                return name, await self._realtime_request(path, params=params)
            except CollectorNetworkError as exc:
                return name, exc
            except CollectorError:
                return name, {}

        results = await asyncio.gather(*(fetch(name, path) for name, path in requests))
        bundle: dict[str, Any] = {}
        for name, result in results:
            if isinstance(result, CollectorNetworkError):
                raise result
            bundle[name] = result
        return bundle

    async def token_bundle(self, address: str) -> Mapping[str, Any]:
        params = {"chain": "sol", "address": address}
        return await self._parallel_bundle_requests((
            ("token_info", self.client.endpoints.token_info),
            ("security", self.client.endpoints.token_security),
            ("pool", self.client.endpoints.token_pool_info),
        ), params=params)

    async def supplement_missing_facts(
        self, address: str, missing_fields: Sequence[str]
    ) -> Mapping[str, Any]:
        """Refetch only endpoint families capable of supplying missing facts."""
        fields = {str(field) for field in missing_fields}
        security_fields = {
            "rug_ratio", "insider_ratio", "bundler_rate", "top_10_holder_rate",
            "fresh_wallet_rate", "burn_status", "renounced_mint",
            "renounced_freeze_account", "is_wash_trading",
            "rat_trader_amount_rate", "sell_tax", "buy_tax", "sniper_count",
        }
        info_fields = {
            "address", "launchpad", "symbol", "price", "liquidity",
            "holder_count", "marketcap", "age", "swaps_1h", "volume_1h",
            "volume", "smart_degen_count", "renowned_count",
        }
        pool_fields = {"quote_symbol", "price", "liquidity"}
        wanted: list[tuple[str, str]] = []
        if fields & security_fields:
            wanted.append(("security", self.client.endpoints.token_security))
        if fields & info_fields:
            wanted.append(("token_info", self.client.endpoints.token_info))
        if fields & pool_fields:
            wanted.append(("pool", self.client.endpoints.token_pool_info))
        if not wanted:
            wanted = [
                ("token_info", self.client.endpoints.token_info),
                ("security", self.client.endpoints.token_security),
                ("pool", self.client.endpoints.token_pool_info),
            ]
        params = {"chain": "sol", "address": address}
        return await self._parallel_bundle_requests(wanted, params=params)

    async def top_holders(self, address: str, limit: int = 20) -> Sequence[Mapping[str, Any]]:
        data = await self._realtime_request(
            self.client.endpoints.top_holders,
            params={"chain": "sol", "address": address, "limit": limit},
        )
        return extract_items(data, ("holders", "list", "items", "rows", "data"))

    async def created_tokens(self, creator: str) -> Mapping[str, Any]:
        if not creator:
            return {}
        try:
            return await self._realtime_request(
                self.client.endpoints.created_tokens,
                params={
                    "chain": "sol",
                    "wallet_address": creator,
                    "order_by": "token_ath_mc",
                    "direction": "desc",
                },
            )
        except CollectorError:
            return {}

    async def klines(self, address: str, from_ts: int, to_ts: int) -> Sequence[Kline]:
        primary = self.roles.kline[self._kline_index % len(self.roles.kline)]
        self._kline_index += 1
        params = {
            "chain": "sol",
            "address": address,
            "resolution": "1m",
            "from": int(from_ts) * 1_000,
            "to": int(to_ts) * 1_000,
            "limit": 500,
        }
        last_error: BaseException | None = None
        if self.client.slot_rate_limit_remaining(primary) <= 0:
            for attempt in range(3):
                try:
                    data = await self.client.request(
                        primary,
                        self.client.endpoints.kline,
                        params=params,
                        timeout_seconds=max(self.client.timeout_seconds, 30.0),
                    )
                    return [Kline.from_mapping(item) for item in extract_items(data, ("klines", "list", "items", "rows", "data"))]
                except CollectorNetworkError:
                    raise
                except CollectorRateLimitError as exc:
                    last_error = exc
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < 2 and self.fallback_delay_seconds:
                        await self._sleep(self.fallback_delay_seconds)
        for fallback in self.roles.kline_fallback:
            if fallback.index == primary.index or self.client.slot_rate_limit_remaining(fallback) > 0:
                continue
            try:
                data = await self.client.request(
                    fallback,
                    self.client.endpoints.kline,
                    params=params,
                    timeout_seconds=max(self.client.timeout_seconds, 30.0),
                )
                return [Kline.from_mapping(item) for item in extract_items(data, ("klines", "list", "items", "rows", "data"))]
            except CollectorNetworkError:
                raise
            except CollectorRateLimitError as exc:
                last_error = exc
                continue
            except Exception as exc:
                last_error = exc
        raise CollectorError("Kline request failed after primary and idle fallback roles") from last_error


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    sample: CollectedSample | None
    decision: FilterDecision


def _ln(value: Any) -> float | None:
    number = to_float(value)
    return math.log(number) if number is not None and number > 0 else None


def _ln1p(value: Any) -> float | None:
    number = to_float(value)
    return math.log1p(number) if number is not None and number >= 0 else None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    n, d = to_float(numerator), to_float(denominator)
    return n / d if n is not None and d not in (None, 0) else None


def _bool01(value: Any) -> int:
    if value in (None, "", False, 0, "0"):
        return 0
    if isinstance(value, str) and value.strip().lower() in {"false", "no", "none", "null"}:
        return 0
    return 1


def _price_change(current_price: float, source: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    old_price = to_float(recursive_find(source, keys))
    return current_price / old_price - 1 if old_price else None


def _historical_change_from_klines(
    current_price: float,
    klines: Sequence[Kline],
    target_ts: int,
) -> float | None:
    eligible = [line for line in klines if line.timestamp <= target_ts and line.close not in (None, 0)]
    if not eligible:
        return None
    previous = max(eligible, key=lambda line: line.timestamp)
    return current_price / float(previous.close) - 1.0


class EnrichmentService:
    def __init__(
        self,
        provider: EnrichmentProvider,
        safety_filter: SafetyFilter | None = None,
        onchain_admission: Any | None = None,
        public_social_signals: Any | None = None,
        *,
        readiness_attempts: int = 3,
        readiness_retry_seconds: float = 2.0,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.provider = provider
        self.safety_filter = safety_filter or SafetyFilter()
        self.onchain_admission = onchain_admission
        self.public_social_signals = public_social_signals
        self.readiness_attempts = max(1, int(readiness_attempts))
        self.readiness_retry_seconds = max(0.0, float(readiness_retry_seconds))
        self._sleep = sleeper

    def prefilter(self, candidate: TokenCandidate) -> FilterDecision:
        normalized = normalize_token(candidate.raw, candidate.token_type)
        if not normalized.get("address"):
            normalized["address"] = candidate.address
        return self.safety_filter.evaluate_discovery_prefilter(normalized)

    async def enrich(
        self,
        candidate: TokenCandidate,
        *,
        trending: Mapping[str, Any] | None = None,
        now_ts: int | None = None,
    ) -> EnrichmentResult:
        bundle: Mapping[str, Any] = await self.provider.token_bundle(candidate.address)
        source: Mapping[str, Any] = merge_sources(candidate.raw, bundle, trending or {})
        normalized: dict[str, Any] = normalize_token(source, candidate.token_type)
        if not normalized.get("address"):
            normalized["address"] = candidate.address

        # Prefer a real PIT field value. Server qualification is only a final
        # fallback for a fact that the upstream rank query proved but did not echo.
        strict_readiness = self.safety_filter.evaluate_required_facts(
            normalized, allow_server_qualified=False
        )
        if not strict_readiness.accepted:
            missing = tuple(
                str(reason).split(":", 1)[1]
                for reason in strict_readiness.reasons
                if str(reason).startswith("missing_or_invalid:") and ":" in str(reason)
            )
            supplement = getattr(self.provider, "supplement_missing_facts", None)
            if callable(supplement):
                if self.readiness_retry_seconds:
                    await self._sleep(self.readiness_retry_seconds)
                extra = await supplement(candidate.address, missing)
                bundle = merge_sources(bundle, extra)
                source = merge_sources(candidate.raw, bundle, trending or {})
                normalized = normalize_token(source, candidate.token_type)
                if not normalized.get("address"):
                    normalized["address"] = candidate.address
                strict_readiness = self.safety_filter.evaluate_required_facts(
                    normalized, allow_server_qualified=False
                )
            else:
                # Compatibility path for alternate/test providers that cannot
                # target a missing endpoint family. Production GMGN never uses
                # this whole-bundle retry loop.
                for _attempt in range(max(0, self.readiness_attempts - 1)):
                    if strict_readiness.accepted:
                        break
                    if self.readiness_retry_seconds:
                        await self._sleep(self.readiness_retry_seconds)
                    extra = await self.provider.token_bundle(candidate.address)
                    bundle = merge_sources(bundle, extra)
                    source = merge_sources(candidate.raw, bundle, trending or {})
                    normalized = normalize_token(source, candidate.token_type)
                    if not normalized.get("address"):
                        normalized["address"] = candidate.address
                    strict_readiness = self.safety_filter.evaluate_required_facts(
                        normalized, allow_server_qualified=False
                    )

        readiness = strict_readiness
        if not readiness.accepted:
            readiness = self.safety_filter.evaluate_required_facts(
                normalized, allow_server_qualified=True
            )
        if not readiness.accepted:
            return EnrichmentResult(None, readiness)

        decision = self.safety_filter.evaluate(normalized)
        if not decision.accepted:
            return EnrichmentResult(None, decision)

        holders: Sequence[Mapping[str, Any]] = ()
        holders_decision = FilterDecision(False, ("missing_or_invalid:top1_addr_type0_rate",))
        for attempt in range(self.readiness_attempts):
            holders = await self.provider.top_holders(candidate.address, 20)
            rate = self.safety_filter.top1_addr_type0_rate(holders)
            if rate is not None:
                holders_decision = self.safety_filter.evaluate_top_holders(holders)
                break
            if attempt < self.readiness_attempts - 1 and self.readiness_retry_seconds:
                await self._sleep(self.readiness_retry_seconds)
        if not holders_decision.accepted:
            return EnrichmentResult(None, holders_decision)

        creator = str(recursive_find(source, ("creator_address", "creator", "owner")) or "")
        created_tokens: Mapping[str, Any] = {}
        entry_time = int(now_ts or time.time())
        onchain_decision = None
        if self.onchain_admission is not None:
            age_minutes = to_float(normalized.get("age"))
            pool_address = str(recursive_find(
                source,
                ("biggest_pool_address", "pool_address", "pair_address", "amm_address", "pool_id"),
            ) or "")
            if age_minutes is None:
                return EnrichmentResult(None, FilterDecision(False, ("missing_or_invalid:age",)))

            async def load_created_tokens() -> Mapping[str, Any]:
                nonlocal created_tokens
                created_tokens = await self.provider.created_tokens(creator)
                return created_tokens

            onchain_decision = await self.onchain_admission.evaluate(
                token_mint=candidate.address,
                pool_address=pool_address,
                creator=creator,
                entry_time=entry_time,
                age_minutes=age_minutes,
                gmgn_buys_1h=normalized.get("buys_1h"),
                gmgn_swaps_1h=normalized.get("swaps_1h"),
                created_tokens_loader=load_created_tokens,
            )
            if not onchain_decision.accepted:
                return EnrichmentResult(None, FilterDecision(False, onchain_decision.reasons))
            source = dict(source)
            source["_onchain_admission"] = {
                "buy_swap_source": onchain_decision.buy_swap_source,
                "creator_launch_source": onchain_decision.creator_launch_source,
                "rpc_used": bool(onchain_decision.rpc_used),
            }
        else:
            created_tokens = await self.provider.created_tokens(creator)

        social_features: Mapping[str, Any] = {}
        if self.public_social_signals is not None:
            try:
                social_snapshot = await self.public_social_signals.snapshot(
                    candidate.address, entry_time=entry_time
                )
                social_features = dict(social_snapshot.features)
                source = dict(source)
                source["_public_social_signals"] = {
                    "provider": "985monitor_public",
                    "fetched_at": int(social_snapshot.fetched_at),
                    "successful_sources": list(social_snapshot.successful_sources),
                    "incomplete_sources": list(social_snapshot.incomplete_sources),
                    "failed_sources": list(social_snapshot.failed_sources),
                    "matched_events_15m": int(social_snapshot.matched_events),
                }
            except Exception:
                social_features = {}

        price = to_float(normalized.get("price"))
        liquidity = to_float(normalized.get("liquidity"))
        if not price or liquidity is None:
            return EnrichmentResult(None, FilterDecision(False, ("entry_price_or_liquidity_missing",)))

        stat = recursive_find(bundle, ("stat", "stats"))
        stat = stat if isinstance(stat, Mapping) else {}
        link = recursive_find(bundle, ("link",))
        link = link if isinstance(link, Mapping) else {}
        twitter = first(link, ("twitter_username", "twitter", "twitter_url", "x"), recursive_find(source, ("twitter_username", "twitter", "twitter_url", "x")))
        website = first(link, ("website", "web", "homepage"), recursive_find(source, ("website", "web", "homepage")))
        ath_price = recursive_find(source, ("ath_price", "athPrice", "all_time_high_price", "history_highest_price", "highest_price"))
        twitter_history = recursive_find(source, ("twitter_name_change_history", "twitterNameChangeHistory"))
        twitter_rename = recursive_find(source, ("twitter_rename_count", "twitterRenameCount"))
        if twitter_rename in (None, "") and isinstance(twitter_history, list):
            twitter_rename = len(twitter_history)

        price_change_1h = _price_change(price, source, ("price_1h", "price1h", "price_h1"))
        price_change_5m = _price_change(price, source, ("price_5m", "price5m", "price_m5"))
        # One bounded pre-entry OHLCV request supplies the optional 2m event
        # feature and also remains the existing fallback for 5m/1h momentum.
        # No kline with a timestamp after entry_time is consumed.
        try:
            history_klines = tuple(
                line for line in await self.provider.klines(
                    candidate.address, entry_time - 60 * 60, entry_time
                ) if line.timestamp <= entry_time
            )
        except Exception:
            history_klines = ()
        if price_change_1h is None:
            price_change_1h = _historical_change_from_klines(
                price, history_klines, entry_time - 60 * 60
            )
        if price_change_5m is None:
            price_change_5m = _historical_change_from_klines(
                price, history_klines, entry_time - 5 * 60
            )
        event_features = build_gmgn_event_features(
            source,
            current_price=price,
            entry_time=entry_time,
            history_klines=history_klines,
            age_minutes=normalized.get("age"),
            holder_count=normalized.get("holder_count"),
            marketcap=normalized.get("marketcap"),
            liquidity=normalized.get("liquidity"),
        )
        features = {
            "ln(age+1)": _ln1p(normalized.get("age")),
            "ln(liquidity_usd)": _ln(liquidity),
            "liquidity/holder_count": _ln(_ratio(normalized.get("liquidity"), normalized.get("holder_count"))),
            "volume_1h/swaps_1h": _ln(_ratio(normalized.get("volume_1h"), normalized.get("swaps_1h"))),
            "buy_swap_ratio_1h": onchain_decision.buy_swap_ratio_1h if onchain_decision is not None else None,
            "ln(creator_launches_24h+1)": _ln1p(onchain_decision.creator_launches_24h) if onchain_decision is not None else None,
            "has_twitter": _bool01(twitter),
            "has_website": _bool01(website),
            "ln(image_dup+1)": _ln1p(recursive_find(source, ("image_dup", "image_duplicate", "imageDup"))),
            "dexscr_update_link": _bool01(recursive_find(source, ("dexscr_update_link", "dexscreener_update_link", "dexscrUpdateLink"))),
            "cto_flag": _bool01(recursive_find(source, ("cto_flag", "ctoFlag"))),
            "ln(twitter_rename_count+1)": _ln1p(twitter_rename),
            "ln(twitter_del_post_token_count+1)": _ln1p(recursive_find(source, ("twitter_del_post_token_count", "twitterDelPostTokenCount"))),
            "ln(twitter_create_token_count+1)": _ln1p(recursive_find(source, ("twitter_create_token_count", "twitterCreateTokenCount"))),
            "top_10_holder_rate": normalized.get("top_10_holder_rate"),
            "top_bot_degen_percentage": first(stat, ("top_bot_degen_percentage", "topBotDegenPercentage"), recursive_find(source, ("top_bot_degen_percentage",))),
            "fresh_wallet_rate": first(stat, ("fresh_wallet_rate", "freshWalletRate"), normalized.get("fresh_wallet_rate")),
            "bot_degen_rate": first(stat, ("bot_degen_rate", "botDegenRate"), recursive_find(source, ("bot_degen_rate", "botDegenRate"))),
            "price/ath_price": _ratio(price, ath_price),
            "stat.holder_count/market_cap": _ratio(normalized.get("holder_count"), normalized.get("marketcap")),
            "ln(smart_degen_count+1)": _ln1p(normalized.get("smart_degen_count")),
            "ln(renowned_count+1)": _ln1p(normalized.get("renowned_count")),
            "entrapment_ratio": recursive_find(source, ("entrapment_ratio", "max_entrapment_ratio")),
            "dev_team_hold_rate": recursive_find(source, ("dev_team_hold_rate", "dev_hold_rate", "creator_hold_rate")),
            "top70_sniper_hold_rate": recursive_find(source, ("top70_sniper_hold_rate", "top_70_sniper_hold_rate")),
            "ln(twitter_dup+1)": _ln1p(recursive_find(source, ("twitter_dup", "twitter_duplicate"))),
            "ln(website_dup+1)": _ln1p(recursive_find(source, ("website_dup", "website_duplicate"))),
            "ln(visiting_count+1)": _ln1p(recursive_find(source, ("visiting_count", "visitingCount"))),
            "price_change_1h": price_change_1h,
            "price_change_5m": price_change_5m,
            **event_features,
            "ln(creator_open_count+1)": _ln1p(recursive_find([source, created_tokens], ("creator_open_count", "open_count"))),
            "creator_open_ratio": recursive_find([source, created_tokens], ("creator_open_ratio", "open_ratio")),
            "ln(top_wallets+1)": _ln1p(recursive_find(source, ("top_wallets", "topWallets"))),
            **social_features,
        }
        sample = CollectedSample(
            address=candidate.address,
            token_type=candidate.token_type,
            entry_time=entry_time,
            entry_price=price,
            launchpad=canonical_launchpad(normalized.get("launchpad")),
            liquidity=liquidity,
            features=features,
            age_minutes=to_float(normalized.get("age")),
            holder_count=to_float(normalized.get("holder_count")),
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            feature_snapshot_at=entry_time,
            source=source,
        )
        return EnrichmentResult(sample, FilterDecision(True, ()))
