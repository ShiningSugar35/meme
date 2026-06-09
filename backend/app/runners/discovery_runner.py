import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config import ProviderMode, settings
from ..db.repositories import Repositories
from ..logging_config import logger
from ..providers.base import MarketDataProvider, SwapProvider, ExecutionProvider, RpcProvider
from ..providers.credential_router import get_credential_router
from ..providers.rate_limiter import get_rate_limiter
from ..services.event_bus import event_bus
from ..strategy.filters import (
    run_entry_local_risk_filter, evaluate_price_activity_rules, evaluate_smart_degen,
    _parse_creation_ts, _compute_age_minutes, sort_klines,
)
from ..strategy.thresholds import compute_thresholds, StrategyThresholds, build_trench_filters_for_x, strip_internal_debug_fields, normalize_rate_fraction
from ..trading.executor import TradingPipeline

MOCK_MINTS = {'PASS1', 'PASS1_150', 'PASS1_510', 'FAIL_INIT', 'FAIL_SECOND'}

# Fields that run_entry_local_risk_filter (Stage 0) requires.
# If any are missing in the raw trenches token, we attempt a lightweight enrich
# from fetch_token_snapshot before running the filter.
STAGE0_REQUIRED_FIELDS = [
    "renounced_mint",
    "renounced_freeze_account",
    "is_wash_trading",
    "rat_trader_amount_rate",
    "suspected_insider_hold_rate",
    "sell_tax",
    "burn_status",
    "sniper_count",
]

STAGE0_REQUIRED_ALIASES = {
    "renounced_mint": ["renounced_mint", "mint_renounced", "is_mint_renounced"],
    "renounced_freeze_account": ["renounced_freeze_account", "freeze_renounced", "is_freeze_renounced", "freeze_authority_renounced"],
    "is_wash_trading": ["is_wash_trading", "wash_trading", "wash_trading_detected"],
    "rat_trader_amount_rate": ["rat_trader_amount_rate", "rat_trader_rate"],
    "suspected_insider_hold_rate": ["suspected_insider_hold_rate", "insider_hold_rate", "max_insider_ratio"],
    "sell_tax": ["sell_tax", "sell_tax_rate"],
    "burn_status": ["burn_status", "lp_burn_status", "burnt_status"],
    "sniper_count": ["sniper_count", "snipers", "sniper_trader_count"],
}

SNAPSHOT_COLUMNS = [
    'pool_address', 'platform', 'launchpad',
    'type', 'liquidity_usd', 'sol_side_liquidity', 'volume_usd', 'market_cap',
    'price_usd', 'price_sol', 'top_10_holder_rate', 'top1_holder_rate',
    'renounced_mint', 'renounced_freeze_account', 'max_rug_ratio',
    'max_insider_ratio', 'max_entrapment_ratio', 'is_wash_trading',
    'rat_trader_amount_rate', 'suspected_insider_hold_rate', 'max_bundler_rate',
    'fresh_wallet_rate', 'sell_tax', 'has_social', 'creator_token_status',
    'dev_team_hold_rate', 'dev_token_burn_ratio', 'sniper_count', 'burn_status',
    'holder_count',
    'source_mode',
]

DISCOVERY_GROUPS = [
    {"group_name": "pump_fun", "platforms": ["Pump.fun"]},
    {"group_name": "other_platforms", "platforms": [
        "Moonshot", "moonshot_app", "letsbonk", "memoo",
        "token_mill", "jup_studio", "bags", "believe", "heaven"
    ]},
]
DISCOVERY_TRENCH_TYPES = ["new_creation", "near_completion"]

_feature_slot_cursor_global = 0
_holding_slot_cursor_global = 0


def acquire_feature_slot(stage: str = "") -> Optional[int]:
    stage_lower = (stage or "").lower()
    if "kline" in stage_lower:
        endpoint = getattr(settings, "GMGN_KLINE_PATH", "/v1/market/token_kline")
        task_type = "kline"
    elif "holder" in stage_lower or "degen" in stage_lower or "smart_money" in stage_lower:
        endpoint = getattr(settings, "GMGN_TOKEN_HOLDERS_PATH", "/v1/market/token_top_holders")
        task_type = "holders"
    else:
        endpoint = getattr(settings, "GMGN_TOKEN_INFO_PATH", "/v1/token/info")
        task_type = "token_info"
    try:
        slot = get_credential_router().choose_slot(endpoint=endpoint, task_type=task_type)
        if slot is not None:
            return slot
    except Exception as e:
        logger.warning(f"credential router feature slot selection failed stage={stage}: {e}")

    global _feature_slot_cursor_global
    rl = get_rate_limiter()
    feature_slots = settings.get_feature_slots()
    if not feature_slots:
        return None
    cursor = _feature_slot_cursor_global
    for offset in range(len(feature_slots)):
        idx = (cursor + offset) % len(feature_slots)
        slot = feature_slots[idx]
        if not rl.is_slot_cooldown(slot):
            _feature_slot_cursor_global = (cursor + offset + 1) % len(feature_slots)
            return slot
    return None


def acquire_holding_slot(stage: str = "") -> Optional[int]:
    global _holding_slot_cursor_global
    slots = settings.get_holding_slots()
    if not slots:
        return None
    rl = get_rate_limiter()
    cursor = _holding_slot_cursor_global
    for offset in range(len(slots)):
        idx = (cursor + offset) % len(slots)
        slot = slots[idx]
        if not rl.is_slot_cooldown(slot):
            _holding_slot_cursor_global = (cursor + offset + 1) % len(slots)
            return slot
    return None


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(',', ':'))


def _snapshot_kwargs(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {k: snapshot.get(k) for k in SNAPSHOT_COLUMNS if snapshot.get(k) is not None}


def _first_present(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and value != '':
            return value
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _to_float(value: Any):
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _csv_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value).split(',') if x.strip()]


