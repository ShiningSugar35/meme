"""
Jito Provider - Three Mode Support (mock/online_readonly/live)

Modes:
1. mock: Uses mock behavior, no external calls
2. online_readonly: Allows read-only endpoints (tip floor, status)
3. live: (Future) Real transactions, requires LIVE_TRADING_ENABLED=true

Safety:
- mock/online_readonly modes BLOCK send() operations
- online_readonly doesn't require private key
- Live mode requires all configurations
"""
import asyncio
from typing import Any, Dict
from .base import ExecutionProvider
from ..db.repositories import Repositories
from ..config import settings, ProviderMode
from ..logging_config import logger
import json
import time

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False
    logger.warning("httpx not installed. online_readonly mode will not work for Jito.")


class JitoProvider(ExecutionProvider):
    """
    Jito Provider with three modes:
    - mock: Uses mock behavior (default)
    - online_readonly: Read-only calls (tip floor, status)
    - live: (Future) Real transactions
    """
    
    def __init__(self, repo: Repositories, mode: ProviderMode = None):
        """
        Initialize Jito Provider.
        
        Args:
            repo: Database repository
            mode: ProviderMode (mock/online_readonly/live). If None, uses settings.get_provider_mode()
        """
        self.repo = repo
        self.mode = mode or settings.get_provider_mode()
        self.jito_enabled = settings.JITO_ENABLED
        self._tip_cache = None
        self._tip_cache_time = 0
        self._tip_cache_ttl = 3  # 3 seconds cache
        
        if self.mode == ProviderMode.MOCK:
            logger.info("Jito Provider initialized in MOCK mode - send() is BLOCKED")
        elif self.mode == ProviderMode.ONLINE_READONLY:
            if not HAS_HTTPX:
                raise ImportError("httpx required for online_readonly mode. Install with: pip install httpx")
            logger.info("Jito Provider initialized in ONLINE_READONLY mode - send() is BLOCKED")
        elif self.mode == ProviderMode.LIVE:
            if not self.jito_enabled or not settings.LIVE_TRADING_ENABLED:
                raise ValueError("Jito requires LIVE_TRADING_ENABLED=true")
            if not HAS_HTTPX:
                raise ImportError("httpx required for live mode. Install with: pip install httpx")
            logger.info("Jito Provider initialized in LIVE mode")

    async def _log(
        self, endpoint: str, ok: bool, 
        request_summary: Dict[str, Any], response_summary: Dict[str, Any],
        status_code: int = 200, latency_ms: int = 1,
        error_code: str = None, error_summary: str = None
    ):
        """Log provider request"""
        await self.repo.append_provider_request(
            'JITO', endpoint, 'POST', status_code, latency_ms, ok,
            error_code, error_summary,
            json.dumps(request_summary), json.dumps(response_summary)
        )

    async def _make_request(self, path: str, json_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Make HTTP request to Jito API (for online_readonly/live modes).
        
        Args:
            path: API path (e.g., '/api/v1/bundles/tip_floor')
            json_data: JSON body for POST requests
            
        Returns:
            Parsed JSON response
            
        Raises:
            Exception: On request failure, timeout, or API error
        """
        if not HAS_HTTPX:
            raise ImportError("httpx required for real API calls")
        
        url = f"https://mainnet.block-engine.jito.wtf{path}"
        headers = {'Content-Type': 'application/json'}
        
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                if json_data:
                    response = await client.post(url, json=json_data, headers=headers)
                else:
                    response = await client.get(url, headers=headers)
                
                latency_ms = int((time.time() - start) * 1000)
                
                if response.status_code != 200:
                    error_msg = f"Jito API error: {response.status_code} - {response.text}"
                    await self._log(
                        path, False, json_data or {}, {'error': error_msg},
                        status_code=response.status_code, latency_ms=latency_ms,
                        error_code='JITO_HTTP_ERROR', error_summary=error_msg
                    )
                    raise Exception(error_msg)
                
                data = response.json()
                await self._log(path, True, json_data or {}, data,
                              status_code=response.status_code, latency_ms=latency_ms)
                return data
                
        except asyncio.TimeoutError:
            latency_ms = int((time.time() - start) * 1000)
            error_msg = "Jito API timeout after 5s"
            await self._log(
                path, False, json_data or {}, {'error': error_msg},
                status_code=504, latency_ms=latency_ms,
                error_code='JITO_TIMEOUT', error_summary=error_msg
            )
            raise Exception(error_msg)
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            error_msg = str(e)
            await self._log(
                path, False, json_data or {}, {'error': error_msg},
                status_code=500, latency_ms=latency_ms,
                error_code='JITO_ERROR', error_summary=error_msg
            )
            raise

    async def get_tip_floor(self) -> Dict[str, Any]:
        """
        Get current tip floor with 3-second cache.
        Parses 50th, 75th, 95th percentile from response.
        """
        # Check cache (3 second TTL)
        now = time.time()
        if self._tip_cache and (now - self._tip_cache_time) < self._tip_cache_ttl:
            return self._tip_cache
        
        try:
            if self.mode == ProviderMode.MOCK:
                # MOCK: return mock tip floor with percentiles
                response = {
                    'landed_tips_50th_percentile': 2000,  # 50th percentile
                    'landed_tips_75th_percentile': 3000,  # 75th percentile
                    'landed_tips_95th_percentile': 5000,  # 95th percentile
                    'ema_landed_tips_50th_percentile': 2000,
                    'mode': 'MOCK'
                }
                await self._log('/api/v1/bundles/tip_floor', True, {}, {'mode': 'MOCK'})
                self._tip_cache = response
                self._tip_cache_time = now
                return response
            
            elif self.mode in [ProviderMode.ONLINE_READONLY, ProviderMode.LIVE]:
                # ONLINE_READONLY/LIVE: call real API
                data = await self._make_request('/api/v1/bundles/tip_floor')
                
                # Parse and normalize response
                tip_data = {
                    'landed_tips_50th_percentile': data.get('landed_tips_50th_percentile', 0),
                    'landed_tips_75th_percentile': data.get('landed_tips_75th_percentile', 0),
                    'landed_tips_95th_percentile': data.get('landed_tips_95th_percentile', 0),
                    'ema_landed_tips_50th_percentile': data.get('ema_landed_tips_50th_percentile', 0),
                    'raw': data if len(str(data)) < 500 else {'summary': 'response too large'}
                }
                
                self._tip_cache = tip_data
                self._tip_cache_time = now
                return tip_data
            
        except Exception as e:
            await self._log('/api/v1/bundles/tip_floor', False, {}, {}, 500, 0, 'JITO_ERROR', str(e))
            raise

    async def simulate(self, transaction_or_bundle: Any) -> Dict[str, Any]:
        """
        Simulate transaction (read-only safe, no side effects)
        
        Safe to call in any mode.
        """
        try:
            if self.mode == ProviderMode.MOCK:
                # MOCK: return mock simulation
                response = {
                    'ok': True,
                    'result': 'simulated',
                    'mode': 'MOCK'
                }
                await self._log('/api/v1/bundles/simulate', True, {'tx': '...'}, response)
                return response
            
            elif self.mode == ProviderMode.ONLINE_READONLY:
                # ONLINE_READONLY: call real API for simulation
                data = await self._make_request('/api/v1/bundles/simulate', json_data=transaction_or_bundle)
                return {'ok': True, 'result': data, 'mode': 'ONLINE_READONLY'}
            
            elif self.mode == ProviderMode.LIVE:
                # LIVE: call real API (future implementation)
                data = await self._make_request('/api/v1/bundles/simulate', json_data=transaction_or_bundle)
                return {'ok': True, 'result': data, 'mode': 'LIVE'}
                
        except Exception as e:
            await self._log('/api/v1/bundles/simulate', False, {'tx': '...'}, {}, 500, 0, 'JITO_ERROR', str(e))
            raise

    async def send(self, transaction_or_bundle: Any) -> Dict[str, Any]:
        """
        Send transaction/bundle to Jito with retry logic.
        
        CRITICAL PROTECTION: BLOCKED in ONLINE_READONLY mode (default)
        MOCK mode: Allows mock send for testing.
        LIVE mode: Implements tip ladder retry (50th→75th→95th, max 2 retries)
        InstructionError: Direct failure, no retry.
        tip too low: Retry with higher tip.
        v1: No fallback to normal RPC.
        """
        if self.mode == ProviderMode.ONLINE_READONLY:
            # ONLINE_READONLY: BLOCK send (do NOT broadcast)
            error_msg = (
                f"send() is BLOCKED in {self.mode.value} mode. "
                f"Set mode to LIVE only if you intend live trading."
            )
            logger.error(error_msg)
            await self._log('/api/v1/bundles/send', False, {'tx': '...'}, {},
                          403, 0, 'JITO_MODE_BLOCKED', error_msg)
            return {
                'ok': False,
                'error': 'MODE_BLOCKED',
                'message': error_msg
            }
        
        if self.mode == ProviderMode.MOCK:
            # MOCK: Return mock success for testing
            response = {
                'ok': True,
                'bundle_id': 'MOCK_BUNDLE123',
                'signature': 'MOCK_SIG123',
                'mode': 'MOCK'
            }
            await self._log('/api/v1/bundles/send', True, {'tx': '...'}, response)
            return response
        
        # LIVE mode: Implement retry logic
        tip_ladder = [
            ('landed_tips_50th_percentile', 0),
            ('landed_tips_75th_percentile', 1),
            ('landed_tips_95th_percentile', 2),
        ]
        
        for tip_key, retry_count in tip_ladder:
            try:
                # Get current tip floor
                tip_data = await self.get_tip_floor()
                tip_lamports = tip_data.get(tip_key, 2000)
                
                # TODO: Inject tip into transaction_or_bundle
                # transaction_with_tip = inject_tip(transaction_or_bundle, tip_lamports)
                
                # Send
                data = await self._make_request('/api/v1/bundles/send', 
                                                    json_data=transaction_or_bundle)
                
                response = {
                    'ok': True,
                    'bundle_id': data.get('bundle_id', ''),
                    'signature': data.get('signature', ''),
                    'mode': 'LIVE',
                    'tip_used': tip_lamports,
                    'retry_count': retry_count,
                }
                await self._log('/api/v1/bundles/send', True, 
                               {'tip': tip_lamports, 'retry': retry_count}, 
                               {'bundle_id': response['bundle_id']})
                return response
                
            except Exception as e:
                error_msg = str(e)
                
                # InstructionError: Direct failure, no retry
                if 'InstructionError' in error_msg:
                    await self._log('/api/v1/bundles/send', False, 
                                   {'tx': '...'}, {}, 400, 0, 
                                   'JITO_INSTRUCTION_ERROR', error_msg)
                    return {
                        'ok': False,
                        'error': 'INSTRUCTION_ERROR',
                        'message': 'Instruction error, no retry',
                        'mode': 'LIVE'
                    }
                
                # tip too low: Retry with higher tip
                if 'tip too low' in error_msg.lower() and retry_count < 2:
                    await self._log('/api/v1/bundles/send', False,
                                   {'tip': tip_lamports}, {},
                                   400, 0, 'JITO_TIP_TOO_LOW', 
                                   f"Retry {retry_count+1}/2 with higher tip")
                    continue  # Try next tip in ladder
                
                # Other errors: Fail
                await self._log('/api/v1/bundles/send', False,
                               {'tx': '...'}, {}, 500, 0, 'JITO_ERROR', error_msg)
                return {
                    'ok': False,
                    'error': 'JITO_ERROR',
                    'message': error_msg,
                    'mode': 'LIVE'
                }
        
        # All retries failed
        return {
            'ok': False,
            'error': 'MAX_RETRIES_EXCEEDED',
            'message': 'Failed after 2 tip retries',
            'mode': 'LIVE'
        }
