import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Set

from ..config import ProviderMode, settings
from ..db.repositories import Repositories
from ..logging_config import logger
from ..providers.base import MarketDataProvider, SwapProvider, ExecutionProvider, RpcProvider
from ..providers.rate_limiter import get_rate_limiter
from ..services.event_bus import event_bus
from ..strategy.filters import (
    run_risk_filter, evaluate_price_activity_rules, evaluate_smart_degen,
    _parse_creation_ts, _compute_age_minutes, sort_klines,
)
from ..trading.executor import TradingPipeline

MOCK_MINTS = {'PASS1', 'PASS1_150', 'PASS1_510', 'FAIL_INIT', 'FAIL_SECOND'}

SNAPSHOT_COLUMNS = [
    'pool_address', 'platform', 'launchpad',
    'type', 'liquidity_usd', 'sol_side_liquidity', 'volume_usd', 'market_cap',
    'price_usd', 'price_sol', 'top_10_holder_rate', 'top1_holder_rate',
    'renounced_mint', 'renounced_freeze_account', 'max_rug_ratio',
    'max_insider_ratio', 'max_entrapment_ratio', 'is_wash_trading',
    'rat_trader_amount_rate', 'suspected_insider_hold_rate', 'max_bundler_rate',
    'fresh_wallet_rate', 'sell_tax', 'has_social', 'creator_token_status',
    'dev_team_hold_rate', 'dev_token_burn_ratio', 'sniper_count', 'burn_status',
    'source_mode',
]

MIN_CREATED = getattr(settings, 'GMGN_MIN_CREATED_SECONDS', 1800) or 1800
MAX_CREATED = getattr(settings, 'GMGN_MAX_CREATED_SECONDS', 14400) or 14400

DISCOVERY_GROUPS = [
    {"group_name": "pump_fun", "platforms": ["Pump.fun"]},
    {"group_name": "other_platforms", "platforms": [
        "Moonshot", "moonshot_app", "letsbonk", "memoo",
        "token_mill", "jup_studio", "bags", "believe", "heaven"
    ]},
]

