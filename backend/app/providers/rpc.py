import asyncio
from typing import Any, Dict
from .base import RpcProvider
from ..db.repositories import Repositories
import json
import time


class MockRpcProvider(RpcProvider):
    def __init__(self, repo: Repositories):
        self.repo = repo

    async def _log(self, endpoint: str, ok: bool, request_summary: Dict[str, Any], response_summary: Dict[str, Any], status_code: int = 200, latency_ms: int = 1, error_code: str = None, error_summary: str = None):
        await self.repo.append_provider_request('RPC', endpoint, 'POST', status_code, latency_ms, ok, error_code, error_summary, json.dumps(request_summary), json.dumps(response_summary))

    async def get_balance(self, wallet: str) -> Dict[str, Any]:
        res = {'wallet': wallet, 'sol_balance': 10.0}
        await self._log('/get_balance', True, {'wallet': wallet}, res)
        return res

    async def get_token_balance(self, wallet: str, mint: str) -> Dict[str, Any]:
        res = {'wallet': wallet, 'mint': mint, 'amount': 1000}
        await self._log('/get_token_balance', True, {'wallet': wallet, 'mint': mint}, res)
        return res

    async def wait_signature(self, signature: str, timeout_seconds: int) -> Dict[str, Any]:
        # simulate confirmation
        t0 = time.time()
        await asyncio.sleep(0.01)
        res = {'signature': signature, 'status': 'confirmed'}
        await self._log('/wait_signature', True, {'signature': signature}, res)
        return res
