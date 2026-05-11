from ..db.repositories import Repositories
from ..providers.base import MarketDataProvider
from ..strategy.filters import run_initial_filter
from ..services.event_bus import event_bus
from ..config import settings, ProviderMode
from datetime import datetime, timezone
from typing import List, Optional
from ..logging_config import logger

MOCK_MINTS = {'PASS1', 'PASS1_150', 'PASS1_510', 'FAIL_INIT', 'FAIL_SECOND'}


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
        dedup_skipped = 0
        mode = settings.get_provider_mode()

        try:
            trenches = await self.gmgn.fetch_trenches({})
        except Exception as e:
            await self.repo.append_system_event('ERROR', 'DISCOVERY', 'GMGN fetch_trenches failed',
                str({'error': str(e)}), account_type='SIM')
            await event_bus.publish('system', {'level': 'ERROR', 'category': 'DISCOVERY',
                'message': 'fetch_trenches failed'})
            return

        for token in trenches:
            token_mint = token.get('token_mint')
            source_mode = token.get('source_mode', 'MOCK')

            # Skip mock tokens in non-mock mode
            if mode != ProviderMode.MOCK and source_mode == 'MOCK' and token_mint in MOCK_MINTS:
                continue

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
                latest_state=token.get('type', 'discovered'),
            )
            await self.repo.insert_token_metric_snapshot(
                token_mint, now.isoformat(), str(token),
                liquidity_usd=token.get('liquidity_usd'),
                sol_side_liquidity=token.get('sol_side_liquidity'),
                price_usd=token.get('price_usd'),
                price_sol=token.get('price_sol'),
                top_10_holder_rate=token.get('top_10_holder_rate'),
                volume_usd=token.get('volume_usd'),
                market_cap=token.get('market_cap'),
                source_mode=source_mode,
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

        self.processed_count = len(trenches)
        self.last_elapsed_ms = int((datetime.now(timezone.utc).timestamp() - t0) * 1000)

        await self.repo.append_system_event(
            'INFO', 'DISCOVERY', 'Discovery run complete',
            str({'count': len(trenches), 'discovered': discovered_count, 'dedup_skipped': dedup_skipped,
                 'elapsed_ms': self.last_elapsed_ms}),
            account_type='SIM'
        )
        await event_bus.publish('system', {
            'level': 'INFO', 'category': 'DISCOVERY',
            'message': f'Discovered {discovered_count} tokens, skipped {dedup_skipped} duplicates'
        })
