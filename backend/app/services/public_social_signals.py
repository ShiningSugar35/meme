from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class PublicSignalEndpoint:
    key: str
    path: str
    category: str
    row_limit: int | None = None


PUBLIC_SIGNAL_ENDPOINTS: tuple[PublicSignalEndpoint, ...] = (
    PublicSignalEndpoint("binance_square", "/api/binance-square-events?limit=200", "exchange", 200),
    PublicSignalEndpoint("binance_alpha", "/api/binance-alpha-events?limit=200", "exchange", 200),
    PublicSignalEndpoint("binance_web3", "/api/binance-web3-events?limit=200", "exchange", 200),
    PublicSignalEndpoint("okx_board", "/api/okx-board-events?limit=200", "exchange", 200),
    PublicSignalEndpoint("news", "/api/news-events?limit=200", "news", 200),
    PublicSignalEndpoint("truth_social", "/api/truth-social-events?limit=200", "social", 200),
    PublicSignalEndpoint("dex", "/api/dex-events?limit=200", "market", 200),
    PublicSignalEndpoint("fomo", "/fomo-events.json", "trade", 800),
    PublicSignalEndpoint("telegram", "/tg-events.json", "social", 600),
)


@dataclass(frozen=True, slots=True)
class PublicSocialSignalSnapshot:
    observed_at: int
    fetched_at: int
    features: Mapping[str, float | None]
    successful_sources: tuple[str, ...]
    incomplete_sources: tuple[str, ...]
    failed_sources: tuple[str, ...]
    matched_events: int


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _event_items(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    events = payload.get("events")
    if isinstance(events, list):
        return [item for item in events if isinstance(item, Mapping)]
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, Mapping)]
    if isinstance(data, Mapping):
        return _event_items(data)
    return []


def _timestamp(value: Any) -> int | None:
    if value in (None, ""):
        return None
    number = _finite(value)
    if number is not None:
        parsed = int(number)
        return parsed // 1_000 if abs(parsed) >= 100_000_000_000 else parsed
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed_dt = datetime.fromisoformat(text)
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        return int(parsed_dt.timestamp())
    except (TypeError, ValueError):
        return None


def _event_time(event: Mapping[str, Any]) -> int | None:
    for key in ("createdAt", "ts", "receivedAt", "savedAt", "updatedAt", "certifiedAt"):
        parsed = _timestamp(event.get(key))
        if parsed is not None:
            return parsed
    return None


