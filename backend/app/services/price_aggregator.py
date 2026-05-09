from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from ..db.repositories import Repositories
from ..providers.base import MarketDataProvider, SwapProvider
from ..providers.gmgn_subscriber import GMGNSubscriberBase, SubscribedTick
from ..config import settings
from ..logging_config import logger


class PriceAggregator:
    """
    Price aggregator with 3-tier fallback:
    1. GMGN WebSocket subscription (fastest, most accurate)
    2. GMGN latest price endpoint (HTTP polling)
    3. Jupiter quote as fallback (last resort, marked as JUPITER_QUOTE_FALLBACK)
    """

    def __init__(self, repo: Repositories, gmgn: MarketDataProvider, jupiter: SwapProvider, subscriber: GMGNSubscriberBase):
        self.repo = repo
        self.gmgn = gmgn
        self.jupiter = jupiter
        self.subscriber = subscriber

    async def get_price(self, token_mint: str, subscribe: bool = True) -> Dict[str, Any]:
        tick = await self._try_subscription(token_mint)
        if tick:
            await self._log_tick(token_mint, 'GMGN_SUBSCRIPTION', tick)
            return {
                'price': tick.price_usd,
                'price_sol': tick.price_sol,
                'liquidity_usd': tick.liquidity_usd,
                'sol_side_liquidity': tick.sol_side_liquidity,
                'market_cap': tick.market_cap,
                'source': 'GMGN_SUBSCRIPTION',
                'observed_at': tick.observed_at,
            }

        latest = await self._try_gmgn_latest(token_mint)
        if latest:
            await self._log_tick(token_mint, 'GMGN_LATEST', None, latest.get('price'), latest.get('price_sol'))
            return {
                'price': latest.get('price'),
                'price_sol': latest.get('price_sol'),
                'liquidity_usd': latest.get('liquidity_usd', 0),
                'sol_side_liquidity': latest.get('sol_side_liquidity', 0),
                'market_cap': latest.get('market_cap', 0),
                'source': 'GMGN_LATEST',
                'observed_at': datetime.now(timezone.utc).isoformat(),
            }

        fallback = await self._try_jupiter_fallback(token_mint)
        if fallback:
            await self._log_tick(token_mint, 'JUPITER_QUOTE_FALLBACK', None, fallback.get('price'), fallback.get('price_sol'))
            return {
                'price': fallback.get('price'),
                'price_sol': fallback.get('price_sol'),
                'liquidity_usd': 0,
                'sol_side_liquidity': 0,
                'market_cap': 0,
                'source': 'JUPITER_QUOTE_FALLBACK',
                'observed_at': datetime.now(timezone.utc).isoformat(),
            }

        return None

    async def get_prices_batch(self, token_mints: List[str]) -> Dict[str, Dict[str, Any]]:
        results = {}
        for mint in token_mints:
            try:
                results[mint] = await self.get_price(mint)
            except Exception as e:
                await self.repo.append_system_event('ERROR', 'PRICE', f'PriceAggregator failed for {mint}', str({'error': str(e)}))
                results[mint] = None
        return results

    async def _try_subscription(self, token_mint: str) -> Optional[SubscribedTick]:
        try:
            return await self.subscriber.get_latest(token_mint)
        except Exception as e:
            logger.debug("GMGN subscription miss for %s: %s", token_mint, str(e))
            return None

    async def _try_gmgn_latest(self, token_mint: str) -> Optional[Dict[str, Any]]:
        try:
            return await self.gmgn.fetch_latest_price(token_mint)
        except Exception as e:
            logger.debug("GMGN latest miss for %s: %s", token_mint, str(e))
            return None

    async def _try_jupiter_fallback(self, token_mint: str) -> Optional[Dict[str, Any]]:
        try:
            WRAPPED_SOL_MINT = 'So11111111111111111111111111111111111111112'
            quote = await self.jupiter.quote_exact_in(
                input_mint=WRAPPED_SOL_MINT,
                output_mint=token_mint,
                amount=1_000_000_000,  # 1 SOL in lamports
                slippage_bps=100  # 1%
            )
            if quote and quote.get('outAmount'):
                out_amount = int(quote['outAmount'])
                price_sol = 1_000_000_000 / out_amount
                price_usd = price_sol * 150.0  # approximate SOL/USD rate
                return {
                    'price': price_usd,
                    'price_sol': price_sol,
                }
        except Exception as e:
            logger.debug("Jupiter fallback miss for %s: %s", token_mint, str(e))
        return None

    async def _log_tick(self, token_mint: str, source: str, tick: Optional[SubscribedTick] = None, price_usd: Optional[float] = None, price_sol: Optional[float] = None):
        try:
            observed_at = tick.observed_at if tick else datetime.now(timezone.utc).isoformat()
            p_usd = tick.price_usd if tick else (price_usd or 0)
            p_sol = tick.price_sol if tick else (price_sol or 0)
            liq = tick.liquidity_usd if tick else 0
            sol_liq = tick.sol_side_liquidity if tick else 0
            mcap = tick.market_cap if tick else 0

            await self.repo.insert_tick_snapshot(
                token_mint, source, observed_at,
                p_usd, p_sol, liq, sol_liq, mcap,
                str({'source': source})
            )
        except Exception as e:
            logger.debug("Failed to log tick for %s: %s", token_mint, str(e))
