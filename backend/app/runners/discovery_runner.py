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

STAGE0_REQUIRED_FIELDS = [
    "renounced_mint",
    "renounced_freeze_account",
    "is_wash_trading",
    "rat_trader_amount_rate",
    "suspected_insider_hold_rate",
    "sell_tax",
    "burn_status",
    "sniper_count",
    "liquidity_usd",
    "holder_count",
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
    "liquidity_usd": ["liquidity_usd", "liquidity", "pool_liquidity_usd"],
    "holder_count": ["holder_count", "holders", "total_holders", "holder"],
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


def acquire_feature_slot(stage: str = "") -> Optional[int]:
    stage_lower = (stage or "").lower()
    if "kline" in stage_lower:
        endpoint = getattr(settings, "GMGN_KLINE_PATH", "/v1/market/token_kline")
    elif "holder" in stage_lower or "degen" in stage_lower or "smart_money" in stage_lower:
        endpoint = getattr(settings, "GMGN_TOKEN_HOLDERS_PATH", "/v1/market/token_top_holders")
    else:
        endpoint = getattr(settings, "GMGN_TOKEN_INFO_PATH", "/v1/token/info")
    try:
        slot = get_credential_router().choose_slot(endpoint=endpoint)
        if slot is not None:
            return slot
    except Exception:
        pass
    rl = get_rate_limiter()
    for slot in settings.get_feature_slots():
        if rl.is_slot_available(slot):
            return slot
    return None


def acquire_holding_slot(stage: str = "") -> Optional[int]:
    endpoint = getattr(settings, "GMGN_TOKEN_INFO_PATH", "/v1/token/info")
    try:
        slot = get_credential_router().choose_slot(endpoint=endpoint)
        if slot is not None:
            return slot
    except Exception:
        pass
    rl = get_rate_limiter()
    for slot in settings.get_holding_slots():
        if rl.is_slot_available(slot):
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
        self._last_discovery_error_event_at: Dict[tuple, float] = {}

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
        for slot in settings.get_feature_slots():
            if not rl.is_slot_cooldown(slot):
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

    def _feature_slot_for_token(self, token: Dict[str, Any], stage: str, exclude: Optional[Set[int]] = None) -> Optional[int]:
        exclude = set(exclude or set())
        rl = get_rate_limiter()

        for slot in settings.get_feature_slots():
            if slot in exclude:
                continue
            if rl.is_slot_available(slot):
                return slot

        for slot in settings.get_discovery_slots():
            if slot in exclude:
                continue
            if rl.is_slot_available(slot):
                logger.warning("emergency_feature_borrow_discovery_slot", slot=slot, stage=stage)
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

    async def _call_with_slot_retry(
        self,
        stage: str,
        method_ref,
        params: tuple,
        primary_slot: Optional[int],
        validate_func,
        endpoint: str = "unknown",
        max_retries: int = 2,
    ) -> Tuple[Any, Optional[int], Dict[str, Any]]:
        rl = get_rate_limiter()
        feature_pool = settings.get_feature_slots()
        diag = {"stage": stage, "attempts": 0, "slots_tried": [], "errors": []}
        tried: Set[int] = set()

        attempt_slots = []
        if primary_slot is not None:
            attempt_slots.append(primary_slot)
        for s in feature_pool:
            if s not in attempt_slots:
                attempt_slots.append(s)

        max_attempts = max_retries + 1
        for idx, slot in enumerate(attempt_slots):
            if idx >= max_attempts:
                break
            if slot in tried:
                continue
            tried.add(slot)
            diag["attempts"] += 1
            diag["slots_tried"].append(slot)

            if not rl.is_slot_available(slot):
                diag["errors"].append({"slot": slot, "error": "unavailable"})
                continue

            try:
                result = await method_ref(slot)
                if validate_func(result):
                    diag["slot"] = slot
                    return result, slot, diag
                diag["errors"].append({"slot": slot, "error": "validation_failed"})
                await rl.report_failure(slot, endpoint=endpoint, kind="empty")
            except Exception as e:
                if self._is_slot_retryable_error(e):
                    diag["errors"].append({"slot": slot, "error": str(e)[:200]})
                    continue
                diag["errors"].append({"slot": slot, "error": str(e)[:200]})

        return None, None, diag

    def _validate_for_stage(self, stage: str) -> Tuple:
        if stage in ("price_info", "price_filter"):
            def _validate(r):
                if r is None:
                    return False
                price = r.get("price_usd") or r.get("price")
                return price is not None and float(price) > 0
            ep = getattr(settings, "GMGN_TOKEN_INFO_PATH", "/v1/token/info")
            return _validate, ep
        if stage == "snapshot":
            def _validate(r):
                return isinstance(r, dict) and bool(r) and "error" not in r
            ep = getattr(settings, "GMGN_TOKEN_SNAPSHOT_PATH", "/v1/token/security")
            return _validate, ep
        if stage in ("kline", "kline_fallback"):
            def _validate(r):
                return isinstance(r, list) and len(r) > 0
            ep = getattr(settings, "GMGN_KLINE_PATH", "/v1/market/token_kline")
            return _validate, ep
        if stage in ("top_holder", "top_holder_filter"):
            def _is_addr_type0(h):
                try:
                    return int(h.get("addr_type", -1)) == 0
                except Exception:
                    return False
            def _validate(r):
                if not isinstance(r, list) or len(r) == 0:
                    return False
                return any(_is_addr_type0(h) for h in r)
            ep = getattr(settings, "GMGN_TOKEN_HOLDERS_PATH", "/v1/market/token_top_holders")
            return _validate, ep
        if stage in ("smart_degen", "smart_degen_filter"):
            def _validate(r):
                return isinstance(r, list) and len(r) > 0
            ep = getattr(settings, "GMGN_TOKEN_HOLDERS_PATH", "/v1/market/token_top_holders")
            return _validate, ep
        return (lambda r: r is not None), "unknown"

    async def _call_gmgn_with_token_slot(
        self,
        token: Dict[str, Any],
        stage: str,
        method_name: str,
        *args,
        **kwargs,
    ) -> Tuple[Any, Optional[int]]:
        method = getattr(self.gmgn, method_name)
        preferred = self._feature_slot_for_token(token, stage)
        validate_func, endpoint = self._validate_for_stage(stage)

        call_delay = float(getattr(settings, 'GMGN_FEATURE_CALL_DELAY_SECONDS', 0.15) or 0.15)

        async def call_with_slot(slot: int):
            if call_delay > 0:
                await asyncio.sleep(call_delay)
            try:
                return await method(*args, **kwargs, credential_slot=slot)
            except TypeError as exc:
                if self._is_credential_kw_unsupported(exc):
                    return await method(*args, **kwargs)
                raise

        result, slot, diag = await self._call_with_slot_retry(
            stage=stage,
            method_ref=call_with_slot,
            params=(),
            primary_slot=preferred,
            validate_func=validate_func,
            endpoint=endpoint,
        )
        return result, slot

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
        # min_created: pool must be at least 60 minutes old
        params['min_created'] = '60m'
        return params

    @staticmethod
    def _group_by_timing(strategy_groups: List[dict]) -> List[Tuple[str, List[dict]]]:
        grouped: Dict[str, List[dict]] = defaultdict(list)
        key = 'default'
        for sg in strategy_groups:
            grouped[key].append(sg)
        return sorted(grouped.items(), key=lambda item: item[0])

    @staticmethod
    def _strategy_x(strategy_group: dict) -> float:
        return float(
            strategy_group.get("x")
            if strategy_group.get("x") is not None
            else settings.STRATEGY_DEFAULT_X
        )

    @classmethod
    def _requires_smart_degen(cls, strategy_group: dict) -> bool:
        from ..strategy.thresholds import requires_smart_degen_for_x
        x = cls._strategy_x(strategy_group)
        return requires_smart_degen_for_x(x)

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
            "type": params.get("type") or params.get("types"),
        }
        try:
            items = await self.gmgn.fetch_trenches(params, credential_slot=request_slot)
            for item in items or []:
                item["_credential_slot"] = request_slot
                item["_trench_request_type"] = params.get("type") or params.get("types")
                item["_trench_type"] = item.get("type")
            gres["ok"] = True
            gres["raw_count"] = len(items)
            gres["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            return items, gres
        except Exception as e:
            gres["error"] = str(e)[:300]
            gres["status_code"] = getattr(e, 'status_code', None)
            gres["retryable"] = getattr(e, 'retryable', None)
            gres["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            logger.warning(f"trenches fetch failed for {group_name} slot={request_slot}: {e}")

            event_key = (group_name, request_slot, gres.get("status_code"), str(e)[:50])
            now = time.time()
            last_at = self._last_discovery_error_event_at.get(event_key, 0)
            if now - last_at <= 60:
                return None, gres
            self._last_discovery_error_event_at[event_key] = now

            try:
                gmgn_error = ""
                err_msg = str(e)
                if "AUTH_CLIENT_ID_REPLAYED" in err_msg:
                    gmgn_error = "AUTH_CLIENT_ID_REPLAYED"
                elif "AUTH_TIMESTAMP_EXPIRED" in err_msg:
                    gmgn_error = "AUTH_TIMESTAMP_EXPIRED"
                await self.repo.append_system_event(
                    "WARNING",
                    "DISCOVERY",
                    f"trenches fetch failed for {group_name} slot={request_slot}",
                    _json_dumps({
                        "group_name": group_name,
                        "slot": request_slot,
                        "role": role,
                        "status_code": gres.get("status_code"),
                        "error": str(e)[:500],
                        "gmgn_error": gmgn_error,
                        "type": params.get("type") or params.get("types"),
                        "platforms_count": len(platforms or []),
                    }),
                    account_type="SIM",
                )
            except Exception:
                pass

            return None, gres

    async def _fetch_trenches_by_type(self, custom_params: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        mode = settings.get_provider_mode()
        groups_results: List[Dict[str, Any]] = []
        all_items: List[Dict[str, Any]] = []
        dedup: Dict[Tuple, int] = {}
        dedup_meta: Dict[Tuple, Dict[str, Any]] = {}
        per_type_raw_count: Dict[str, int] = {}
        per_type_unique_count: Dict[str, int] = {}
        rl = get_rate_limiter()
        platforms = self._all_discovery_platforms()

        type_slots = settings.get_discovery_type_slots()
        reserve_slot = settings.get_discovery_reserve_slot()
        feature_pool = settings.get_feature_slots()

        if not type_slots or all(v is None for v in type_slots.values()):
            return [], {
                "mode": "type_sharded_discovery",
                "raw_fetched_count": 0,
                "unique_fetched_count": 0,
                "duplicate_count_estimate": 0,
                "per_type_raw_count": {},
                "per_type_unique_count": {},
                "groups": [],
                "error": "no GMGN discovery type slots configured",
            }

        def _make_key(item: Dict[str, Any]) -> Tuple:
            t_mint = str(item.get("token_mint", ""))
            p_addr = str(item.get("pool_address", ""))
            if p_addr:
                return (t_mint, p_addr)
            return (t_mint,)

        async def _fetch_one_type(trench_type: str, primary_slot: Optional[int]) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, Any]]:
            group_name = f"{trench_type}_shard"

            if mode == ProviderMode.MOCK:
                params_base = dict(custom_params) if custom_params else {}
                params_base.setdefault('platforms', platforms)
                params_base['platforms'] = platforms
                params_base['type'] = [trench_type]
                t0 = time.perf_counter()
                items = await self.gmgn.fetch_trenches(params_base)
                for item in items or []:
                    item["_credential_slot"] = primary_slot
                    item["_trench_request_type"] = trench_type
                    item["_trench_type"] = item.get("type")
                gres = {"group_name": group_name, "platforms": platforms, "slot": primary_slot, "role": "mock",
                        "ok": True, "raw_count": len(items), "unique_count": 0, "duplicate_count": 0,
                        "status_code": None, "error": None, "latency_ms": int((time.perf_counter()-t0)*1000),
                        "type": [trench_type]}
                per_type_raw_count[trench_type] = per_type_raw_count.get(trench_type, 0) + len(items)
                return items, gres

            slot_sequence: List[int] = []
            if primary_slot is not None:
                slot_sequence.append(primary_slot)
            if reserve_slot is not None:
                slot_sequence.append(reserve_slot)
            for s in feature_pool:
                if s not in slot_sequence:
                    slot_sequence.append(s)

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
            base_params["type"] = [trench_type]

            last_error: Optional[str] = None
            attempts = 0
            empty_attempts = 0
            failure_attempts = 0

            max_attempts = min(settings.GMGN_DISCOVERY_MAX_ATTEMPTS_PER_TYPE, len(slot_sequence))

            for idx, slot in enumerate(slot_sequence):
                if idx >= max_attempts:
                    break
                attempts += 1

                if not rl.is_slot_available(slot):
                    gres_fail = {
                        "group_name": group_name, "platforms": platforms, "slot": slot, "role": "type_shard",
                        "ok": False, "raw_count": 0, "unique_count": 0, "duplicate_count": 0,
                        "status_code": None, "error": "slot unavailable", "latency_ms": 0, "type": [trench_type],
                    }
                    groups_results.append(gres_fail)
                    last_error = "slot unavailable"
                    continue

                send_params = strip_internal_debug_fields(base_params)

                logger.info(
                    "trenches fetch params",
                    x=custom_params.get("_x") if custom_params else None,
                    platforms=platforms,
                    trench_type=trench_type,
                    credential_slot=slot,
                    attempt=idx + 1,
                    raw_params_sent=send_params,
                )

                items, gres = await self._try_fetch_group(group_name, platforms, slot, "type_shard", custom_params=send_params)
                groups_results.append(gres)

                if items is not None and len(items) > 0:
                    for item in items:
                        item["_credential_slot"] = slot
                        item["_trench_request_type"] = trench_type
                        item["_trench_type"] = item.get("type")

                    per_type_raw_count[trench_type] = per_type_raw_count.get(trench_type, 0) + len(items)

                    if items:
                        dup_count = 0
                        for item in items:
                            key = _make_key(item)
                            if key not in dedup:
                                dedup[key] = 0
                                dedup_meta[key] = {
                                    "count": 0,
                                    "types_seen": [],
                                    "slots_seen": [],
                                }
                                all_items.append(item)
                                per_type_unique_count[trench_type] = per_type_unique_count.get(trench_type, 0) + 1
                            else:
                                dup_count += 1
                            dedup[key] += 1
                            dedup_meta[key]["count"] = dedup[key]
                            if trench_type not in dedup_meta[key]["types_seen"]:
                                dedup_meta[key]["types_seen"].append(trench_type)
                            if slot not in dedup_meta[key]["slots_seen"]:
                                dedup_meta[key]["slots_seen"].append(slot)
                        gres["duplicate_count"] = dup_count
                        gres["unique_count"] = max(0, gres.get("raw_count", 0) - dup_count)

                    logger.info(
                        "trenches fetch result",
                        group=group_name,
                        trench_type=trench_type,
                        x=custom_params.get("_x") if custom_params else None,
                        raw_count=gres.get("raw_count", 0),
                        unique_count=gres.get("unique_count", 0),
                        slot=slot,
                    )
                    return items, gres

                if items is not None and len(items) == 0:
                    empty_attempts += 1
                    last_error = "empty response"
                    continue

                if items is None:
                    failure_attempts += 1
                    last_error = gres.get("error", "unknown error")
                    if gres.get("retryable") is False:
                        break
                    continue

            all_empty = empty_attempts > 0 and failure_attempts == 0
            if all_empty:
                logger.warning(f"discovery returned empty for {trench_type} after {attempts} attempts")
            else:
                logger.critical(f"all {attempts} discovery attempts failed for {trench_type}")
            event_data = {
                "trench_type": trench_type,
                "primary_slot": primary_slot,
                "reserve_slot": reserve_slot,
                "attempts": attempts,
                "empty_attempts": empty_attempts,
                "failure_attempts": failure_attempts,
                "last_error": last_error,
                "slots_tried": slot_sequence,
            }
            try:
                await self.repo.append_system_event(
                    'WARN' if all_empty else 'CRITICAL', 'DISCOVERY',
                    f"All {attempts} discovery fetch attempts returned empty for {trench_type}" if all_empty else f"All {attempts} discovery fetch attempts failed for {trench_type}: {last_error}",
                    _json_dumps(event_data),
                    account_type='SIM',
                )
            except Exception:
                pass

            gres_final = {
                "group_name": group_name, "platforms": platforms, "slot": None, "role": "type_shard",
                "ok": True if all_empty else False, "raw_count": 0, "unique_count": 0, "duplicate_count": 0,
                "status_code": None, "error": last_error, "latency_ms": 0, "type": [trench_type],
                "empty": all_empty,
            }
            groups_results.append(gres_final)
            return None, gres_final

        tasks = [
            _fetch_one_type("new_creation", type_slots.get("new_creation")),
            _fetch_one_type("near_completion", type_slots.get("near_completion")),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"trenches type fetch exception: {result}")
                continue

        for gr in groups_results:
            gr["unique_count"] = max(0, gr.get("unique_count", gr.get("raw_count", 0) - gr.get("duplicate_count", 0)))

        raw_total = sum(g.get("raw_count", 0) for g in groups_results)
        unique_total = len(all_items)
        dup_total = raw_total - unique_total

        duplicate_keys: List[Dict[str, Any]] = []
        for key, meta in dedup_meta.items():
            if meta["count"] > 1:
                duplicate_keys.append({
                    "token_mint": key[0],
                    "pool_address": key[1] if len(key) > 1 else None,
                    "seen_count": meta["count"],
                    "types": meta["types_seen"],
                    "slots": meta["slots_seen"],
                })

        if duplicate_keys:
            diag_dup = {
                "duplicate_count": len(duplicate_keys),
                "duplicate_keys_sample": duplicate_keys[:10],
                "per_type_raw_count": per_type_raw_count,
                "per_type_unique_count": per_type_unique_count,
            }
        else:
            diag_dup = {}

        diag = {
            "mode": "type_sharded_discovery",
            "raw_fetched_count": raw_total,
            "unique_fetched_count": unique_total,
            "duplicate_count_estimate": dup_total,
            "per_type_raw_count": per_type_raw_count,
            "per_type_unique_count": per_type_unique_count,
            **diag_dup,
            "groups": groups_results,
        }
        return all_items, diag

    async def _enrich_token_if_needed(self, token: Dict[str, Any], token_mint: str) -> Dict[str, Any]:
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

    # ----------------------------------------------------------------
    # STRICT AND SCREENING PIPELINE
    #
    # Stage 0: risk_filter       — local risk filter (no extra API)
    # Stage 1: top_holder_filter — top1 holder rate via holders API
    # Stage 2: smart_degen_filter— smart degen holders via holders API
    # Stage 3: price_filter      — latest price + kline evaluation (活跃度与价格面)
    # Stage 4: create position
    #
    # If any stage fails, ALL subsequent stages are skipped (strict AND).
    # ----------------------------------------------------------------

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

    async def _run_stage1_top_holder(
        self, token_mint: str, token: Dict[str, Any], groups: List[dict],
        snapshot_id: int, discovery_event_ids: Dict[int, int],
    ) -> Tuple[List[dict], Dict[str, Any]]:
        stage_diag: Dict[str, Any] = {"candidates_in": len(groups), "checked": 0, "passed": 0, "failed": 0}
        passed_groups: List[dict] = []

        if not groups:
            return passed_groups, stage_diag

        try:
            holders, slot = await self._call_gmgn_with_token_slot(token, "top_holder", "fetch_top_holders", token_mint, limit=20)
        except Exception as e:
            logger.warning(f"top holders fetch failed for {token_mint}: {e}")
            holders, slot = None, None

        if not holders:
            reason = "API retries exhausted" if slot is None else "holders API returned empty"
            await self._insert_match_data_unavailable(
                token_mint, groups, snapshot_id, discovery_event_ids,
                'top_holder_filter', reason)
            stage_diag["failed"] = len(groups)
            return passed_groups, stage_diag

        stage_diag["credential_slot"] = slot

        for sg in groups:
            sg_id = int(sg.get('id') or 0)
            config_version = int(sg.get('config_version') or 1)
            discovery_id = discovery_event_ids.get(sg_id)
            x = float(sg.get("x") if sg.get("x") is not None else settings.STRATEGY_DEFAULT_X)
            t = compute_thresholds(x)
            top1_min_threshold = t.top1_addr_type0_min
            top1_max_threshold = t.top1_addr_type0_max

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

    async def _run_stage2_smart_degen(
        self, token_mint: str, token: Dict[str, Any], groups: List[dict],
        snapshot_id: int, discovery_event_ids: Dict[int, int],
    ) -> Tuple[List[dict], Dict[str, Any], Optional[List[Dict[str, Any]]]]:
        original_count = len(groups)
        # 防御：只对要求聪明钱的策略组执行 smart_degen_filter
        groups = [sg for sg in groups if self._requires_smart_degen(sg)]
        stage_diag: Dict[str, Any] = {
            "candidates_in": original_count, "checked": 0, "passed": 0, "failed": 0,
            "skipped_not_required": original_count - len(groups),
        }
        passed_groups: List[dict] = []

        if not groups:
            return passed_groups, stage_diag, None

        try:
            smart_degen_holders, slot = await self._call_gmgn_with_token_slot(token, "smart_degen", "fetch_smart_degen_holders", token_mint, limit=30)
        except Exception as e:
            logger.warning(f"smart degen fetch failed for {token_mint}: {e}")
            smart_degen_holders, slot = None, None

        if not smart_degen_holders:
            reason = "API retries exhausted" if slot is None else "degen API returned empty"
            await self._insert_match_data_unavailable(
                token_mint, groups, snapshot_id, discovery_event_ids,
                'smart_degen_filter', reason)
            stage_diag["failed"] = len(groups)
            return passed_groups, stage_diag, None

        stage_diag["credential_slot"] = slot

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

    async def _run_stage3_price_filter(
        self, token_mint: str, token: Dict[str, Any], groups: List[dict],
        snapshot_id: int, discovery_event_ids: Dict[int, int],
    ) -> Tuple[List[dict], Dict[str, Any]]:
        stage_diag: Dict[str, Any] = {"candidates_in": len(groups), "checked": 0, "passed": 0, "failed": 0,
                                       "kline_invalid_or_missing_count": 0}
        passed_groups: List[dict] = []

        if not groups:
            return passed_groups, stage_diag

        # 拉最新价格
        try:
            latest, slot = await self._call_gmgn_with_token_slot(token, "price_info", "fetch_latest_price", token_mint)
        except Exception as e:
            logger.warning(f"latest price fetch failed for {token_mint}: {e}")
            latest, slot = None, None

        if not latest:
            reason = "API retries exhausted" if slot is None else "latest price response empty"
            await self._insert_match_data_unavailable(
                token_mint, groups, snapshot_id, discovery_event_ids,
                'price_filter', reason)
            stage_diag["failed"] = len(groups)
            return passed_groups, stage_diag

        stage_diag["credential_slot"] = slot

        # 拉 24h K 线
        klines = None
        creation_ts, _, _ = _parse_creation_ts(token)
        try:
            now_ts = int(datetime.now(timezone.utc).timestamp())
            from_ts = max(int(creation_ts), now_ts - 86400) if creation_ts else now_ts - 86400
            klines, kline_slot = await self._call_gmgn_with_token_slot(
                token, "kline", "fetch_kline",
                token_mint, "1m", 1440,
                from_ts=from_ts, to_ts=now_ts,
            )
        except Exception as e:
            logger.warning(f"kline fetch failed during price_filter for {token_mint}: {e}")
            klines = None

        for sg in groups:
            sg_id = int(sg.get('id') or 0)
            config_version = int(sg.get('config_version') or 1)
            discovery_id = discovery_event_ids.get(sg_id)

            res = await evaluate_price_activity_rules(
                token, sg, latest,
                klines=klines,
                require_kline=True,
            )
            stage_diag["checked"] += 1

            await self.repo.insert_strategy_match(
                token_mint, sg_id, config_version, snapshot_id,
                'price_filter', res.passed,
                _json_dumps(res.details), _json_dumps(res.feature_vector),
                discovery_event_id=discovery_id,
            )

            if res.passed:
                stage_diag["passed"] += 1
                passed_groups.append(sg)
            else:
                stage_diag["failed"] += 1
                if not res.feature_vector.get("kline_data_quality_pass"):
                    stage_diag["kline_invalid_or_missing_count"] += 1

        return passed_groups, stage_diag

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
                x_trenches, x_diag = await self._fetch_trenches_by_type(custom_params=custom_params)
                x_diag["x"] = x
                x_diag["strategy_group_ids"] = [g["id"] for g in groups_for_x]
                per_x_diags[str(x)] = x_diag

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

                    enriched_token = await self._enrich_token_if_needed(token, token_mint)
                    if enriched_token is not token:
                        logger.info("enriched token for stage0", token_mint=token_mint)

                    # ---- Stage 0: risk_filter ----
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

                    # ---- Stage 1: top_holder_filter ----
                    top_holder_passed, holder_diag = await self._run_stage1_top_holder(
                        token_mint, enriched_token, risk_passed, snapshot_id, discovery_event_ids,
                    )
                    stage_diags.setdefault("stage1_top_holder", {"checked": 0, "passed": 0, "failed": 0})
                    stage_diags["stage1_top_holder"]["checked"] += holder_diag["checked"]
                    stage_diags["stage1_top_holder"]["passed"] += holder_diag["passed"]
                    stage_diags["stage1_top_holder"]["failed"] += holder_diag["failed"]

                    if not top_holder_passed:
                        continue

                    # ---- Stage 2: smart_degen_filter (conditional) ----
                    degen_required = [sg for sg in top_holder_passed if self._requires_smart_degen(sg)]
                    degen_not_required = [sg for sg in top_holder_passed if not self._requires_smart_degen(sg)]

                    degen_passed = list(degen_not_required)  # 不要求聪明钱的直接通过
                    degen_holders = None

                    stage_diags.setdefault("stage2_degen", {"checked": 0, "passed": 0, "failed": 0, "skipped_not_required": 0})
                    stage_diags["stage2_degen"]["skipped_not_required"] += len(degen_not_required)

                    if degen_required:
                        required_passed, degen_diag, fetched_holders = await self._run_stage2_smart_degen(
                            token_mint, enriched_token, degen_required, snapshot_id, discovery_event_ids,
                        )
                        stage_diags["stage2_degen"]["checked"] += degen_diag["checked"]
                        stage_diags["stage2_degen"]["passed"] += degen_diag["passed"]
                        stage_diags["stage2_degen"]["failed"] += degen_diag["failed"]

                        degen_passed.extend(required_passed)
                        if fetched_holders:
                            degen_holders = fetched_holders

                    if not degen_passed:
                        continue

                    # ---- Stage 3: price_filter（活跃度与价格面）----
                    price_passed, price_diag = await self._run_stage3_price_filter(
                        token_mint, enriched_token, degen_passed, snapshot_id, discovery_event_ids,
                    )
                    stage_diags.setdefault("stage3_price", {"checked": 0, "passed": 0, "failed": 0,
                                                             "kline_invalid_or_missing_count": 0})
                    stage_diags["stage3_price"]["checked"] += price_diag["checked"]
                    stage_diags["stage3_price"]["passed"] += price_diag["passed"]
                    stage_diags["stage3_price"]["failed"] += price_diag["failed"]
                    stage_diags["stage3_price"]["kline_invalid_or_missing_count"] += price_diag.get("kline_invalid_or_missing_count", 0)

                    if not price_passed:
                        continue

                    # ---- Stage 4: create position ----
                    try:
                        result = await self.pipeline.handle_token_second_filter_result(
                            token_mint, price_passed,
                            snapshot_id=snapshot_id,
                            discovery_event_id=discovery_event_ids.get(int(price_passed[0].get('id') or 0)),
                            discovery_event_ids_by_strategy=discovery_event_ids,
                        )
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

        if getattr(settings, 'GMGN_TRENCHES_DEBUG_RELAXED_ON_ZERO', False):
            all_raw_zero = all(
                d.get("raw_fetched_count", 0) == 0
                for d in per_x_diags.values()
            )
            diag_discovery_slots = settings.get_discovery_slots()
            if all_raw_zero and diag_discovery_slots and mode != ProviderMode.MOCK:
                logger.warning("all trenches returned 0 raw tokens; issuing relaxed diagnostic request")
                diag_slot = diag_discovery_slots[0]
                try:
                    relaxed_params = {
                        "chain": "sol",
                        "platforms": self._all_discovery_platforms(),
                        "type": list(DISCOVERY_TRENCH_TYPES),
                        "limit": 5,
                    }
                    diag_items = await self.gmgn.fetch_trenches(relaxed_params, credential_slot=diag_slot)
                    logger.info(
                        "diagnostic relaxed trenches result",
                        slot=diag_slot,
                        count=len(diag_items) if diag_items else 0,
                    )
                except Exception as diag_e:
                    logger.warning("diagnostic relaxed trenches failed", error=str(diag_e)[:200])

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
            'fetch_mode': 'and_screening_discovery',
            'trench_groups': [
                g for d in per_x_diags.values()
                for g in (d.get("groups") or [])
            ],
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