def _text_blob(event: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in (
        "content", "comment", "text", "title", "body", "symbol", "tokenAddress",
        "profileUrl", "handle", "twAccount", "userName",
    ):
        value = event.get(key)
        if value not in (None, ""):
            values.append(str(value))
    return "\n".join(values)


def _matches_address(event: Mapping[str, Any], address: str) -> bool:
    for key in ("tokenAddress", "address", "mint", "token_mint", "contractAddress", "ca"):
        value = event.get(key)
        if value not in (None, "") and str(value) == address:
            return True
    return address in _text_blob(event)


def _author(event: Mapping[str, Any]) -> str:
    for key in ("handle", "twAccount", "userName", "username", "author", "channel"):
        value = event.get(key)
        if value not in (None, ""):
            return str(value).strip().lower()
    return ""


def _followers(event: Mapping[str, Any]) -> float | None:
    for key in ("followers", "followerCount", "followersCount", "x_user_follower"):
        value = _finite(event.get(key))
        if value is not None and value >= 0:
            return value
    return None


def _side(event: Mapping[str, Any]) -> str:
    value = str(event.get("side") or event.get("action") or event.get("direction") or "").strip().lower()
    if value in {"buy", "b", "long"}:
        return "buy"
    if value in {"sell", "s", "short"}:
        return "sell"
    return ""


def _usd(event: Mapping[str, Any]) -> float | None:
    for key in ("usd", "holdingUsd", "amountUsd", "valueUsd", "tradeUsd"):
        value = _finite(event.get(key))
        if value is not None and value >= 0:
            return value
    return None


def _log1p(value: float | int) -> float:
    return math.log1p(max(0.0, float(value)))


def _signed_imbalance(buy: float, sell: float) -> float | None:
    total = float(buy) + float(sell)
    return (float(buy) - float(sell)) / total if total > 0 else None


def _window_complete(
    endpoint: PublicSignalEndpoint,
    events: Sequence[Mapping[str, Any]],
    *,
    entry_time: int,
    window_seconds: int,
) -> bool:
    if endpoint.row_limit is None or len(events) < int(endpoint.row_limit):
        return True
    timestamps = [
        timestamp
        for event in events
        if (timestamp := _event_time(event)) is not None and timestamp <= int(entry_time)
    ]
    return bool(timestamps) and min(timestamps) <= int(entry_time) - int(window_seconds)


class PublicSocialSignalProvider:
    """Low-cost PIT social/event adapter over 985monitor's public read-only feeds.

    It never logs in, never reads browser cookies, never invokes an LLM, and never
    turns a failed source into a zero signal. A shared short-TTL snapshot prevents
    per-token request amplification within a collector cycle.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://985monitor.xyz",
        endpoints: Sequence[PublicSignalEndpoint] = PUBLIC_SIGNAL_ENDPOINTS,
        cache_seconds: float = 20.0,
        timeout_seconds: float = 10.0,
        retry_delay_seconds: float = 0.20,
        client: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if self.base_url.lower() == "https://985monitor.xyz":
            self.retry_base_url: str | None = "https://www.985monitor.xyz"
        elif self.base_url.lower() == "https://www.985monitor.xyz":
            self.retry_base_url = "https://985monitor.xyz"
        else:
            self.retry_base_url = None
        self.endpoints = tuple(endpoints)
        self.cache_seconds = max(1.0, float(cache_seconds))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._cache_mono = 0.0
        self._cache: dict[str, list[Mapping[str, Any]]] = {}
        self._failures: tuple[str, ...] = ()
        self._fetched_at = 0

    async def _http_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers={"User-Agent": "meme-quant-public-signal/1.0"},
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _fetch_one(self, endpoint: PublicSignalEndpoint) -> tuple[str, list[Mapping[str, Any]] | None]:
        client = await self._http_client()
        bases = (self.base_url, self.retry_base_url or self.base_url)
        for attempt, base in enumerate(bases):
            try:
                response = await client.get(f"{base}{endpoint.path}")
                status = int(response.status_code)
                if status == 200:
                    return endpoint.key, _event_items(response.json())
                retryable = status in {408, 425, 429} or status >= 500
                if attempt == 0 and retryable:
                    if self.retry_delay_seconds > 0:
                        await asyncio.sleep(self.retry_delay_seconds)
                    continue
                return endpoint.key, None
            except Exception:
                if attempt == 0:
                    if self.retry_delay_seconds > 0:
                        await asyncio.sleep(self.retry_delay_seconds)
                    continue
                return endpoint.key, None
        return endpoint.key, None

    async def _refresh(self) -> tuple[dict[str, list[Mapping[str, Any]]], tuple[str, ...], int]:
        now_mono = time.monotonic()
        if self._fetched_at and now_mono - self._cache_mono <= self.cache_seconds:
            return self._cache, self._failures, self._fetched_at
        async with self._lock:
            now_mono = time.monotonic()
            if self._fetched_at and now_mono - self._cache_mono <= self.cache_seconds:
                return self._cache, self._failures, self._fetched_at
            results = await asyncio.gather(*(self._fetch_one(endpoint) for endpoint in self.endpoints))
            cache: dict[str, list[Mapping[str, Any]]] = {}
            failures: list[str] = []
            for key, rows in results:
                if rows is None:
                    failures.append(key)
                else:
                    cache[key] = rows
            self._cache = cache
            self._failures = tuple(failures)
            self._cache_mono = time.monotonic()
            self._fetched_at = int(time.time())
            return self._cache, self._failures, self._fetched_at

    async def snapshot(self, address: str, *, entry_time: int) -> PublicSocialSignalSnapshot:
        feeds, failures, fetched_at = await self._refresh()
        endpoint_by_key = {endpoint.key: endpoint for endpoint in self.endpoints}
        successful = tuple(sorted(feeds))
        if not successful:
            missing = {
                name: None
                for name in (
                    "ln(monitor_mentions_5m+1)",
                    "ln(monitor_mentions_15m+1)",
                    "ln(monitor_unique_authors_15m+1)",
                    "monitor_unique_sources_15m",
                    "ln(monitor_follower_reach_15m+1)",
                    "monitor_mention_accel_5m_vs_15m",
                    "ln(monitor_latest_mention_age_s+1)",
                    "monitor_fomo_buy_ratio_15m",
                    "monitor_fomo_usd_imbalance_15m",
                    "ln(monitor_fomo_usd_15m+1)",
                    "monitor_exchange_hits_15m",
                    "monitor_news_hits_15m",
                    "monitor_social_hits_15m",
                    "ln(monitor_global_events_5m+1)",
                    "monitor_global_source_diversity_5m",
                    "monitor_source_coverage",
                )
            }
            return PublicSocialSignalSnapshot(
                observed_at=int(entry_time),
                fetched_at=fetched_at,
                features=missing,
                successful_sources=(),
                incomplete_sources=(),
                failed_sources=tuple(sorted(failures)),
                matched_events=0,
            )

        complete_15m = {
            key
            for key, events in feeds.items()
            if _window_complete(endpoint_by_key[key], events, entry_time=int(entry_time), window_seconds=900)
        }
        complete_5m = {
            key
            for key, events in feeds.items()
            if _window_complete(endpoint_by_key[key], events, entry_time=int(entry_time), window_seconds=300)
        }
        incomplete = tuple(sorted(set(successful) - complete_15m))
        coverage = len(complete_15m) / len(self.endpoints) if self.endpoints else 0.0

        matches: list[tuple[str, str, int, Mapping[str, Any]]] = []
        global_5m_sources: set[str] = set()
        global_5m_events = 0
        for key, events in feeds.items():
            endpoint = endpoint_by_key[key]
            for event in events:
                timestamp = _event_time(event)
                if timestamp is None or timestamp > int(entry_time):
                    continue
                age = int(entry_time) - timestamp
                if key in complete_5m and age <= 300:
                    global_5m_events += 1
                    global_5m_sources.add(key)
                if key in complete_15m and age <= 900 and _matches_address(event, address):
                    matches.append((key, endpoint.category, timestamp, event))

        token_window_available = bool(complete_15m)
        global_window_available = bool(complete_5m)
        recent_5m = [item for item in matches if int(entry_time) - item[2] <= 300]
        recent_15m = matches
        source_keys = {item[0] for item in recent_15m}
        authors = {_author(item[3]) for item in recent_15m if _author(item[3])}
        author_followers: dict[str, float] = {}
        follower_unattributed = 0.0
        for _key, _category, _timestamp_value, event in recent_15m:
            followers = _followers(event)
            if followers is None:
                continue
            author = _author(event)
            if author:
                author_followers[author] = max(followers, author_followers.get(author, 0.0))
            else:
                follower_unattributed += followers
        follower_reach = sum(author_followers.values()) + follower_unattributed

        buy_count = sell_count = 0
        buy_usd = sell_usd = 0.0
        has_usd = False
        exchange_hits = news_hits = social_hits = 0
        for source_key, category, _timestamp_value, event in recent_15m:
            if source_key == "fomo":
                side = _side(event)
                if side == "buy":
                    buy_count += 1
                elif side == "sell":
                    sell_count += 1
                usd = _usd(event)
                if usd is not None and side:
                    has_usd = True
                    if side == "buy":
                        buy_usd += usd
                    elif side == "sell":
                        sell_usd += usd
            exchange_hits += int(category == "exchange")
            news_hits += int(category == "news")
            social_hits += int(category == "social")

        action_count = buy_count + sell_count
        latest_ts = max((item[2] for item in recent_15m), default=None)
        prior_10m_count = max(0, len(recent_15m) - len(recent_5m))
        acceleration = len(recent_5m) / 5.0 - prior_10m_count / 10.0
        fomo_complete = "fomo" in complete_15m
        features: dict[str, float | None] = {
            "ln(monitor_mentions_5m+1)": _log1p(len(recent_5m)) if token_window_available else None,
            "ln(monitor_mentions_15m+1)": _log1p(len(recent_15m)) if token_window_available else None,
            "ln(monitor_unique_authors_15m+1)": _log1p(len(authors)) if token_window_available else None,
            "monitor_unique_sources_15m": float(len(source_keys)) if token_window_available else None,
            "ln(monitor_follower_reach_15m+1)": _log1p(follower_reach) if token_window_available else None,
            "monitor_mention_accel_5m_vs_15m": float(acceleration) if token_window_available else None,
            "ln(monitor_latest_mention_age_s+1)": _log1p(int(entry_time) - latest_ts) if latest_ts is not None else (_log1p(900) if token_window_available else None),
            "monitor_fomo_buy_ratio_15m": buy_count / action_count if action_count > 0 else (0.5 if fomo_complete else None),
            "monitor_fomo_usd_imbalance_15m": _signed_imbalance(buy_usd, sell_usd) if has_usd else (0.0 if fomo_complete and action_count == 0 else None),
            "ln(monitor_fomo_usd_15m+1)": _log1p(buy_usd + sell_usd) if has_usd else (0.0 if fomo_complete and action_count == 0 else None),
            "monitor_exchange_hits_15m": float(exchange_hits) if token_window_available else None,
            "monitor_news_hits_15m": float(news_hits) if token_window_available else None,
            "monitor_social_hits_15m": float(social_hits) if token_window_available else None,
            "ln(monitor_global_events_5m+1)": _log1p(global_5m_events) if global_window_available else None,
            "monitor_global_source_diversity_5m": float(len(global_5m_sources)) if global_window_available else None,
            "monitor_source_coverage": float(coverage),
        }
        return PublicSocialSignalSnapshot(
            observed_at=int(entry_time),
            fetched_at=fetched_at,
            features=features,
            successful_sources=successful,
            incomplete_sources=incomplete,
            failed_sources=tuple(sorted(failures)),
            matched_events=len(recent_15m),
        )
