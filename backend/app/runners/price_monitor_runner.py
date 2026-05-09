from ..db.repositories import Repositories
from ..services.price_aggregator import PriceAggregator
from ..services.event_bus import event_bus
from datetime import datetime, timezone
from ..logging_config import logger


class PriceMonitorRunner:
    def __init__(self, repo: Repositories, price_aggregator: PriceAggregator):
        self.repo = repo
        self.aggregator = price_aggregator

    async def run_once(self):
        positions = await self.repo.list_open_positions()
        now = datetime.now(timezone.utc).isoformat()
        for p in positions:
            token = p['token_mint']
            try:
                result = await self.aggregator.get_price(token)
                if result:
                    await self.repo.insert_tick_snapshot(
                        token, result.get('source', 'UNKNOWN'), result.get('observed_at', now),
                        result.get('price', 0), result.get('price_sol', 0),
                        result.get('liquidity_usd', 0), result.get('sol_side_liquidity', 0),
                        result.get('market_cap', 0), str(result)
                    )
            except Exception as e:
                await self.repo.append_system_event('ERROR', 'PRICE', f'PriceMonitorRunner failed for {token}', str({'error': str(e)}))
                await event_bus.publish('system', {'level': 'ERROR', 'category': 'PRICE', 'message': f'Tick failed for {token}'})
