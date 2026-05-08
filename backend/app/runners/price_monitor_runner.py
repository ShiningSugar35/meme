from ..db.repositories import Repositories
from ..providers.base import MarketDataProvider
from datetime import datetime, timezone


class PriceMonitorRunner:
    def __init__(self, repo: Repositories, gmgn: MarketDataProvider):
        self.repo = repo
        self.gmgn = gmgn

    async def run_once(self):
        positions = await self.repo.list_open_positions()
        for p in positions:
            token = p['token_mint']
            latest = await self.gmgn.fetch_latest_price(token)
            await self.repo.insert_tick_snapshot(token, 'GMGN', datetime.now(timezone.utc).isoformat(), latest.get('price'), latest.get('price_sol'), latest.get('liquidity_usd', 0), latest.get('sol_side_liquidity'), latest.get('market_cap', 0), str(latest))
