import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from ..config import ProviderMode, settings
from ..db.repositories import Repositories
from ..logging_config import logger
from ..providers.base import MarketDataProvider
from ..services.event_bus import event_bus
from ..strategy.filters import run_initial_filter

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


def _csv_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value).split(',') if x.strip()]


class DiscoveryRunner:
    def __init__(self, repo: Repositories, gmgn: MarketDataProvider, strategy_groups: List[dict]):
        self.repo = repo
        self.gmgn = gmgn
        self.strategy_groups = strategy_groups or []
        self.processed_count = 0
        self.last_elapsed_ms = 0

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

    def _build_trench_params(self, t_seconds: int) -> Dict[str, Any]:
        """Build provider query params from runtime strategy timing.

        No ``limit`` is sent.  The GMGN trenches API decides how many rows are
        returned, matching the requirement that GMGN_TRENCHES_LIMIT is no longer
        used anywhere in the system.
        """
        params: Dict[str, Any] = {
            'chain': 'sol',
            'type': 'new_creation',
            'min_created': t_seconds,
            'max_created': t_seconds + 60,
        }

        types = _csv_list(getattr(settings, 'GMGN_TRENCHES_TYPES', ''))
        if types:
            params['type'] = types[0]
            if len(types) > 1:
                params['types'] = types

        platforms = _csv_list(getattr(settings, 'GMGN_TRENCHES_PLATFORMS', ''))
        if platforms:
            params['platforms'] = platforms

        return params

    @staticmethod
    def _group_by_t_seconds(strategy_groups: List[dict]) -> List[Tuple[int, List[dict]]]:
        grouped: Dict[int, List[dict]] = defaultdict(list)
        for sg in strategy_groups:
            t_seconds = _as_int(sg.get('t_seconds'), 150)
            grouped[t_seconds].append(sg)
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

    async def run_once(self):
        now = datetime.now(timezone.utc)
        t0 = now.timestamp()
        discovered_count = 0
        tracked_count = 0
        dedup_skipped = 0
        total_fetched = 0
        mode = settings.get_provider_mode()

        strategy_groups = await self._load_enabled_strategy_groups()
        if not strategy_groups:
            await self.repo.append_system_event(
                'WARNING', 'DISCOVERY', 'No enabled strategy groups for current runtime mode; skip trench discovery', '{}', account_type='SIM'
            )
            self.processed_count = 0
            self.last_elapsed_ms = int((datetime.now(timezone.utc).timestamp() - t0) * 1000)
            return

        for t_seconds, groups_for_t in self._group_by_t_seconds(strategy_groups):
            params = self._build_trench_params(t_seconds)
            try:
                trenches = await self.gmgn.fetch_trenches(params)
            except Exception as e:
                await self.repo.append_system_event(
                    'ERROR', 'DISCOVERY', 'GMGN fetch_trenches failed',
                    _json_dumps({'t_seconds': t_seconds, 'params': params, 'error': str(e)}),
                    account_type='SIM',
                )
                await event_bus.publish('system', {
                    'level': 'ERROR', 'category': 'DISCOVERY', 'message': f'fetch_trenches failed for t={t_seconds}'
                })
                continue

            total_fetched += len(trenches)

            for token in trenches:
                token_mint = token.get('token_mint')
                if not token_mint:
                    await self.repo.append_system_event(
                        'WARNING', 'DISCOVERY', 'skip trench without token_mint',
                        _json_dumps({'token': token, 't_seconds': t_seconds}), account_type='SIM'
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
                        _json_dumps({'token': token_mint, 't_seconds': t_seconds, 'error': str(e)}),
                        account_type='SIM',
                    )
                    continue

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

                        res = await run_initial_filter(token, sg, now)
                        next_second_check_at = (now + timedelta(seconds=60)).isoformat() if res.passed else None
                        status = 'INITIAL_FILTER_PASSED' if res.passed else 'INITIAL_FILTER_FAILED'

                        discovery_id, created = await self.repo.create_discovery_event_idempotent(
                            token_mint=token_mint,
                            pool_address=pool_address,
                            pool_created_at=pool_created_at,
                            t_seconds=t_seconds,
                            snapshot_id=snapshot_id,
                            strategy_id=sg_id,
                            strategy_config_version=config_version,
                            status=status,
                            next_second_check_at=next_second_check_at,
                            feature_vector_json=_json_dumps(res.feature_vector),
                        )
                        if not created:
                            dedup_skipped += 1
                            continue

                        match_id = await self.repo.insert_strategy_match(
                            token_mint,
                            sg_id,
                            config_version,
                            snapshot_id,
                            'initial_filter',
                            res.passed,
                            _json_dumps([d.__dict__ for d in res.details]),
                            _json_dumps(res.feature_vector),
                            discovery_event_id=discovery_id,
                        )

                        await self.repo.update_discovery_event_status(
                            discovery_id,
                            status,
                            initial_match_id=match_id,
                            next_second_check_at=next_second_check_at,
                            feature_vector_json=_json_dumps(res.feature_vector),
                            t_seconds=t_seconds,
                        )

                        discovered_count += 1
                        if res.passed:
                            tracked_count += 1

                        await event_bus.publish('discovery', {
                            'token_mint': token_mint,
                            'discovery_event_id': discovery_id,
                            'strategy_id': sg_id,
                            't_seconds': t_seconds,
                            'status': status,
                            'passed_strategy_ids': [sg_id] if res.passed else [],
                            'failed_strategy_ids': [] if res.passed else [sg_id],
                        })
                    except Exception as e:
                        logger.error(f"Initial filter exception for {token_mint} strategy {sg_id}: {e}")
                        await self.repo.append_system_event(
                            'ERROR', 'DISCOVERY', 'initial filter exception',
                            _json_dumps({'token': token_mint, 'strategy_id': sg_id, 't_seconds': t_seconds, 'error': str(e)}),
                            account_type='SIM',
                        )

        self.processed_count = total_fetched
        self.last_elapsed_ms = int((datetime.now(timezone.utc).timestamp() - t0) * 1000)

        await self.repo.append_system_event(
            'INFO', 'DISCOVERY', 'Discovery run complete',
            _json_dumps({
                'count': total_fetched,
                'discovered': discovered_count,
                'tracked_initial_passed': tracked_count,
                'dedup_skipped': dedup_skipped,
                'enabled_strategy_groups': len(strategy_groups),
                'elapsed_ms': self.last_elapsed_ms,
            }),
            account_type='SIM',
        )
        await event_bus.publish('system', {
            'level': 'INFO',
            'category': 'DISCOVERY',
            'message': f'Discovered {discovered_count} strategy-token events, tracked {tracked_count}, skipped {dedup_skipped} duplicates',
        })
