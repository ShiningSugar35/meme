import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from ..config import ProviderMode, settings
from ..db.repositories import Repositories
from ..logging_config import logger
from ..providers.base import MarketDataProvider
from ..services.event_bus import event_bus
from ..strategy.filters import run_initial_filter

MOCK_MINTS = {'PASS1', 'PASS1_150', 'PASS1_510', 'FAIL_INIT', 'FAIL_SECOND'}


SNAPSHOT_COLUMNS = [
    'type',
    'liquidity_usd',
    'sol_side_liquidity',
    'volume_usd',
    'market_cap',
    'price_usd',
    'price_sol',
    'top_10_holder_rate',
    'top1_holder_rate',
    'renounced_mint',
    'renounced_freeze_account',
    'max_rug_ratio',
    'max_insider_ratio',
    'max_entrapment_ratio',
    'is_wash_trading',
    'rat_trader_amount_rate',
    'suspected_insider_hold_rate',
    'max_bundler_rate',
    'fresh_wallet_rate',
    'sell_tax',
    'has_social',
    'has_at_least_one_social',
    'creator_token_status',
    'burn_status',
    'dev_team_hold_rate',
    'dev_token_burn_ratio',
    'sniper_count',
    'source_mode',
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


class DiscoveryRunner:
    def __init__(self, repo: Repositories, gmgn: MarketDataProvider, strategy_groups: List[dict]):
        self.repo = repo
        self.gmgn = gmgn
        self.strategy_groups = strategy_groups
        self.processed_count = 0
        self.last_elapsed_ms = 0

    async def run_once(self):
        now = datetime.now(timezone.utc)
        t0 = now.timestamp()
        discovered_count = 0
        tracked_count = 0
        dedup_skipped = 0
        mode = settings.get_provider_mode()

        try:
            trenches = await self.gmgn.fetch_trenches({})
        except Exception as e:
            await self.repo.append_system_event(
                'ERROR', 'DISCOVERY', 'GMGN fetch_trenches failed',
                _json_dumps({'error': str(e)}), account_type='SIM'
            )
            await event_bus.publish('system', {
                'level': 'ERROR', 'category': 'DISCOVERY', 'message': 'fetch_trenches failed'
            })
            return

        for token in trenches:
            token_mint = token.get('token_mint')
            if not token_mint:
                await self.repo.append_system_event(
                    'WARNING', 'DISCOVERY', 'skip trench without token_mint',
                    _json_dumps({'token': token}), account_type='SIM'
                )
                continue

            source_mode = token.get('source_mode', 'MOCK')

            # 防止 online/live 模式误拿 mock mint 去打真实 GMGN 查询。
            if mode != ProviderMode.MOCK and source_mode == 'MOCK' and token_mint in MOCK_MINTS:
                continue

            snapshot_id = token.get('snapshot_id')
            pool_address = token.get('pool_address') or ''
            pool_created_at = token.get('pool_created_at')

            if snapshot_id is not None:
                existing = await self.repo.get_discovery_event_by_snapshot_token_pool(
                    snapshot_id, token_mint, pool_address
                )
                if existing:
                    dedup_skipped += 1
                    continue

            await self.repo.upsert_token_first_seen(
                token_mint,
                symbol=token.get('symbol'),
                name=token.get('name'),
                pool_address=pool_address,
                launchpad=_first_present(token, 'launchpad', 'platform'),
                pool_created_at=pool_created_at,
                latest_state=token.get('type', 'discovered'),
            )

            await self.repo.insert_token_metric_snapshot(
                token_mint,
                now.isoformat(),
                _json_dumps(token),
                **_snapshot_kwargs(token),
            )

            # tokens 表记录“最新快照”，不再只在初筛通过时才更新。
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

            discovery_id, created = await self.repo.create_discovery_event_idempotent(
                token_mint=token_mint,
                pool_address=pool_address,
                pool_created_at=pool_created_at,
                t_seconds=None,
                snapshot_id=snapshot_id,
            )
            if not created:
                dedup_skipped += 1
                continue

            passed_any_strategy = False
            passed_strategy_ids: List[int] = []
            failed_strategy_ids: List[int] = []

            for sg in self.strategy_groups:
                try:
                    res = await run_initial_filter(token, sg, now)
                    await self.repo.insert_strategy_match(
                        token_mint,
                        sg.get('id', 0),
                        sg.get('config_version', 1),
                        snapshot_id,
                        'initial_filter',
                        res.passed,
                        _json_dumps([d.__dict__ for d in res.details]),
                        _json_dumps(res.feature_vector),
                        discovery_event_id=discovery_id,
                    )
                    if res.passed:
                        passed_any_strategy = True
                        passed_strategy_ids.append(sg.get('id', 0))
                    else:
                        failed_strategy_ids.append(sg.get('id', 0))
                except Exception as e:
                    failed_strategy_ids.append(sg.get('id', 0))
                    logger.error(f"Initial filter exception for {token_mint} strategy {sg.get('id')}: {e}")
                    await self.repo.append_system_event(
                        'ERROR', 'DISCOVERY', 'initial filter exception',
                        _json_dumps({'token': token_mint, 'strategy_id': sg.get('id'), 'error': str(e)}),
                        account_type='SIM'
                    )

            if passed_any_strategy:
                tracked_count += 1
                await self.repo.update_discovery_event_status(discovery_id, 'INITIAL_PASSED')
            else:
                await self.repo.update_discovery_event_status(discovery_id, 'INITIAL_FAILED')

            discovered_count += 1
            await event_bus.publish('discovery', {
                'token_mint': token_mint,
                'discovery_event_id': discovery_id,
                'status': 'INITIAL_PASSED' if passed_any_strategy else 'INITIAL_FAILED',
                'passed_strategy_ids': passed_strategy_ids,
                'failed_strategy_ids': failed_strategy_ids,
            })

        self.processed_count = len(trenches)
        self.last_elapsed_ms = int((datetime.now(timezone.utc).timestamp() - t0) * 1000)

        await self.repo.append_system_event(
            'INFO', 'DISCOVERY', 'Discovery run complete',
            _json_dumps({
                'count': len(trenches),
                'discovered': discovered_count,
                'tracked_initial_passed': tracked_count,
                'dedup_skipped': dedup_skipped,
                'elapsed_ms': self.last_elapsed_ms,
            }),
            account_type='SIM',
        )
        await event_bus.publish('system', {
            'level': 'INFO',
            'category': 'DISCOVERY',
            'message': f'Discovered {discovered_count} tokens, tracked {tracked_count}, skipped {dedup_skipped} duplicates',
        })
