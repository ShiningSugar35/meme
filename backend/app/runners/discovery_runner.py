from ..db.repositories import Repositories
from ..providers.base import MarketDataProvider
from ..strategy.filters import run_initial_filter
from datetime import datetime, timezone
from typing import List
from ..logging_config import logger


class DiscoveryRunner:
    def __init__(self, repo: Repositories, gmgn: MarketDataProvider, strategy_groups: List[dict]):
        self.repo = repo
        self.gmgn = gmgn
        self.strategy_groups = strategy_groups

    async def run_once(self):
        now = datetime.now(timezone.utc)
        try:
            trenches = await self.gmgn.fetch_trenches({})
        except Exception as e:
            await self.repo.append_system_event('ERROR', 'DISCOVERY', 'GMGN fetch_trenches failed', str({'error': str(e)}))
            return

        for token in trenches:
            token_mint = token.get('token_mint')
            # upsert token first seen
            await self.repo.upsert_token_first_seen(token_mint, symbol=token.get('symbol'), pool_created_at=token.get('pool_created_at'))
            # insert metric snapshot
            await self.repo.insert_token_metric_snapshot(token_mint, now.isoformat(), str(token), **{k: token.get(k) for k in ['liquidity_usd', 'sol_side_liquidity', 'price_usd', 'price_sol', 'top_10_holder_rate']})

            # run initial filters for enabled strategy groups
            for sg in self.strategy_groups:
                res = await run_initial_filter(token, sg, now)
                await self.repo.insert_strategy_match(token_mint, sg.get('id', 0), sg.get('config_version', 1), None, 'initial_filter', res.passed, str([d.__dict__ for d in res.details]), str(res.feature_vector))
                if res.passed:
                    # mark as WAIT_SECOND_FILTER by updating token latest_state
                    await self.repo.update_token_latest_snapshot(token_mint, None)
        await self.repo.append_system_event('INFO', 'DISCOVERY', 'Discovery run complete', str({'count': len(trenches)}))
