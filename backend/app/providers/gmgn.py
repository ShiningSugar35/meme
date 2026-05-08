import asyncio
from typing import Any, Dict, List
from .base import MarketDataProvider
from .mock_data import MockData
from ..db.repositories import Repositories
from ..logging_config import logger
import json


class GMGNProvider(MarketDataProvider):
    def __init__(self, repo: Repositories, mock: MockData):
        self.repo = repo
        self.mock = mock

    async def _log_request(self, endpoint: str, ok: bool, request_summary: Dict[str, Any], response_summary: Dict[str, Any], status_code: int = 200, latency_ms: int = 1, error_code: str = None, error_summary: str = None):
        await self.repo.append_provider_request('GMGN', endpoint, 'GET', status_code, latency_ms, ok, error_code, error_summary, json.dumps(request_summary), json.dumps(response_summary))

    async def fetch_trenches(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        # return token list
        try:
            tokens = list(self.mock.tokens.values())
            await self._log_request('/trenches', True, params, {'count': len(tokens)})
            return tokens
        except Exception as e:
            await self._log_request('/trenches', False, params, {}, 500, 0, 'GMGN_ERROR', str(e))
            raise

    async def fetch_token_snapshot(self, token_mint: str) -> Dict[str, Any]:
        t = self.mock.tokens.get(token_mint)
        await self._log_request(f'/snapshot/{token_mint}', True, {'token_mint': token_mint}, t)
        return t

    async def fetch_kline(self, token_mint: str, interval: str, limit: int) -> List[Dict[str, Any]]:
        k = self.mock.klines.get(token_mint, [])
        await self._log_request(f'/kline/{token_mint}', True, {'interval': interval, 'limit': limit}, {'count': len(k)})
        return k

    async def fetch_latest_price(self, token_mint: str) -> Dict[str, Any]:
        info = self.mock.latest.get(token_mint)
        if not info:
            await self._log_request(f'/latest/{token_mint}', False, {'token_mint': token_mint}, {}, 404, 1, 'NOT_FOUND', 'token not found')
            raise Exception('token not found')
        # increment call counter and slightly bump price for PASS1
        info['calls'] += 1
        if token_mint == 'PASS1':
            info['price'] += 0.05 * (info['calls'])
        await self._log_request(f'/latest/{token_mint}', True, {'token_mint': token_mint}, info)
        # normalize keys expected by downstream code
        return {
            'price': info['price'],
            'price_usd': info.get('price', None),
            'price_sol': info['sol_price'],
            'sol_side_liquidity': info['sol_liquidity'],
        }