class DiscoveryRunner:
    def __init__(
        self,
        repo: Repositories,
        gmgn: MarketDataProvider,
        strategy_groups: List[dict],
        jupiter: SwapProvider = None,
        jito: ExecutionProvider = None,
        rpc: RpcProvider = None,
    ):
        self.repo = repo
        self.gmgn = gmgn
        self.strategy_groups = strategy_groups or []
        self.processed_count = 0
        self.last_elapsed_ms = 0
        self.pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)
        self._run_lock = asyncio.Lock()
        self._feature_slot_cursor = 0

    @staticmethod
    def _unique_x_values(strategy_groups: List[dict]) -> List[float]:
        xs: Set[float] = set()
        for sg in strategy_groups:
            x = float(sg.get("x") if sg.get("x") is not None else settings.STRATEGY_DEFAULT_X)
            xs.add(round(x, 6))
        return sorted(xs)

    def _next_feature_slot(self, stage: str = "") -> Optional[int]:
        slot = acquire_feature_slot(stage)
        if slot is not None:
            return slot

        rl = get_rate_limiter()
        feature_slots = settings.get_feature_slots()
        if not feature_slots:
            return None
        cursor = self._feature_slot_cursor
        for offset in range(len(feature_slots)):
            idx = (cursor + offset) % len(feature_slots)
            slot = feature_slots[idx]
            if not rl.is_slot_cooldown(slot):
                self._feature_slot_cursor = (cursor + offset + 1) % len(feature_slots)
                return slot
        return None

    @staticmethod
    def _all_discovery_platforms() -> List[str]:
        platforms: List[str] = []
        seen: Set[str] = set()
        for group in DISCOVERY_GROUPS:
            for platform in group.get("platforms", []):
                if platform not in seen:
                    seen.add(platform)
                    platforms.append(platform)
        return platforms

    @staticmethod
    def _slot_age_window(slot: int) -> Tuple[str, str]:
        i = int(slot) + 1
        return f"{60 * i}m", f"{60 * i + 60}m"

    def _feature_slot_for_token(self, token: Dict[str, Any], stage: str, exclude: Optional[Set[int]] = None) -> Optional[int]:
        exclude = set(exclude or set())
        rl = get_rate_limiter()
        preferred_raw = token.get("_credential_slot") if isinstance(token, dict) else None
        try:
            preferred = int(preferred_raw) if preferred_raw is not None else None
        except Exception:
            preferred = None
        if preferred is not None and preferred not in exclude and not rl.is_slot_cooldown(preferred):
            return preferred

        candidates: List[int] = []
        for slot in settings.get_feature_slots() + settings.get_discovery_slots():
            if slot not in candidates:
                candidates.append(slot)
        for slot in candidates:
            if slot in exclude:
                continue
            if not rl.is_slot_cooldown(slot):
                return slot
        return None

    @staticmethod
    def _token_preferred_slot(token: Dict[str, Any]) -> Optional[int]:
        if not isinstance(token, dict) or token.get("_credential_slot") is None:
            return None
        try:
            return int(token.get("_credential_slot"))
        except Exception:
            return None

    @staticmethod
    def _is_slot_retryable_error(exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            return True
        msg = str(exc).lower()
        return "local rate limited" in msg or "bucket_empty" in msg or "slot_cooldown" in msg

    @staticmethod
    def _is_credential_kw_unsupported(exc: TypeError) -> bool:
        msg = str(exc).lower()
        return "credential_slot" in msg and ("unexpected keyword" in msg or "got an unexpected" in msg)

    async def _call_gmgn_with_token_slot(
        self,
        token: Dict[str, Any],
        stage: str,
        method_name: str,
        *args,
        **kwargs,
    ) -> Tuple[Any, Optional[int]]:
        method = getattr(self.gmgn, method_name)
        preferred = self._token_preferred_slot(token)
        if preferred is None:
            return await method(*args, **kwargs), None

        # Small inter-call delay to smooth burst traffic
        call_delay = float(getattr(settings, 'GMGN_FEATURE_CALL_DELAY_SECONDS', 0.15) or 0.15)
        if call_delay > 0:
            await asyncio.sleep(call_delay)

        tried: Set[int] = set()
        last_exc: Optional[Exception] = None
        while True:
            slot = self._feature_slot_for_token(token, stage, exclude=tried)
            if slot is None:
                break
            tried.add(slot)
            try:
                return await method(*args, **kwargs, credential_slot=slot), slot
            except TypeError as exc:
                if self._is_credential_kw_unsupported(exc):
                    return await method(*args, **kwargs), None
                raise
            except Exception as exc:
                last_exc = exc
                if not self._is_slot_retryable_error(exc):
                    raise
                logger.warning(
                    "GMGN feature call slot unavailable; trying fallback",
                    token_mint=token.get("token_mint"),
                    stage=stage,
                    method=method_name,
                    slot=slot,
                    error=str(exc)[:200],
                )

            # Additional delay before fallback retry
            if call_delay > 0:
                await asyncio.sleep(call_delay)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("all feature slots cooldown or bucket empty")

    async def _insert_match_data_unavailable(
        self, token_mint: str, groups: List[dict], snapshot_id: int,
        discovery_event_ids: Dict[int, int], stage: str, reason: str,
    ):
        for sg in groups:
            sg_id = int(sg.get('id') or 0)
            try:
                await self.repo.insert_strategy_match(
                    token_mint, sg_id, int(sg.get('config_version') or 1), snapshot_id,
                    stage, False,
                    _json_dumps([{"rule": "data_unavailable", "passed": False,
                                  "reason": reason, "stage": stage, "missing": True}]),
                    _json_dumps({"error": reason, "stage": stage}),
                    discovery_event_id=discovery_event_ids.get(sg_id),
                )
            except Exception:
                pass

    async def _load_enabled_strategy_groups(self) -> List[dict]:
        try:
            groups = await self.repo.get_enabled_strategy_groups()
        except Exception as e:
            logger.error(f"load enabled strategy groups failed: {e}")
            groups = self.strategy_groups or []

        try:
            runtime = await self.repo.get_all_runtime_settings()
            user_mode = runtime.get('user_mode', 'IDLE')
        except Exception:
            user_mode = 'IDLE'

        if user_mode == 'SIM_TEST':
            groups = [g for g in groups if not bool(g.get('is_live'))]
        elif user_mode == 'FORMAL_SIM_LIVE':
            groups = list(groups)
        else:
            groups = []

        self.strategy_groups = groups
        return groups

    def _build_trench_params(self, platforms: Optional[List[str]] = None, x: Optional[float] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            'chain': 'sol',
            'type': 'new_creation',
        }
        types = _csv_list(getattr(settings, 'GMGN_TRENCHES_TYPES', ''))
        if types:
            params['type'] = types[0]
        if platforms:
            params['platforms'] = platforms
        if x is not None:
            t = compute_thresholds(x)
            params['trench_filters'] = t.to_trench_filters()
            params['_x'] = x
        return params

    @staticmethod
    def _group_by_timing(strategy_groups: List[dict]) -> List[Tuple[str, List[dict]]]:
        grouped: Dict[str, List[dict]] = defaultdict(list)
        key = 'default'
        for sg in strategy_groups:
            grouped[key].append(sg)
        return sorted(grouped.items(), key=lambda item: item[0])

    async def _store_snapshot_and_token(self, token: Dict[str, Any], now: datetime) -> Tuple[str, str, Any, int]:
        token_mint = token['token_mint']
        pool_address = token.get('pool_address') or ''
        pool_created_at = token.get('pool_created_at')

        await self.repo.upsert_token_first_seen(
            token_mint,
            symbol=token.get('symbol'),
            name=token.get('name'),
            pool_address=pool_address,
            launchpad=_first_present(token, 'launchpad', 'platform'),
            pool_created_at=pool_created_at,
            latest_state=token.get('type', 'discovered'),
        )

        snapshot_id = await self.repo.insert_token_metric_snapshot(
            token_mint,
            now.isoformat(),
            _json_dumps(token),
            **_snapshot_kwargs(token),
        )

        await self.repo.update_token_latest_snapshot(
            token_mint,
            latest_snapshot_id=snapshot_id,
            latest_price_usd=token.get('price_usd'),
            latest_price_sol=token.get('price_sol'),
            latest_liquidity_usd=token.get('liquidity_usd'),
            latest_sol_side_liquidity=token.get('sol_side_liquidity'),
            latest_market_cap=token.get('market_cap'),
            latest_type=token.get('type'),
        )
        return token_mint, pool_address, pool_created_at, snapshot_id

    async def _try_fetch_group(self, group_name: str, platforms: List[str], request_slot: int, role: str, custom_params: Optional[Dict[str, Any]] = None) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, Any]]:
        t0 = time.perf_counter()
        params = dict(custom_params) if custom_params else {}
        params.setdefault('platforms', platforms)
        params['platforms'] = platforms
        params.setdefault('chain', 'sol')
        if 'type' not in params and 'types' not in params:
            types = _csv_list(getattr(settings, 'GMGN_TRENCHES_TYPES', ''))
            params['type'] = types if types else 'new_creation'
        gres: Dict[str, Any] = {
            "group_name": group_name, "platforms": platforms,
            "slot": request_slot, "role": role, "ok": False,
            "raw_count": 0, "unique_count": 0, "duplicate_count": 0,
            "status_code": None, "error": None, "latency_ms": 0,
            "min_created": params.get("min_created"), "max_created": params.get("max_created"),
            "type": params.get("type") or params.get("types"),
        }
        try:
            items = await self.gmgn.fetch_trenches(params, credential_slot=request_slot)
            for item in items or []:
                item["_credential_slot"] = request_slot
                item["_trench_age_min"] = params.get("min_created")
                item["_trench_age_max"] = params.get("max_created")
                item["_trench_request_type"] = params.get("type") or params.get("types")
                item["_trench_type"] = item.get("type")
            gres["ok"] = True
            gres["raw_count"] = len(items)
            gres["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            return items, gres
        except Exception as e:
            gres["error"] = str(e)[:300]
            gres["status_code"] = getattr(e, 'status_code', None)
            gres["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            logger.warning(f"trenches fetch failed for {group_name} slot={request_slot}: {e}")
            return None, gres

    async def _fetch_trenches_two_group(self, custom_params: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        mode = settings.get_provider_mode()
        groups_results: List[Dict[str, Any]] = []
        all_items: List[Dict[str, Any]] = []
        dedup: Dict[Tuple[str, str], int] = {}
        rl = get_rate_limiter()
        platforms = self._all_discovery_platforms()
        discovery_slots = settings.get_discovery_slots()
        concurrency = int(getattr(settings, 'GMGN_TRENCHES_CONCURRENCY', 2) or 2)

        if not discovery_slots:
            return [], {
                "mode": "age_sharded_discovery",
                "raw_fetched_count": 0,
                "unique_fetched_count": 0,
                "duplicate_count_estimate": 0,
                "groups": [],
                "error": "no GMGN discovery credential slots configured",
            }

        sem = asyncio.Semaphore(concurrency)

        async def _fetch_one_slot(slot: int):
            min_created, max_created = self._slot_age_window(slot)
            group_name = f"age_{min_created}_{max_created}"

            if mode == ProviderMode.MOCK:
                params_base = dict(custom_params) if custom_params else {}
                params_base.setdefault('platforms', platforms)
                params_base['platforms'] = platforms
                params_base['type'] = list(DISCOVERY_TRENCH_TYPES)
                params_base['min_created'] = min_created
                params_base['max_created'] = max_created
                t0 = time.perf_counter()
                items = await self.gmgn.fetch_trenches(params_base)
                for item in items or []:
                    item["_credential_slot"] = slot
                    item["_trench_age_min"] = min_created
                    item["_trench_age_max"] = max_created
                    item["_trench_request_type"] = list(DISCOVERY_TRENCH_TYPES)
                    item["_trench_type"] = item.get("type")
                gres = {"group_name": group_name, "platforms": platforms, "slot": slot, "role": "mock",
                        "ok": True, "raw_count": len(items), "unique_count": 0, "duplicate_count": 0,
                        "status_code": None, "error": None, "latency_ms": int((time.perf_counter()-t0)*1000),
                        "min_created": min_created, "max_created": max_created, "type": list(DISCOVERY_TRENCH_TYPES)}
                return items, gres, slot

            if rl.is_slot_cooldown(slot):
                gres = {"group_name": group_name, "platforms": platforms, "slot": slot, "role": "age_shard",
                        "ok": False, "raw_count": 0, "unique_count": 0, "duplicate_count": 0,
                        "status_code": None, "error": "slot cooldown", "latency_ms": 0,
                        "min_created": min_created, "max_created": max_created, "type": list(DISCOVERY_TRENCH_TYPES)}
                return None, gres, slot

            # Build base params, then merge trench_filters from custom_params.
            base_params = self._build_trench_params(platforms=platforms)
            if custom_params:
                trench_filters = custom_params.get("trench_filters", {})
                if trench_filters:
                    base_params.update(trench_filters)
                debug_x = custom_params.get("_x")
                if debug_x is not None:
                    base_params["_debug_x"] = debug_x
                debug_ids = custom_params.get("_strategy_group_ids")
                if debug_ids is not None:
                    base_params["_debug_strategy_group_ids"] = debug_ids
            base_params["type"] = list(DISCOVERY_TRENCH_TYPES)
            base_params["min_created"] = min_created
            base_params["max_created"] = max_created

            send_params = strip_internal_debug_fields(base_params)

            logger.info(
                "trenches fetch params",
                x=custom_params.get("_x") if custom_params else None,
                platforms=platforms,
                trench_filters=custom_params.get("trench_filters") if custom_params else None,
                credential_slot=slot,
                min_created=min_created,
                max_created=max_created,
                types=DISCOVERY_TRENCH_TYPES,
                raw_params_sent=send_params,
            )

            items, gres = await self._try_fetch_group(group_name, platforms, slot, "age_shard", custom_params=send_params)
            final_items = items or [] if gres["ok"] else None
            return final_items, gres, slot

        async def _fetch_with_sem(slot: int):
            async with sem:
                return await _fetch_one_slot(slot)

        tasks = [_fetch_with_sem(slot) for slot in discovery_slots]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"trenches shard exception: {result}")
                continue
            final_items, final_gres, slot = result
            min_created, max_created = self._slot_age_window(slot)
            groups_results.append(final_gres)

            if final_items:
                duplicate_count = 0
                for item in final_items:
                    key = (str(item.get("token_mint", "")), str(item.get("pool_address", "")))
                    if key not in dedup:
                        dedup[key] = 0
                        all_items.append(item)
                    else:
                        duplicate_count += 1
                    dedup[key] += 1
                final_gres["duplicate_count"] = duplicate_count
                final_gres["unique_count"] = max(0, final_gres.get("raw_count", 0) - duplicate_count)

                logger.info(
                    "trenches fetch result",
                    group=final_gres.get("group_name"),
                    x=custom_params.get("_x") if custom_params else None,
                    raw_count=final_gres.get("raw_count", 0),
                    unique_count=final_gres.get("unique_count", 0),
                    slot=slot if final_gres.get("ok") else None,
                    min_created=min_created,
                    max_created=max_created,
                )

        for gr in groups_results:
            gr["unique_count"] = max(0, gr.get("unique_count", gr.get("raw_count", 0) - gr.get("duplicate_count", 0)))

        raw_total = sum(g.get("raw_count", 0) for g in groups_results)
        unique_total = len(all_items)
        dup_total = raw_total - unique_total

        diag = {
            "mode": "age_sharded_discovery",
            "raw_fetched_count": raw_total,
            "unique_fetched_count": unique_total,
            "duplicate_count_estimate": dup_total,
            "groups": groups_results,
        }
        return all_items, diag

    async def _enrich_token_if_needed(self, token: Dict[str, Any], token_mint: str) -> Dict[str, Any]:
        """Lightweight enrich: fetch_token_snapshot only if Stage 0 fields are missing.

        Returns the (possibly merged) token dict.  If enrichment was attempted,
        logs diagnostic info about raw-vs-enriched fields.
        """
        missing: List[str] = []
        for canonical, aliases in STAGE0_REQUIRED_ALIASES.items():
            found = False
            for a in aliases:
                v = token.get(a)
                if v is not None and v != "":
                    found = True
                    break
            if not found:
                missing.append(canonical)

        if not missing:
            return token

        logger.info(
            "trenches token missing stage0 fields; enriching",
            token_mint=token_mint,
            missing=missing,
        )

        try:
            snap, slot = await self._call_gmgn_with_token_slot(token, "snapshot", "fetch_token_snapshot", token_mint)
        except Exception as e:
            logger.warning("enrich fetch_token_snapshot failed", token_mint=token_mint, error=str(e))
            return token

        if not snap:
            return token

        enriched = dict(token)
        merged_count = 0
        for key, val in snap.items():
            if key in ("raw_json", "source_mode", "token_mint"):
                continue
            if val is not None and val != "":
                existing = token.get(key)
                if existing is None or existing == "":
                    enriched[key] = val
                    merged_count += 1

        still_missing = []
        for canonical, aliases in STAGE0_REQUIRED_ALIASES.items():
            found = False
            for a in aliases:
                v = enriched.get(a)
                if v is not None and v != "":
                    found = True
                    break
            if not found:
                still_missing.append(canonical)

        diag = {
            "token_mint": token_mint,
            "raw_keys": list(token.keys()),
            "enrich_snap_keys": list(snap.keys()),
            "merged_count": merged_count,
            "was_missing": missing,
            "still_missing": still_missing,
            "credential_slot": slot,
        }
        logger.info("enrich result", **diag)

        if still_missing:
            await self.repo.append_system_event(
                "WARN", "DISCOVERY",
                f"Token {token_mint} still missing Stage0 fields after enrich",
                _json_dumps(diag),
                account_type="SIM",
            )

        return enriched

    # ---- Staged filter pipeline ----
    # Stage 0: local trenches risk (w=0 extra API)
    # Stage 1: token info / price (w=1)
    # Stage 2: kline fallback (w=2, only young tokens needing it)
    # Stage 3: top1 holder via API (w=5)
    # Stage 4: smart degen (w=5)

    async def _run_stage0_risk_filter(
        self, token: Dict[str, Any], groups_for_t: List[dict], now: datetime,
        snapshot_id: int, token_mint: str, pool_address: str,
    ) -> Tuple[List[dict], Dict[int, int], int, int]:
        risk_passed: List[dict] = []
        discovery_event_ids: Dict[int, int] = {}
        discovered_count = 0
        tracked_count = 0

        for sg in groups_for_t:
            sg_id = int(sg.get('id') or 0)
            config_version = int(sg.get('config_version') or 1)
            try:
                existing = await self.repo.get_discovery_event_by_snapshot_token_pool(
                    snapshot_id, token_mint, pool_address, strategy_id=sg_id
                )
                if existing:
                    continue

                res = await run_entry_local_risk_filter(token, sg, now)
                status = 'RISK_PASSED' if res.passed else 'RISK_FAILED'

                discovery_id, created = await self.repo.create_discovery_event_idempotent(
                    token_mint=token_mint, pool_address=pool_address,
                    pool_created_at=token.get('pool_created_at'),
                    snapshot_id=snapshot_id, strategy_id=sg_id,
                    strategy_config_version=config_version, status=status,
                    feature_vector_json=_json_dumps(res.feature_vector),
                )
                if not created:
                    continue

                match_id = await self.repo.insert_strategy_match(
                    token_mint, sg_id, config_version, snapshot_id,
                    'risk_filter', res.passed,
                    _json_dumps([d.__dict__ for d in res.details]),
                    _json_dumps(res.feature_vector),
                    discovery_event_id=discovery_id,
                )
                await self.repo.update_discovery_event_status(
                    discovery_id, status, initial_match_id=match_id,
                    feature_vector_json=_json_dumps(res.feature_vector),
                )

                discovered_count += 1
                if res.passed:
                    risk_passed.append(sg)
                    discovery_event_ids[sg_id] = discovery_id
                    tracked_count += 1

                await event_bus.publish('discovery', {
                    'token_mint': token_mint, 'discovery_event_id': discovery_id,
                    'strategy_id': sg_id, 'status': status,
                    'passed_strategy_ids': [sg_id] if res.passed else [],
                    'failed_strategy_ids': [] if res.passed else [sg_id],
                })
            except Exception as e:
                logger.error(f"Stage 0 risk filter exception for {token_mint} sg={sg_id}: {e}")

        return risk_passed, discovery_event_ids, discovered_count, tracked_count

    async def _run_stage1_price_info(
        self, token_mint: str, token: Dict[str, Any], groups: List[dict],
        snapshot_id: int, discovery_event_ids: Dict[int, int],
    ) -> Tuple[List[dict], List[dict], Dict[str, Any]]:
        """Stage 1: token info (w=1). Evaluates swaps + price_change. Returns (passed, needs_kline, diag)."""
        stage_diag: Dict[str, Any] = {"candidates_in": len(groups), "checked": 0, "passed": 0, "failed": 0, "needs_kline": 0}
        passed_groups: List[dict] = []
        needs_kline_groups: List[dict] = []

        if not groups:
            return passed_groups, needs_kline_groups, stage_diag

        try:
            latest, slot = await self._call_gmgn_with_token_slot(token, "price_info", "fetch_latest_price", token_mint)
            stage_diag["credential_slot"] = slot
        except Exception as e:
            logger.warning(f"latest price fetch failed for {token_mint}: {e}")
            await self._insert_match_data_unavailable(
                token_mint, groups, snapshot_id, discovery_event_ids,
                'price_filter', f"API failed: {e}")
            stage_diag["failed"] = len(groups)
            return passed_groups, needs_kline_groups, stage_diag

        if not latest:
            await self._insert_match_data_unavailable(
                token_mint, groups, snapshot_id, discovery_event_ids,
                'price_filter', 'latest price response empty')
            stage_diag["failed"] = len(groups)
            return passed_groups, needs_kline_groups, stage_diag

        token["_latest_price_for_kline_fallback"] = latest

        creation_ts, _, age_missing = _parse_creation_ts(token)
        age_minutes = _compute_age_minutes(creation_ts)

        for sg in groups:
            sg_id = int(sg.get('id') or 0)
            config_version = int(sg.get('config_version') or 1)
            discovery_id = discovery_event_ids.get(sg_id)

            res = await evaluate_price_activity_rules(token, sg, latest, klines=None)
            stage_diag["checked"] += 1

            if res.passed:
                stage_diag["passed"] += 1
                passed_groups.append(sg)
                await self.repo.insert_strategy_match(
                    token_mint, sg_id, config_version, snapshot_id,
                    'price_filter', True,
                    _json_dumps(res.details), _json_dumps(res.feature_vector),
                    discovery_event_id=discovery_id,
                )
            else:
                pct_detail = next((d for d in res.details if d.get("rule") == "price_change_1h"), None)
                range_detail = next((d for d in res.details if d.get("rule") == "price_range_24h_percentile"), None)
                price_change_need_kline = (age_minutes is not None and age_minutes < 60 and
                                           pct_detail and pct_detail.get("age_mode") == "young_no_kline_fallback" and
                                           str(pct_detail.get("source")) == "missing")
                range_need_kline = bool(range_detail and range_detail.get("data_unavailable"))
                young_need_kline = price_change_need_kline or range_need_kline

                if young_need_kline:
                    needs_kline_groups.append(sg)
                    stage_diag["needs_kline"] += 1
                else:
                    stage_diag["failed"] += 1
                    await self.repo.insert_strategy_match(
                        token_mint, sg_id, config_version, snapshot_id,
                        'price_filter', False,
                        _json_dumps(res.details), _json_dumps(res.feature_vector),
                        discovery_event_id=discovery_id,
                    )

        return passed_groups, needs_kline_groups, stage_diag

    async def _run_stage2_kline_fallback(
        self, token_mint: str, token: Dict[str, Any], groups: List[dict],
        snapshot_id: int, discovery_event_ids: Dict[int, int],
    ) -> Tuple[List[dict], Dict[str, Any]]:
        """Stage 2: Kline fallback for young tokens that Stage 1 couldn't evaluate (w=2)."""
        stage_diag: Dict[str, Any] = {"candidates_in": len(groups), "checked": 0, "kline_used": 0, "passed": 0, "failed": 0}
        passed_groups: List[dict] = []

        if not groups:
            return passed_groups, stage_diag

        creation_ts, _, age_missing = _parse_creation_ts(token)
        age_minutes = _compute_age_minutes(creation_ts)

        try:
            now_ts = int(datetime.now(timezone.utc).timestamp())
            from_ts = max(int(creation_ts), now_ts - 86400) if creation_ts else now_ts - 86400
            klines, slot = await self._call_gmgn_with_token_slot(
                token,
                "kline",
                "fetch_kline",
                token_mint, "1m", 1440,
                from_ts=from_ts,
                to_ts=now_ts,
            )
            stage_diag["kline_used"] = 1
            stage_diag["credential_slot"] = slot
        except Exception as e:
            logger.warning(f"kline fetch failed for {token_mint}: {e}")
            await self._insert_match_data_unavailable(
                token_mint, groups, snapshot_id, discovery_event_ids,
                'kline_fallback', f"kline API failed: {e}")
            stage_diag["failed"] = len(groups)
            return passed_groups, stage_diag

        for sg in groups:
            sg_id = int(sg.get('id') or 0)
            config_version = int(sg.get('config_version') or 1)
            discovery_id = discovery_event_ids.get(sg_id)
            latest = token.get("_latest_price_for_kline_fallback")
            latest = latest if isinstance(latest, dict) else {}
            res = await evaluate_price_activity_rules(token, sg, latest, klines=klines)
            await self.repo.insert_strategy_match(
                token_mint, sg_id, config_version, snapshot_id,
                'kline_fallback', res.passed,
                _json_dumps(res.details), _json_dumps(res.feature_vector),
                discovery_event_id=discovery_id,
            )
            stage_diag["checked"] += 1
            if res.passed:
                stage_diag["passed"] += 1
                passed_groups.append(sg)
            else:
                stage_diag["failed"] += 1

        return passed_groups, stage_diag

    async def _run_stage3_top_holder(
        self, token_mint: str, token: Dict[str, Any], groups: List[dict],
        snapshot_id: int, discovery_event_ids: Dict[int, int],
    ) -> Tuple[List[dict], Dict[str, Any]]:
        """Stage 3: top1 holder via holders API (w=5)."""
        stage_diag: Dict[str, Any] = {"candidates_in": len(groups), "checked": 0, "passed": 0, "failed": 0}
        passed_groups: List[dict] = []

        if not groups:
            return passed_groups, stage_diag

        try:
            holders, slot = await self._call_gmgn_with_token_slot(token, "top_holder", "fetch_top_holders", token_mint, limit=20)
            stage_diag["credential_slot"] = slot
        except Exception as e:
            logger.warning(f"top holders fetch failed for {token_mint}: {e}")
            await self._insert_match_data_unavailable(
                token_mint, groups, snapshot_id, discovery_event_ids,
                'top_holder_filter', f"holders API failed: {e}")
            stage_diag["failed"] = len(groups)
            return passed_groups, stage_diag

        if not holders:
            await self._insert_match_data_unavailable(
                token_mint, groups, snapshot_id, discovery_event_ids,
                'top_holder_filter', 'holders API returned empty')
            stage_diag["failed"] = len(groups)
            return passed_groups, stage_diag

        for sg in groups:
            sg_id = int(sg.get('id') or 0)
            config_version = int(sg.get('config_version') or 1)
            discovery_id = discovery_event_ids.get(sg_id)
            x = float(sg.get("x") if sg.get("x") is not None else settings.STRATEGY_DEFAULT_X)
            top1_min_threshold = 0.031 - 0.01 * x
            top1_max_threshold = 0.049 + 0.01 * x

            top1_holder = None
            for h in holders:
                try:
                    at = int(h.get("addr_type", -1))
                except Exception:
                    at = -1
                if at == 0:
                    top1_holder = h
                    break

            rate = None
            if top1_holder:
                rate = normalize_rate_fraction(_to_float(_first_present(top1_holder, "top1_holder_rate", "rate", "amount_percentage", "percentage", "hold_rate")))

            passed = rate is not None and top1_min_threshold < rate < top1_max_threshold
            detail = {"rule": "top1_holder_addr_type0", "passed": passed,
                      "value": rate, "threshold_min": top1_min_threshold,
                      "threshold_max": top1_max_threshold,
                      "missing": rate is None}
            feature_vector = {"top1_holder_rate": rate, "top1_threshold_min": top1_min_threshold,
                              "top1_threshold_max": top1_max_threshold, "stage": "top_holder_filter"}

            await self.repo.insert_strategy_match(
                token_mint, sg_id, config_version, snapshot_id,
                'top_holder_filter', passed,
                _json_dumps([detail]), _json_dumps(feature_vector),
                discovery_event_id=discovery_id,
            )
            stage_diag["checked"] += 1
            if passed:
                stage_diag["passed"] += 1
                passed_groups.append(sg)
            else:
                stage_diag["failed"] += 1

        return passed_groups, stage_diag

    async def _run_stage4_smart_degen(
        self, token_mint: str, token: Dict[str, Any], groups: List[dict],
        snapshot_id: int, discovery_event_ids: Dict[int, int],
    ) -> Tuple[List[dict], Dict[str, Any], Optional[List[Dict[str, Any]]]]:
        """Stage 4: Smart degen holders (w=5). Returns (passed, diag, smart_degen_holders)."""
        stage_diag: Dict[str, Any] = {"candidates_in": len(groups), "checked": 0, "passed": 0, "failed": 0}
        passed_groups: List[dict] = []

        if not groups:
            return passed_groups, stage_diag, None

        try:
            smart_degen_holders, slot = await self._call_gmgn_with_token_slot(token, "smart_degen", "fetch_smart_degen_holders", token_mint, limit=30)
            stage_diag["credential_slot"] = slot
        except Exception as e:
            logger.warning(f"smart degen fetch failed for {token_mint}: {e}")
            await self._insert_match_data_unavailable(
                token_mint, groups, snapshot_id, discovery_event_ids,
                'smart_degen_filter', f"degen API failed: {e}")
            stage_diag["failed"] = len(groups)
            return passed_groups, stage_diag, None

        for sg in groups:
            sg_id = int(sg.get('id') or 0)
            config_version = int(sg.get('config_version') or 1)
            discovery_id = discovery_event_ids.get(sg_id)
            res = await evaluate_smart_degen(sg, smart_degen_holders)
            await self.repo.insert_strategy_match(
                token_mint, sg_id, config_version, snapshot_id,
                'smart_degen_filter', res.passed,
                _json_dumps(res.details), _json_dumps(res.feature_vector),
                discovery_event_id=discovery_id,
            )
            stage_diag["checked"] += 1
            if res.passed:
                stage_diag["passed"] += 1
                passed_groups.append(sg)
            else:
                stage_diag["failed"] += 1

        return passed_groups, stage_diag, smart_degen_holders if passed_groups else None

    async def _save_top3_smart_money_baselines(self, position_id: int, token_mint: str, degen_holders: List[Dict[str, Any]]):
        """Save TOP3 smart money baselines for a concrete created position."""
        if not degen_holders:
            return
        try:
            pos_id = int(position_id)
            if not pos_id:
                return
            if hasattr(self.repo, "delete_smart_money_baselines_for_position"):
                await self.repo.delete_smart_money_baselines_for_position(pos_id)
            holders_sorted = sorted(degen_holders, reverse=True,
                key=lambda h: (float(h.get("amount_percentage", 0) or 0) / 100.0 if (h.get("amount_percentage") and float(str(h.get("amount_percentage", 0))) > 1.0) else float(h.get("amount_percentage", 0) or 0)))
            top3 = holders_sorted[:3]
            for i, holder in enumerate(top3):
                wallet = str(holder.get("address") or holder.get("wallet") or "")
                if not wallet:
                    continue
                amt_pct = float(holder.get("amount_percentage", 0) or 0)
                if amt_pct > 1.0:
                    amt_pct = amt_pct / 100.0
                usd_val = float(holder.get("usd_value", 0) or 0)
                try:
                    await self.repo.insert_smart_money_baseline(
                        pos_id, token_mint, wallet, i + 1, amt_pct, usd_val,
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"save_top3_smart_money_baselines failed for position_id={position_id} token={token_mint}: {e}")

    async def run_once(self):
        async with self._run_lock:
            await self._run_once_locked()

    async def _run_once_locked(self):
        now = datetime.now(timezone.utc)
        run_started_at = now.isoformat()
        t0 = now.timestamp()
        total_fetched = 0
        discovered_count = 0
        tracked_count = 0
        dedup_skipped = 0
        mode = settings.get_provider_mode()
        stage_diags: Dict[str, Any] = {}
        trench_diag: Dict[str, Any] = {}

        strategy_groups = await self._load_enabled_strategy_groups()
        if not strategy_groups:
            await self.repo.append_system_event(
                'WARNING', 'DISCOVERY', 'No enabled strategy groups for current runtime mode; skip trench discovery',
                '{}', account_type='SIM'
            )
            self.processed_count = 0
            self.last_elapsed_ms = int((datetime.now(timezone.utc).timestamp() - t0) * 1000)
            return

        # Group strategy groups by unique x value for trenches pushdown
        unique_xs = self._unique_x_values(strategy_groups)
        if not unique_xs:
            await self.repo.append_system_event(
                'WARNING', 'DISCOVERY', 'No unique x values from strategy groups; skip discovery',
                '{}', account_type='SIM'
            )
            self.processed_count = 0
            self.last_elapsed_ms = int((datetime.now(timezone.utc).timestamp() - t0) * 1000)
            return

        groups_by_x: Dict[float, List[dict]] = {}
        for sg in strategy_groups:
            x = float(sg.get("x") if sg.get("x") is not None else settings.STRATEGY_DEFAULT_X)
            x = round(x, 6)
            groups_by_x.setdefault(x, []).append(sg)

        logger.info(
            "discovery multi-x trenches pushdown",
            unique_xs=unique_xs,
            groups_by_x={str(k): len(v) for k, v in groups_by_x.items()},
            count=len(unique_xs),
            strategy_groups=len(strategy_groups),
        )

        for timing_key, groups_for_t in self._group_by_timing(strategy_groups):
            per_x_diags: Dict[str, Any] = {}

            for x, groups_for_x in groups_by_x.items():
                trench_filters = build_trench_filters_for_x(x)
                custom_params = {
                    "trench_filters": dict(trench_filters),
                    "_x": x,
                    "_strategy_group_ids": [g["id"] for g in groups_for_x],
                }
                x_trenches, x_diag = await self._fetch_trenches_two_group(custom_params=custom_params)
                x_diag["x"] = x
                x_diag["strategy_group_ids"] = [g["id"] for g in groups_for_x]
                per_x_diags[str(x)] = x_diag

                # Stage-gap cooling: pause before hitting same discovery slots with feature APIs
                post_delay = float(getattr(settings, 'GMGN_POST_TRENCHES_STAGE_DELAY_SECONDS', 2.0) or 2.0)
                if post_delay > 0 and mode != ProviderMode.MOCK and x_trenches:
                    logger.info("post-trenches stage cooldown", delay_s=post_delay, x=x,
                                unique_tokens=len(x_trenches))
                    await asyncio.sleep(post_delay)

                logger.info(
                    "discovery per-x trenches result",
                    x=x, strategy_group_ids=[g["id"] for g in groups_for_x],
                    fetched=len(x_trenches),
                    trench_filters_payload=trench_filters,
                )

                for token in x_trenches:
                    token_mint = token.get('token_mint')
                    if not token_mint:
                        continue

                    source_mode = token.get('source_mode', 'MOCK')
                    if mode != ProviderMode.MOCK and source_mode == 'MOCK' and token_mint in MOCK_MINTS:
                        continue

                    try:
                        token_mint, pool_address, pool_created_at, snapshot_id = await self._store_snapshot_and_token(token, now)
                    except Exception as e:
                        logger.error(f"store discovery snapshot failed token={token_mint}: {e}")
                        continue

                    # Lightweight enrich: fetch_token_snapshot only if Stage 0 fields are missing
                    enriched_token = await self._enrich_token_if_needed(token, token_mint)
                    if enriched_token is not token:
                        logger.info("enriched token for stage0", token_mint=token_mint)

                    # Stage 0: local risk filter — uses ONLY groups_for_x (this x's groups)
                    risk_passed, discovery_event_ids, dc, tc = await self._run_stage0_risk_filter(
                        enriched_token, groups_for_x, now, snapshot_id, token_mint, pool_address,
                    )
                    discovered_count += dc
                    tracked_count += tc
                    stage_diags.setdefault("stage0_risk", {"candidates": 0, "passed": 0})
                    stage_diags["stage0_risk"]["candidates"] += 1
                    stage_diags["stage0_risk"]["passed"] += len(risk_passed)

                    if not risk_passed:
                        continue

                    # Stage 1: token info / price (w=1)
                    price_passed, needs_kline, price_diag = await self._run_stage1_price_info(
                        token_mint, token, risk_passed, snapshot_id, discovery_event_ids,
                    )
                    stage_diags.setdefault("stage1_price", {"checked": 0, "passed": 0, "failed": 0, "needs_kline": 0})
                    stage_diags["stage1_price"]["checked"] += price_diag["checked"]
                    stage_diags["stage1_price"]["passed"] += price_diag["passed"]
                    stage_diags["stage1_price"]["failed"] += price_diag["failed"]
                    stage_diags["stage1_price"]["needs_kline"] += price_diag.get("needs_kline", 0)

                    # Stage 2: kline fallback (w=2) for young tokens that Stage 1 couldn't evaluate
                    kline_passed, kline_diag = await self._run_stage2_kline_fallback(
                        token_mint, token, needs_kline, snapshot_id, discovery_event_ids,
                    )
                    stage_diags.setdefault("stage2_kline", {"checked": 0, "kline_used": 0, "passed": 0, "failed": 0})
                    stage_diags["stage2_kline"]["checked"] += kline_diag["checked"]
                    stage_diags["stage2_kline"]["passed"] += kline_diag["passed"]
                    stage_diags["stage2_kline"]["failed"] += kline_diag["failed"]
                    stage_diags["stage2_kline"]["kline_used"] += kline_diag.get("kline_used", 0)

                    kline_passed_all = price_passed + kline_passed

                    if not kline_passed_all:
                        continue

                    # Stage 3: top1 holder via API (w=5)
                    holder_passed, holder_diag = await self._run_stage3_top_holder(
                        token_mint, token, kline_passed_all, snapshot_id, discovery_event_ids,
                    )
                    stage_diags.setdefault("stage3_holder", {"checked": 0, "passed": 0, "failed": 0})
                    stage_diags["stage3_holder"]["checked"] += holder_diag["checked"]
                    stage_diags["stage3_holder"]["passed"] += holder_diag["passed"]
                    stage_diags["stage3_holder"]["failed"] += holder_diag["failed"]

                    if not holder_passed:
                        continue

                    # Stage 4: smart degen (w=5)
                    final_passed, degen_diag, degen_holders = await self._run_stage4_smart_degen(
                        token_mint, token, holder_passed, snapshot_id, discovery_event_ids,
                    )
                    stage_diags.setdefault("stage4_degen", {"checked": 0, "passed": 0, "failed": 0})
                    stage_diags["stage4_degen"]["checked"] += degen_diag["checked"]
                    stage_diags["stage4_degen"]["passed"] += degen_diag["passed"]
                    stage_diags["stage4_degen"]["failed"] += degen_diag["failed"]

                    if final_passed:
                        try:
                            result = await self.pipeline.handle_token_second_filter_result(
                                token_mint, final_passed,
                                snapshot_id=snapshot_id,
                                discovery_event_id=discovery_event_ids.get(int(final_passed[0].get('id') or 0)),
                                discovery_event_ids_by_strategy=discovery_event_ids,
                            )
                            if degen_holders:
                                for created in (result or {}).get("created", []):
                                    try:
                                        position_id = int(created.get("position_id") or 0)
                                    except Exception:
                                        position_id = 0
                                    if position_id:
                                        await self._save_top3_smart_money_baselines(position_id, token_mint, degen_holders)
                        except Exception as e:
                            logger.error(f"pipeline entry failed for {token_mint}: {e}")

            all_unique = sum(
                d.get("unique_fetched_count", d.get("raw_fetched_count", 0))
                for d in per_x_diags.values()
            )
            total_fetched += all_unique

            trench_diag = {
                "unique_xs": unique_xs,
                "per_x": per_x_diags,
                "unique_fetched_count": all_unique,
                "groups": [],
            }

        self.processed_count = total_fetched
        self.last_elapsed_ms = int((datetime.now(timezone.utc).timestamp() - t0) * 1000)

        run_finished_at = datetime.now(timezone.utc).isoformat()
        context = {
            'count': total_fetched,
            'raw_fetched_count': sum(
                sum(g.get("raw_count", 0) for g in (d.get("groups") or []))
                for d in per_x_diags.values()
            ) if per_x_diags else 0,
            'unique_fetched_count': total_fetched,
            'duplicate_count_estimate': 0,
            'discovered': discovered_count,
            'tracked_initial_passed': tracked_count,
            'dedup_skipped': dedup_skipped,
            'enabled_strategy_groups': len(strategy_groups),
            'elapsed_ms': self.last_elapsed_ms,
            'run_started_at': run_started_at,
            'run_finished_at': run_finished_at,
            'fetch_mode': 'per_x_age_sharded_discovery',
            'trench_groups': [],
            'stage_diagnostics': stage_diags,
        }

        await self.repo.append_system_event(
            'INFO', 'DISCOVERY', 'Discovery run complete',
            _json_dumps(context),
            account_type='SIM',
        )
        await event_bus.publish('system', {
            'level': 'INFO', 'category': 'DISCOVERY',
            'message': f'Discovered {discovered_count} events, tracked {tracked_count}, skipped {dedup_skipped}',
        })
