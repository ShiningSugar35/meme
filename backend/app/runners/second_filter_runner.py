from ..db.repositories import Repositories
from ..providers.base import MarketDataProvider, SwapProvider, ExecutionProvider, RpcProvider
from ..strategy.second_filter import run_second_filter
from datetime import datetime, timezone
from typing import List, Dict, Any
from ..logging_config import logger
from ..trading.executor import TradingPipeline


class SecondFilterRunner:
    def __init__(self, repo: Repositories, gmgn: MarketDataProvider, jupiter: SwapProvider, jito: ExecutionProvider, rpc: RpcProvider, strategy_groups: List[dict]):
        self.repo = repo
        self.gmgn = gmgn
        self.jupiter = jupiter
        self.jito = jito
        self.rpc = rpc
        self.strategy_groups = strategy_groups
        self.pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)

    async def run_once(self):
        # find tokens to evaluate (recently discovered)
        tokens = await self.repo.list_tokens(100)
        now = datetime.now(timezone.utc)
        for t in tokens:
            token_mint = t['token_mint']
            try:
                latest = await self.gmgn.fetch_latest_price(token_mint)
                klines = await self.gmgn.fetch_kline(token_mint, '1m', 5)
            except Exception as e:
                await self.repo.append_system_event('ERROR', 'SECOND_FILTER', 'GMGN failed', str({'error': str(e)}))
                continue

            buy_sell_1m = {'buy_volume': 100, 'sell_volume': 50}
            passed_strategies: List[Dict[str, Any]] = []
            for sg in self.strategy_groups:
                res = await run_second_filter(t, sg, latest, klines, buy_sell_1m)
                await self.repo.insert_strategy_match(token_mint, sg.get('id', 0), sg.get('config_version', 1), None, 'second_filter', res.passed, str(res.details), str(res.feature_vector))
                if res.passed:
                    passed_strategies.append(sg)
            if passed_strategies:
                await self.pipeline.handle_token_second_filter_result(token_mint, passed_strategies, None)
