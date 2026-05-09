from ..db.repositories import Repositories
from ..providers.base import MarketDataProvider
from ..strategy.filters import run_initial_filter
from ..services.event_bus import event_bus
from datetime import datetime, timezone
from typing import List, Optional
from ..logging_config import logger


class DiscoveryRunner:
    def __init__(self, repo: Repositories, gmgn: MarketDataProvider, strategy_groups: List[dict]):
        self.repo = repo
        self.gmgn = gmgn
        self.strategy_groups = strategy_groups

    async def run_once(self):
        now = datetime.now(timezone.utc)
        discovered_count = 0
        dedup_skipped = 0
        try:
            trenches = await self.gmgn.fetch_trenches({})
        except Exception as e:
            await self.repo.append_system_event('ERROR', 'DISCOVERY', 'GMGN fetch_trenches failed', str({'error': str(e)}))
            await event_bus.publish('system', {'level': 'ERROR', 'category': 'DISCOVERY', 'message': 'fetch_trenches failed'})
            return

        snapshot_id_map: dict = {}
        for token in trenches:
            token_mint = token.get('token_mint')
            snapshot_id = token.get('snapshot_id')
            pool_address = token.get('pool_address', '')
            pool_created_at = token.get('pool_created_at')

            if snapshot_id is not None:
                existing = await self.repo.get_discovery_event_by_snapshot_token_pool(
                    snapshot_id, token_mint, pool_address or None
                )
                if existing:
                    dedup_skipped += 1
                    continue

            first_seen_at = token.get('first_seen_at') or now.isoformat()
            await self.repo.upsert_token_first_seen(
                token_mint,
                symbol=token.get('symbol'),
                pool_address=pool_address,
                pool_created_at=pool_created_at,
            )
            await self.repo.insert_token_metric_snapshot(
                token_mint, now.isoformat(), str(token),
                **{k: token.get(k) for k in [
                    'liquidity_usd', 'sol_side_liquidity', 'price_usd', 'price_sol',
                    'top_10_holder_rate', 'volume_usd', 'market_cap'
                ]}
            )

            discovery_id = await self.repo.create_discovery_event(
                token_mint, pool_address=pool_address,
                pool_created_at=pool_created_at,
                source_snapshot_id=snapshot_id
            )

            for sg in self.strategy_groups:
                res = await run_initial_filter(token, sg, now)
                await self.repo.insert_strategy_match(
                    token_mint, sg.get('id', 0), sg.get('config_version', 1),
                    snapshot_id, 'initial_filter', res.passed,
                    str([d.__dict__ for d in res.details]), str(res.feature_vector)
                )
                if res.passed:
                    await self.repo.update_token_latest_snapshot(token_mint, snapshot_id)

            discovered_count += 1

        await self.repo.append_system_event(
            'INFO', 'DISCOVERY', 'Discovery run complete',
            str({'count': len(trenches), 'discovered': discovered_count, 'dedup_skipped': dedup_skipped})
        )
        await event_bus.publish('system', {
            'level': 'INFO', 'category': 'DISCOVERY',
            'message': f'Discovered {discovered_count} tokens, skipped {dedup_skipped} duplicates'
        })
