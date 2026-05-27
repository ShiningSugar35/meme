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
from ..services.event_bus import event_bus
from ..strategy.filters import run_risk_filter, run_price_filter, sort_klines
from ..trading.executor import TradingPipeline

MOCK_MINTS = {'PASS1', 'PASS1_150', 'PASS1_510', 'FAIL_INIT', 'FAIL_SECOND'}

SNAPSHOT_COLUMNS = [
    'type', 'liquidity_usd', 'sol_side_liquidity', 'volume_usd', 'market_cap',
    'price_usd', 'price_sol', 'top_10_holder_rate', 'top1_holder_rate',
    'renounced_mint', 'renounced_freeze_account', 'max_rug_ratio',
    'max_insider_ratio', 'max_entrapment_ratio', 'is_wash_trading',
    'rat_trader_amount_rate', 'suspected_insider_hold_rate', 'max_bundler_rate',
    'fresh_wallet_rate', 'sell_tax', 'has_social', 'creator_token_status',
    'dev_team_hold_rate', 'dev_token_burn_ratio', 'sniper_count', 'source_mode',
]


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
    TRENCH_PLATFORMS = [
        "Pump.fun", "Moonshot", "moonshot_app", "letsbonk", "memoo",
        "token_mill", "jup_studio", "bags", "believe", "heaven"
    ]

    FIXED_MIN_CREATED = 1800
    FIXED_MAX_CREATED = 14400

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
        self._credential_count = self._count_credentials()

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

    def _count_credentials(self) -> int:
        try:
            if hasattr(self.gmgn, 'credentials'):
                return len(self.gmgn.credentials or [])
            if hasattr(self.gmgn, 'api_keys'):
                return len(self.gmgn.api_keys or [])
            if hasattr(self.gmgn, 'client_ids'):
                return len(self.gmgn.client_ids or [])
        except Exception:
            pass
        return 0

    def _build_trench_params(self, platform: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            'chain': 'sol',
            'type': 'new_creation',
            'min_created': self.FIXED_MIN_CREATED,
            'max_created': self.FIXED_MAX_CREATED,
        }

        types = _csv_list(getattr(settings, 'GMGN_TRENCHES_TYPES', ''))
        if types:
            params['type'] = types[0]
            if len(types) > 1:
                params['types'] = types

        if platform:
            params['platforms'] = [platform]
        else:
            platforms = _csv_list(getattr(settings, 'GMGN_TRENCHES_PLATFORMS', ''))
            if platforms:
                params['platforms'] = platforms

        return params

    def _can_use_platform_sharding(self) -> bool:
        return self._credential_count >= 10

    async def _fetch_trenches_by_platforms(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        mode = settings.get_provider_mode()
        if mode == ProviderMode.MOCK:
            params = self._build_trench_params()
            trenches = await self.gmgn.fetch_trenches(params)
            diag: Dict[str, Any] = {
                "mode": "mock_single_request",
                "raw_fetched_count": len(trenches),
                "unique_fetched_count": len(trenches),
                "duplicate_count_estimate": 0,
                "platform_fetch": {"mode": "mock", "platform_count": 0},
            }
            return trenches, diag

        if not self._can_use_platform_sharding():
            logger.warning(f"Credentials={self._credential_count} insufficient for platform sharding; falling back to single request")
            await self.repo.append_system_event(
                'WARNING', 'DISCOVERY',
                f'platform sharding disabled: only {self._credential_count} credentials (need >= 10)',
                json.dumps({"credential_count": self._credential_count}),
                account_type='SIM',
            )
            params = self._build_trench_params()
            trenches = await self.gmgn.fetch_trenches(params)
            diag = {
                "mode": "fallback_single_request",
                "raw_fetched_count": len(trenches),
                "unique_fetched_count": len(trenches),
                "duplicate_count_estimate": 0,
                "platform_fetch": {"mode": "fallback", "platform_count": 0},
            }
            return trenches, diag

        primary_slots = list(range(10))
        PRIMARY_COUNT = 10
        primary_slots = list(range(PRIMARY_COUNT))
        reserve_slots = list(range(PRIMARY_COUNT, min(self._credential_count, 12)))

        async def _fetch_one_platform(idx: int, platform_name: str) -> Dict[str, Any]:
            t0 = time.perf_counter()
            result: Dict[str, Any] = {
                "platform": platform_name,
                "primary_slot": idx,
                "used_slot": idx,
                "used_role": "primary",
                "ok": False,
                "raw_count": 0,
                "unique_count": 0,
                "duplicate_count": 0,
                "fallback_used": False,
                "error": None,
                "latency_ms": 0,
                "items": [],
            }

            params = self._build_trench_params(platform=platform_name)
            provider = self.gmgn

            try:
                items = await provider.fetch_trenches(params, credential_slot=idx)
                result["ok"] = True
                result["raw_count"] = len(items)
                result["items"] = items
            except Exception as e:
                logger.warning(f"primary fetch failed for {platform_name} (slot={idx}): {e}")
                for reserve_slot in reserve_slots:
                    try:
                        items = await provider.fetch_trenches(params, credential_slot=reserve_slot)
                        result["ok"] = True
                        result["raw_count"] = len(items)
                        result["items"] = items
                        result["used_slot"] = reserve_slot
                        result["used_role"] = "reserve"
                        result["fallback_used"] = True
                        result["fallback_from_slot"] = idx
                        logger.info(f"reserve credential {reserve_slot} succeeded for {platform_name}")
                        break
                    except Exception as re:
                        logger.warning(f"reserve credential {reserve_slot} also failed for {platform_name}: {re}")

            result["error"] = str(e) if not result["ok"] else None
            result["latency_ms"] = int((time.perf_counter() - t0) * 1000)
            return result

        tasks = [_fetch_one_platform(i, p) for i, p in enumerate(self.TRENCH_PLATFORMS)]
        platform_results = []
        for i in range(0, len(tasks), 10):
            batch = tasks[i:i+10]
            batch_results = await asyncio.gather(*batch, return_exceptions=True)
            for r in batch_results:
                if isinstance(r, Exception):
                    platform_results.append({"ok": False, "error": str(r), "raw_count": 0, "items": []})
                elif isinstance(r, dict):
                    platform_results.append(r)
                else:
                    platform_results.append({"ok": False, "error": f"unexpected: {r}", "raw_count": 0, "items": []})

        all_items: List[Dict[str, Any]] = []
        dedup_key: Dict[Tuple[str, str], int] = {}
        for pr in platform_results:
            items = pr.get("items", [])
            pr.pop("items", None)
            raw_before = len(items)
            unique_items: List[Dict[str, Any]] = []
            for item in items:
                pool_addr = str(item.get("pool_address") or "")
                token_mint = str(item.get("token_mint") or "")
                key = (token_mint, pool_addr)
                if key not in dedup_key:
                    dedup_key[key] = 0
                    unique_items.append(item)
                dedup_key[key] += 1
            pr["unique_count"] = len(unique_items)
            pr["duplicate_count"] = raw_before - len(unique_items)
            all_items.extend(unique_items)

        raw_fetched_count = sum(p.get("raw_count", 0) for p in platform_results)
        unique_fetched_count = len(all_items)
        duplicate_count_estimate = raw_fetched_count - unique_fetched_count

        diag = {
            "mode": "platform_sharded",
            "raw_fetched_count": raw_fetched_count,
            "unique_fetched_count": unique_fetched_count,
            "duplicate_count_estimate": duplicate_count_estimate,
            "platform_fetch": {
                "mode": "platform_sharded",
                "platform_count": len(self.TRENCH_PLATFORMS),
                "primary_credential_count": PRIMARY_COUNT,
                "reserve_credential_count": len(reserve_slots),
                "items": platform_results,
            },
        }
        return all_items, diag

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

    async def _run_price_screen(
        self,
        token_mint: str,
        token: Dict[str, Any],
        risk_passed_groups: List[dict],
        snapshot_id: int,
        discovery_event_ids: Dict[int, int],
        now: datetime,
    ) -> List[dict]:
        """Fetch latest price + smart degen holders, run new price filter, return strategies that pass."""
        price_error: Optional[str] = None
        holder_error: Optional[str] = None

        try:
            latest = await self.gmgn.fetch_latest_price(token_mint)
        except Exception as e:
            price_error = str(e)
            latest = {}
            logger.warning(f"latest price fetch failed for {token_mint}: {e}")

        if not latest:
            await self.repo.append_system_event(
                'ERROR', 'DISCOVERY', 'GMGN price screen fetch failed',
                _json_dumps({'token': token_mint, 'error': price_error or 'latest price missing', 'stage': 'latest_price'}),
                account_type='SIM',
            )
            return []

        try:
            smart_degen_holders = await self.gmgn.fetch_smart_degen_holders(token_mint, limit=30)
        except Exception as e:
            holder_error = str(e)
            smart_degen_holders = []
            logger.warning(f"smart degen holders fetch failed for {token_mint}: {e}")

        from ..strategy.filters import _parse_creation_ts, _compute_age_minutes
        creation_ts, _creation_source, age_missing = _parse_creation_ts(token)
        age_minutes = _compute_age_minutes(creation_ts)
        klines: Optional[List[Dict[str, Any]]] = None
        if age_minutes is not None and age_minutes < 60 and creation_ts is not None:
            try:
                klines = await self.gmgn.fetch_kline(
                    token_mint, "1m", 60,
                    from_ts=int(creation_ts), to_ts=int(datetime.now(timezone.utc).timestamp()),
                )
            except Exception as e:
                logger.warning(f"kline fetch failed for {token_mint}: {e}")

        price_passed: List[dict] = []
        for sg in risk_passed_groups:
            sg_id = int(sg.get('id') or 0)
            config_version = int(sg.get('config_version') or 1)
            discovery_id = discovery_event_ids.get(sg_id)
            try:
                res = await run_price_filter(token, sg, latest, smart_degen_holders, klines=klines)
                await self.repo.insert_strategy_match(
                    token_mint, sg_id, config_version, snapshot_id,
                    'price_filter', res.passed,
                    _json_dumps(res.details), _json_dumps(res.feature_vector),
                    discovery_event_id=discovery_id,
                )
                if res.passed:
                    price_passed.append(sg)
            except Exception as e:
                logger.error(f"price filter exception for {token_mint} strategy {sg_id}: {e}")

        return price_passed

    async def run_once(self):
        now = datetime.now(timezone.utc)
        t0 = now.timestamp()
        discovered_count = 0
        tracked_count = 0
        dedup_skipped = 0
        total_fetched = 0
        raw_fetched_count = 0
        duplicate_count_estimate = 0
        platform_fetch_meta: Dict[str, Any] = {}
        mode = settings.get_provider_mode()

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
            try:
                trenches, trench_diag = await self._fetch_trenches_by_platforms()
                raw_fetched_count = trench_diag.get("raw_fetched_count", 0)
                total_fetched = trench_diag.get("unique_fetched_count", len(trenches))
                duplicate_count_estimate = trench_diag.get("duplicate_count_estimate", 0)
                platform_fetch_meta = trench_diag.get("platform_fetch", {})
            except Exception as e:
                logger.error(f"fetch_trenches failed: {e}")
                await self.repo.append_system_event(
                    'ERROR', 'DISCOVERY', 'GMGN fetch_trenches failed',
                    _json_dumps({'error': str(e)}),
                    account_type='SIM',
                )
                await event_bus.publish('system', {
                    'level': 'ERROR', 'category': 'DISCOVERY',
                    'message': 'fetch_trenches failed'
                })
                continue

            for token in trenches:
                token_mint = token.get('token_mint')
                if not token_mint:
                    await self.repo.append_system_event(
                        'WARNING', 'DISCOVERY', 'skip trench without token_mint',
                        _json_dumps({'token': token}), account_type='SIM'
                    )
                    continue

                source_mode = token.get('source_mode', 'MOCK')
                if mode != ProviderMode.MOCK and source_mode == 'MOCK' and token_mint in MOCK_MINTS:
                    continue

                try:
                    token_mint, pool_address, pool_created_at, snapshot_id = await self._store_snapshot_and_token(token, now)
                except Exception as e:
                    logger.error(f"store discovery snapshot failed token={token_mint}: {e}")
                    await self.repo.append_system_event(
                        'ERROR', 'DISCOVERY', 'store discovery snapshot failed',
                        _json_dumps({'token': token_mint, 'error': str(e)}),
                        account_type='SIM',
                    )
                    continue

                risk_passed: List[dict] = []
                discovery_event_ids: Dict[int, int] = {}

                for sg in groups_for_t:
                    sg_id = int(sg.get('id') or 0)
                    config_version = int(sg.get('config_version') or 1)
                    try:
                        existing = await self.repo.get_discovery_event_by_snapshot_token_pool(
                            snapshot_id, token_mint, pool_address, strategy_id=sg_id
                        )
                        if existing:
                            dedup_skipped += 1
                            continue

                        res = await run_risk_filter(token, sg, now)
                        status = 'RISK_PASSED' if res.passed else 'RISK_FAILED'

                        discovery_id, created = await self.repo.create_discovery_event_idempotent(
                            token_mint=token_mint,
                            pool_address=pool_address,
                            pool_created_at=pool_created_at,
                            snapshot_id=snapshot_id,
                            strategy_id=sg_id,
                            strategy_config_version=config_version,
                            status=status,
                            feature_vector_json=_json_dumps(res.feature_vector),
                        )
                        if not created:
                            dedup_skipped += 1
                            continue

                        match_id = await self.repo.insert_strategy_match(
                            token_mint, sg_id, config_version, snapshot_id,
                            'risk_filter', res.passed,
                            _json_dumps([d.__dict__ for d in res.details]),
                            _json_dumps(res.feature_vector),
                            discovery_event_id=discovery_id,
                        )

                        await self.repo.update_discovery_event_status(
                            discovery_id, status,
                            initial_match_id=match_id,
                            feature_vector_json=_json_dumps(res.feature_vector),
                        )

                        discovered_count += 1
                        if res.passed:
                            risk_passed.append(sg)
                            discovery_event_ids[sg_id] = discovery_id
                            tracked_count += 1

                        await event_bus.publish('discovery', {
                            'token_mint': token_mint,
                            'discovery_event_id': discovery_id,
                            'strategy_id': sg_id,
                            'status': status,
                            'passed_strategy_ids': [sg_id] if res.passed else [],
                            'failed_strategy_ids': [] if res.passed else [sg_id],
                        })
                    except Exception as e:
                        logger.error(f"Risk filter exception for {token_mint} strategy {sg_id}: {e}")
                        await self.repo.append_system_event(
                            'ERROR', 'DISCOVERY', 'risk filter exception',
                            _json_dumps({'token': token_mint, 'strategy_id': sg_id, 'error': str(e)}),
                            account_type='SIM',
                        )

                if risk_passed:
                    try:
                        passed_strategies = await self._run_price_screen(
                            token_mint, token, risk_passed, snapshot_id,
                            discovery_event_ids, now,
                        )
                        if passed_strategies:
                            await self.pipeline.handle_token_second_filter_result(
                                token_mint, passed_strategies,
                                snapshot_id=snapshot_id, discovery_event_id=discovery_event_ids.get(
                                    int(passed_strategies[0].get('id') or 0)
                                ),
                            )
                    except Exception as e:
                        logger.error(f"price screen entry failed for {token_mint}: {e}")

        self.processed_count = total_fetched
        self.last_elapsed_ms = int((datetime.now(timezone.utc).timestamp() - t0) * 1000)

        context = {
            'count': total_fetched,
            'raw_fetched_count': raw_fetched_count,
            'unique_fetched_count': total_fetched,
            'duplicate_count_estimate': duplicate_count_estimate,
            'discovered': discovered_count,
            'tracked_initial_passed': tracked_count,
            'dedup_skipped': dedup_skipped,
            'enabled_strategy_groups': len(strategy_groups),
            'elapsed_ms': self.last_elapsed_ms,
        }
        if platform_fetch_meta:
            context['platform_fetch'] = platform_fetch_meta

        await self.repo.append_system_event(
            'INFO', 'DISCOVERY', 'Discovery run complete',
            _json_dumps(context),
            account_type='SIM',
        )
        await event_bus.publish('system', {
            'level': 'INFO', 'category': 'DISCOVERY',
            'message': f'Discovered {discovered_count} strategy-token events, tracked {tracked_count}, skipped {dedup_skipped} duplicates',
        })