PRIMARY_SLOT = settings.get_discovery_primary_slot()
RESERVE_SLOT = settings.get_discovery_reserve_slot()
FEATURE_SLOTS = settings.get_feature_slots()


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

    def _build_trench_params(self, platforms: Optional[List[str]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            'chain': 'sol',
            'type': 'new_creation',
            'min_created': MIN_CREATED,
            'max_created': MAX_CREATED,
        }
        types = _csv_list(getattr(settings, 'GMGN_TRENCHES_TYPES', ''))
        if types:
            params['type'] = types[0]
        if platforms:
            params['platforms'] = platforms
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

    async def _fetch_trenches_two_group(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        mode = settings.get_provider_mode()
        groups_results: List[Dict[str, Any]] = []
        all_items: List[Dict[str, Any]] = []
        dedup: Dict[Tuple[str, str], int] = {}

        for ginfo in DISCOVERY_GROUPS:
            group_name = ginfo["group_name"]
            platforms = ginfo["platforms"]
            gres: Dict[str, Any] = {
                "group_name": group_name, "platforms": platforms,
                "slot": None, "role": None, "ok": False,
                "raw_count": 0, "unique_count": 0, "duplicate_count": 0,
                "status_code": None, "error": None, "cooldown_until": None,
                "latency_ms": 0,
            }

            t0 = time.perf_counter()
            params = self._build_trench_params(platforms=platforms)

            if mode == ProviderMode.MOCK:
                items = await self.gmgn.fetch_trenches(params)
                gres["ok"] = True
                gres["raw_count"] = len(items)
                gres["latency_ms"] = int((time.perf_counter() - t0) * 1000)
                groups_results.append(gres)
                for item in items:
                    key = (str(item.get("token_mint", "")), str(item.get("pool_address", "")))
                    if key not in dedup:
                        dedup[key] = 0
                        all_items.append(item)
                    dedup[key] += 1
                continue

            rl = get_rate_limiter()
            selected_slot = None

            for try_slot in [PRIMARY_SLOT, RESERVE_SLOT]:
                if not rl.is_slot_cooldown(try_slot):
                    selected_slot = try_slot
                    break

            if selected_slot is None:
                gres["error"] = "both primary and reserve slots in cooldown"
                gres["cooldown_until"] = "N/A"
                groups_results.append(gres)
                await self.repo.append_system_event(
                    'ERROR', 'DISCOVERY', f'Discovery slots cooldown, skip trenches group {group_name}',
                    _json_dumps(gres), account_type='SIM',
                )
                continue

            gres["slot"] = selected_slot
            gres["role"] = "primary" if selected_slot == PRIMARY_SLOT else "reserve"

            try:
                items = await self.gmgn.fetch_trenches(params, credential_slot=selected_slot)
                gres["ok"] = True
                gres["raw_count"] = len(items)
            except Exception as e:
                gres["error"] = str(e)[:300]
                gres["status_code"] = getattr(e, 'status_code', None)
                logger.warning(f"trenches fetch failed for {group_name} slot={selected_slot}: {e}")

            gres["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            groups_results.append(gres)

            if gres["ok"] and items:
                for item in items:
                    key = (str(item.get("token_mint", "")), str(item.get("pool_address", "")))
                    if key not in dedup:
                        dedup[key] = 0
                        all_items.append(item)
                    dedup[key] += 1
            elif selected_slot == RESERVE_SLOT and PRIMARY_SLOT > 0:
                await asyncio.sleep(settings.GMGN_DISCOVERY_GROUP_DELAY_SECONDS)

        for gr in groups_results:
            gr["unique_count"] = gr["raw_count"] - gr.get("duplicate_count", 0)

        raw_total = sum(g.get("raw_count", 0) for g in groups_results)
        unique_total = len(all_items)
        dup_total = raw_total - unique_total

        diag = {
            "mode": "two_group_discovery",
            "raw_fetched_count": raw_total,
            "unique_fetched_count": unique_total,
            "duplicate_count_estimate": dup_total,
            "groups": groups_results,
        }
        return all_items, diag

    async def _run_stage0_risk_filter(
        self, token: Dict[str, Any], groups_for_t: List[dict], now: datetime,
        snapshot_id: int, token_mint: str, pool_address: str,
    ) -> Tuple[List[dict], Dict[int, int], int, int]:
        """Stage 0: Risk filter using only trenches/snapshot data (zero extra API calls)."""
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

                res = await run_risk_filter(token, sg, now)
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
        self, token_mint: str, token: Dict[str, Any], risk_passed_groups: List[dict],
        snapshot_id: int, discovery_event_ids: Dict[int, int],
    ) -> Tuple[List[dict], Dict[str, Any]]:
        """Stage 1: top1 holder check via holders API (weight=5)."""
        stage_diag: Dict[str, Any] = {"candidates_in": len(risk_passed_groups), "checked": 0, "passed": 0, "failed": 0, "skipped": 0}
        passed_groups: List[dict] = []

        if not risk_passed_groups:
            return passed_groups, stage_diag

        feature_slot = FEATURE_SLOTS[0] if FEATURE_SLOTS else 2
        try:
            holders = await self.gmgn.fetch_top_holders(token_mint, limit=20)
        except Exception as e:
            logger.warning(f"top holders fetch failed for {token_mint}: {e}")
            feature_vector = {"error": str(e), "stage": "top_holder_filter"}
            for sg in risk_passed_groups:
                sg_id = int(sg.get('id') or 0)
                config_version = int(sg.get('config_version') or 1)
                discovery_id = discovery_event_ids.get(sg_id)
                try:
                    await self.repo.insert_strategy_match(
                        token_mint, sg_id, config_version, snapshot_id,
                        'top_holder_filter', False,
                        _json_dumps([{"rule": "top1_holder_addr_type0", "passed": False, "reason": f"holders API failed: {e}","missing": True}]),
                        _json_dumps(feature_vector), discovery_event_id=discovery_id,
                    )
                except Exception:
                    pass
            stage_diag["failed"] = len(risk_passed_groups)
            return passed_groups, stage_diag

        if not holders:
            for sg in risk_passed_groups:
                sg_id = int(sg.get('id') or 0)
                feature_vector = {"stage": "top_holder_filter", "holders_count": 0}
                await self.repo.insert_strategy_match(
                    token_mint, sg_id, int(sg.get('config_version') or 1), snapshot_id,
                    'top_holder_filter', False,
                    _json_dumps([{"rule": "top1_holder_addr_type0", "passed": False, "reason": "no holders returned","missing": True}]),
                    _json_dumps(feature_vector), discovery_event_id=discovery_event_ids.get(sg_id),
                )
            stage_diag["failed"] = len(risk_passed_groups)
            return passed_groups, stage_diag

        for sg in risk_passed_groups:
            sg_id = int(sg.get('id') or 0)
            config_version = int(sg.get('config_version') or 1)
            discovery_id = discovery_event_ids.get(sg_id)
            x = float(sg.get("x", 0.2))
            top1_threshold = 0.049 + 0.01 * x

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
                rate = _to_float(_first_present(top1_holder, ["top1_holder_rate", "rate", "amount_percentage", "percentage", "hold_rate"]))

            passed = rate is not None and rate < top1_threshold
            detail = {"rule": "top1_holder_addr_type0", "passed": passed,
                      "value": rate, "threshold": top1_threshold,
                      "missing": rate is None,
                      "reason": f"top1 rate={rate}, threshold={top1_threshold}" if not passed else ""}
            feature_vector = {"top1_holder_rate": rate, "top1_threshold": top1_threshold, "stage": "top_holder_filter"}

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

    async def _run_stage2_price_info(
        self, token_mint: str, token: Dict[str, Any], groups: List[dict],
        snapshot_id: int, discovery_event_ids: Dict[int, int],
    ) -> Tuple[List[dict], Dict[str, Any]]:
        """Stage 2: token info / latest price for swaps & price_change rules (weight=1)."""
        stage_diag: Dict[str, Any] = {"candidates_in": len(groups), "checked": 0, "passed": 0, "failed": 0, "skipped": 0}
        passed_groups: List[dict] = []

        if not groups:
            return passed_groups, stage_diag

        try:
            latest = await self.gmgn.fetch_latest_price(token_mint)
        except Exception as e:
            logger.warning(f"latest price fetch failed for {token_mint}: {e}")
            feature_vector = {"error": str(e), "stage": "price_info"}
            for sg in groups:
                sg_id = int(sg.get('id') or 0)
                await self.repo.insert_strategy_match(
                    token_mint, sg_id, int(sg.get('config_version') or 1), snapshot_id,
                    'price_filter', False,
                    _json_dumps([{"rule": "latest_price_present", "passed": False, "reason": str(e),"missing": True}]),
                    _json_dumps(feature_vector), discovery_event_id=discovery_event_ids.get(sg_id),
                )
            stage_diag["failed"] = len(groups)
            return passed_groups, stage_diag

        if not latest:
            for sg in groups:
                sg_id = int(sg.get('id') or 0)
                await self.repo.insert_strategy_match(
                    token_mint, sg_id, int(sg.get('config_version') or 1), snapshot_id,
                    'price_filter', False,
                    _json_dumps([{"rule": "latest_price_present", "passed": False, "reason": "latest price empty","missing": True}]),
                    _json_dumps({"error": "latest price empty", "stage": "price_info"}),
                    discovery_event_id=discovery_event_ids.get(sg_id),
                )
            stage_diag["failed"] = len(groups)
            return passed_groups, stage_diag

        for sg in groups:
            sg_id = int(sg.get('id') or 0)
            config_version = int(sg.get('config_version') or 1)
            discovery_id = discovery_event_ids.get(sg_id)
            res = await evaluate_price_activity_rules(token, sg, latest, klines=None)
            await self.repo.insert_strategy_match(
                token_mint, sg_id, config_version, snapshot_id,
                'price_filter', res.passed,
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

    async def _run_stage3_kline_fallback(
        self, token_mint: str, token: Dict[str, Any], groups: List[dict],
        snapshot_id: int, discovery_event_ids: Dict[int, int],
    ) -> Tuple[List[dict], Dict[str, Any]]:
        """Stage 3: Kline fallback for young tokens where price_change couldn't be computed."""
        stage_diag: Dict[str, Any] = {"candidates_in": len(groups), "checked": 0, "kline_used": 0, "passed": 0, "failed": 0, "skipped": 0}
        passed_groups: List[dict] = []

        if not groups:
            return passed_groups, stage_diag

        creation_ts, _, age_missing = _parse_creation_ts(token)
        age_minutes = _compute_age_minutes(creation_ts)

        if age_minutes is None or age_minutes >= 60:
            stage_diag["skipped_reason"] = f"age={age_minutes}, no kline needed"
            stage_diag["passed"] = len(groups)
            return groups, stage_diag

        try:
            klines = await self.gmgn.fetch_kline(
                token_mint, "1m", 60,
                from_ts=int(creation_ts) if creation_ts else None,
                to_ts=int(datetime.now(timezone.utc).timestamp()),
            )
            stage_diag["kline_used"] = 1
        except Exception as e:
            logger.warning(f"kline fetch failed for {token_mint}: {e}")
            for sg in groups:
                sg_id = int(sg.get('id') or 0)
                await self.repo.insert_strategy_match(
                    token_mint, sg_id, int(sg.get('config_version') or 1), snapshot_id,
                    'kline_fallback', False,
                    _json_dumps([{"rule": "price_change_1h", "passed": False, "reason": f"kline API failed: {e}","source": "kline_failed", "age_minutes": age_minutes}]),
                    _json_dumps({"error": str(e), "stage": "kline_fallback"}),
                    discovery_event_id=discovery_event_ids.get(sg_id),
                )
            stage_diag["failed"] = len(groups)
            return passed_groups, stage_diag

        for sg in groups:
            sg_id = int(sg.get('id') or 0)
            config_version = int(sg.get('config_version') or 1)
            discovery_id = discovery_event_ids.get(sg_id)
            res = await evaluate_price_activity_rules(token, sg, {}, klines=klines)
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

    async def _run_stage4_smart_degen(
        self, token_mint: str, token: Dict[str, Any], groups: List[dict],
        snapshot_id: int, discovery_event_ids: Dict[int, int],
    ) -> Tuple[List[dict], Dict[str, Any]]:
        """Stage 4: Smart degen holders check (weight=5)."""
        stage_diag: Dict[str, Any] = {"candidates_in": len(groups), "checked": 0, "passed": 0, "failed": 0, "skipped": 0}
        passed_groups: List[dict] = []

        if not groups:
            return passed_groups, stage_diag

        try:
            smart_degen_holders = await self.gmgn.fetch_smart_degen_holders(token_mint, limit=30)
        except Exception as e:
            logger.warning(f"smart degen fetch failed for {token_mint}: {e}")
            for sg in groups:
                sg_id = int(sg.get('id') or 0)
                await self.repo.insert_strategy_match(
                    token_mint, sg_id, int(sg.get('config_version') or 1), snapshot_id,
                    'smart_degen_filter', False,
                    _json_dumps([{"rule": "smart_degen", "passed": False, "reason": f"API failed: {e}","missing": True}]),
                    _json_dumps({"error": str(e), "stage": "smart_degen_filter"}),
                    discovery_event_id=discovery_event_ids.get(sg_id),
                )
            stage_diag["failed"] = len(groups)
            return passed_groups, stage_diag

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

        strategy_groups = await self._load_enabled_strategy_groups()
        if not strategy_groups:
            await self.repo.append_system_event(
                'WARNING', 'DISCOVERY', 'No enabled strategy groups for current runtime mode; skip trench discovery',
                '{}', account_type='SIM'
            )
            self.processed_count = 0
            self.last_elapsed_ms = int((datetime.now(timezone.utc).timestamp() - t0) * 1000)
            return

        for timing_key, groups_for_t in self._group_by_timing(strategy_groups):
            trenches, trench_diag = await self._fetch_trenches_two_group()
            total_fetched = trench_diag.get("unique_fetched_count", 0)

            for token in trenches:
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

                risk_passed, discovery_event_ids, dc, tc = await self._run_stage0_risk_filter(
                    token, groups_for_t, now, snapshot_id, token_mint, pool_address,
                )
                discovered_count += dc
                tracked_count += tc

                stage_diags.setdefault("stage0_risk", {"candidates": len(trenches), "passed_to_stage1": 0})
                stage_diags["stage0_risk"]["passed_to_stage1"] += len(risk_passed)

                top1_passed, top1_diag = await self._run_stage1_top_holder(
                    token_mint, token, risk_passed, snapshot_id, discovery_event_ids,
                )
                stage_diags.setdefault("stage1_top_holder", {"checked": 0, "passed": 0, "failed": 0})
                stage_diags["stage1_top_holder"]["checked"] += top1_diag["checked"]
                stage_diags["stage1_top_holder"]["passed"] += top1_diag["passed"]
                stage_diags["stage1_top_holder"]["failed"] += top1_diag["failed"]

                price_info_passed, price_diag = await self._run_stage2_price_info(
                    token_mint, token, top1_passed, snapshot_id, discovery_event_ids,
                )
                stage_diags.setdefault("stage2_price_info", {"checked": 0, "passed": 0, "failed": 0})
                stage_diags["stage2_price_info"]["checked"] += price_diag["checked"]
                stage_diags["stage2_price_info"]["passed"] += price_diag["passed"]
                stage_diags["stage2_price_info"]["failed"] += price_diag["failed"]

                kline_passed, kline_diag = await self._run_stage3_kline_fallback(
                    token_mint, token, price_info_passed, snapshot_id, discovery_event_ids,
                )
                stage_diags.setdefault("stage3_kline", {"checked": 0, "kline_used": 0, "passed": 0, "failed": 0})
                stage_diags["stage3_kline"]["checked"] += kline_diag["checked"]
                stage_diags["stage3_kline"]["passed"] += kline_diag["passed"]
                stage_diags["stage3_kline"]["failed"] += kline_diag["failed"]
                stage_diags["stage3_kline"]["kline_used"] += kline_diag.get("kline_used", 0)

                final_passed, degen_diag = await self._run_stage4_smart_degen(
                    token_mint, token, kline_passed, snapshot_id, discovery_event_ids,
                )
                stage_diags.setdefault("stage4_smart_degen", {"checked": 0, "passed": 0, "failed": 0})
                stage_diags["stage4_smart_degen"]["checked"] += degen_diag["checked"]
                stage_diags["stage4_smart_degen"]["passed"] += degen_diag["passed"]
                stage_diags["stage4_smart_degen"]["failed"] += degen_diag["failed"]

                if final_passed:
                    try:
                        await self.pipeline.handle_token_second_filter_result(
                            token_mint, final_passed,
                            snapshot_id=snapshot_id,
                            discovery_event_id=discovery_event_ids.get(int(final_passed[0].get('id') or 0)),
                        )
                    except Exception as e:
                        logger.error(f"pipeline entry failed for {token_mint}: {e}")

        self.processed_count = total_fetched
        self.last_elapsed_ms = int((datetime.now(timezone.utc).timestamp() - t0) * 1000)

        run_finished_at = datetime.now(timezone.utc).isoformat()
        context = {
            'count': total_fetched,
            'raw_fetched_count': trench_diag.get("raw_fetched_count", 0),
            'unique_fetched_count': total_fetched,
            'duplicate_count_estimate': trench_diag.get("duplicate_count_estimate", 0),
            'discovered': discovered_count,
            'tracked_initial_passed': tracked_count,
            'dedup_skipped': dedup_skipped,
            'enabled_strategy_groups': len(strategy_groups),
            'elapsed_ms': self.last_elapsed_ms,
            'run_started_at': run_started_at,
            'run_finished_at': run_finished_at,
            'fetch_mode': 'two_group_discovery',
            'trench_groups': trench_diag.get("groups", []),
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
