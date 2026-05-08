import asyncio
from typing import Any, Dict
from .base import ExecutionProvider
from ..db.repositories import Repositories
import json


class JitoProvider(ExecutionProvider):
    def __init__(self, repo: Repositories, scenario: str = 'success'):
        self.repo = repo
        self.scenario = scenario  # success, instruction_error, tip_too_low_once_then_success, tip_too_low_always, send_timeout, simulate_timeout

    async def _log(self, endpoint: str, ok: bool, request_summary: Dict[str, Any], response_summary: Dict[str, Any], status_code: int = 200, latency_ms: int = 1, error_code: str = None, error_summary: str = None):
        await self.repo.append_provider_request('JITO', endpoint, 'POST', status_code, latency_ms, ok, error_code, error_summary, json.dumps(request_summary), json.dumps(response_summary))

    async def get_tip_floor(self) -> Dict[str, Any]:
        res = {
            'landed_tips_25th_percentile': 1000,
            'landed_tips_50th_percentile': 2000,
            'landed_tips_75th_percentile': 3000,
            'landed_tips_95th_percentile': 5000,
            'landed_tips_99th_percentile': 8000,
            'ema_landed_tips_50th_percentile': 2000,
        }
        await self._log('/tip_floor', True, {}, res)
        return res

    async def simulate(self, transaction_or_bundle: Any) -> Dict[str, Any]:
        if self.scenario == 'instruction_error':
            res = {'ok': False, 'error': 'InstructionError', 'code': 'INSTRUCTION_ERROR'}
            await self._log('/simulate', False, {'tx': '...'}, res, 400, error_code='JITO_INSTRUCTION_ERROR', error_summary='Instruction error')
            return res
        if self.scenario == 'simulate_timeout':
            res = {'ok': False, 'error': 'timeout', 'code': 'TIMEOUT'}
            await self._log('/simulate', False, {'tx': '...'}, res, 504, error_code='JITO_TIMEOUT', error_summary='timeout')
            return res
        if self.scenario == 'tip_too_low_always':
            res = {'ok': False, 'error': 'tip too low', 'code': 'TIP_TOO_LOW'}
            await self._log('/simulate', False, {'tx': '...'}, res, 429, error_code='JITO_TIP_TOO_LOW', error_summary='tip too low')
            return res
        # default success
        res = {'ok': True, 'result': 'simulated'}
        await self._log('/simulate', True, {'tx': '...'}, res)
        return res

    async def send(self, transaction_or_bundle: Any) -> Dict[str, Any]:
        if self.scenario == 'instruction_error' or self.scenario == 'tip_too_low_always':
            # if simulate already failed, send may also fail
            res = {'ok': False, 'error': 'tip too low', 'code': 'TIP_TOO_LOW'}
            await self._log('/send', False, {'tx': '...'}, res, 429, error_code='JITO_TIP_TOO_LOW', error_summary='tip too low')
            return res
        if self.scenario == 'send_timeout':
            res = {'ok': False, 'error': 'timeout', 'code': 'TIMEOUT'}
            await self._log('/send', False, {'tx': '...'}, res, 504, error_code='JITO_TIMEOUT', error_summary='timeout')
            return res
        # success
        res = {'ok': True, 'bundle_id': 'BUNDLE123', 'signature': 'SIG123'}
        await self._log('/send', True, {'tx': '...'}, {'bundle_id': res['bundle_id'], 'signature': res['signature']})
        return res
