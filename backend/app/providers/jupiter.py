import asyncio
from typing import Any, Dict
from .base import SwapProvider
from ..db.repositories import Repositories
import json


class JupiterProvider(SwapProvider):
    def __init__(self, repo: Repositories, scenario: str = 'success'):
        self.repo = repo
        self.scenario = scenario  # 'success', 'high_impact', 'rate_limit', 'timeout', 'malformed'

    async def _log(self, endpoint: str, ok: bool, request_summary: Dict[str, Any], response_summary: Dict[str, Any], status_code: int = 200, latency_ms: int = 1, error_code: str = None, error_summary: str = None):
        await self.repo.append_provider_request('JUPITER', endpoint, 'POST', status_code, latency_ms, ok, error_code, error_summary, json.dumps(request_summary), json.dumps(response_summary))

    async def quote_exact_in(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> Dict[str, Any]:
        if self.scenario == 'high_impact':
            quote = {
                'inAmount': amount,
                'outAmount': int(amount * 0.9),
                'otherAmountThreshold': int(amount * 0.89),
                'swapMode': 'ExactIn',
                'priceImpactPct': 0.15,  # > 0.10
                'routePlan': {'path': [input_mint, output_mint]},
                'raw_json': {}
            }
            await self._log('/quote', True, {'input': input_mint, 'output': output_mint, 'amount': amount}, quote)
            return quote
        if self.scenario == 'rate_limit':
            await self._log('/quote', False, {'input': input_mint}, {}, status_code=429, error_code='JUPITER_RATE_LIMIT', error_summary='rate limited')
            raise Exception('rate limit')
        if self.scenario == 'timeout':
            await self._log('/quote', False, {'input': input_mint}, {}, status_code=504, error_code='JUPITER_TIMEOUT', error_summary='timeout')
            raise Exception('timeout')
        if self.scenario == 'malformed':
            # missing routePlan
            quote = {
                'inAmount': amount,
                'outAmount': int(amount * 0.9),
                'priceImpactPct': 0.5,
                'raw_json': {}
            }
            await self._log('/quote', True, {'input': input_mint}, quote)
            return quote
        # success: small price impact (expressed as fraction, e.g. 0.005 == 0.5%)
        quote = {
            'inAmount': amount,
            'outAmount': int(amount * 0.9),
            'otherAmountThreshold': int(amount * 0.89),
            'swapMode': 'ExactIn',
            'priceImpactPct': 0.005,  # 0.5% impact -> below 10% threshold
            'routePlan': {'path': [input_mint, output_mint]},
            'raw_json': {}
        }
        await self._log('/quote', True, {'input': input_mint, 'output': output_mint, 'amount': amount}, quote)
        return quote

    async def build_swap_instructions(self, quote: Dict[str, Any], user_public_key: str, extra: Dict[str, Any]) -> Dict[str, Any]:
        if self.scenario == 'malformed':
            instr = {'instructions': [], 'raw_tx': 'MOCK'}
            await self._log('/build', True, {'user': user_public_key}, instr)
            return instr
        instr = {'instructions': ['mock_instr'], 'raw_tx': 'MOCK', 'quote': quote}
        await self._log('/build', True, {'user': user_public_key, 'extra': extra}, {'built': True})
        return instr
