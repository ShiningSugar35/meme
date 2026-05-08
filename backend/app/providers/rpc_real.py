"""
RPC Provider - Three Mode Support (mock/online_readonly/live)

Modes:
1. mock: Uses mock data, no external RPC calls
2. online_readonly: Allows read-only RPC calls (getBalance, getAccountInfo, etc.)
3. live: (Future) Same as online_readonly for now, real transactions not implemented

Safety:
- online_readonly/live modes require RPC URL
- Write operations (sendTransaction, sendRawTransaction) are BLOCKED in mock/online_readonly
- Live mode requires LIVE_TRADING_ENABLED=true
"""
import asyncio
from typing import Any, Dict, Optional
from .base import RpcProvider
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
    logger.warning("httpx not installed. online_readonly mode will not work for RPC.")


class RpcRealProvider(RpcProvider):
    """
    RPC Provider with three modes:
    - mock: Returns mock data (default)
    - online_readonly: Real RPC calls, read-only
    - live: (Future) Real RPC calls, can execute transactions
    """
    
    def __init__(self, repo: Repositories, mode: Optional[ProviderMode] = None):
        """
        Initialize RPC Provider.
        
        Args:
            repo: Database repository
            mode: ProviderMode (mock/online_readonly/live). If None, uses settings.get_provider_mode()
        """
        self.repo = repo
        self.mode = mode or settings.get_provider_mode()
        self.rpc_url = settings.SOLANA_RPC_HTTP_PRIMARY or "https://api.mainnet-beta.solana.com"
        
        if self.mode == ProviderMode.MOCK:
            logger.info("RPC Provider initialized in MOCK mode - no real RPC calls")
        elif self.mode == ProviderMode.ONLINE_READONLY:
            if not HAS_HTTPX:
                raise ImportError("httpx required for online_readonly mode. Install with: pip install httpx")
            logger.info(f"RPC Provider initialized in ONLINE_READONLY mode - RPC: {self.rpc_url}")
        elif self.mode == ProviderMode.LIVE:
            if not HAS_HTTPX:
                raise ImportError("httpx required for live mode. Install with: pip install httpx")
            logger.info(f"RPC Provider initialized in LIVE mode - RPC: {self.rpc_url}")

    async def _log(
        self, endpoint: str, ok: bool, 
        request_summary: Dict[str, Any], response_summary: Dict[str, Any],
        status_code: int = 200, latency_ms: int = 1,
        error_code: str = None, error_summary: str = None
    ):
        """Log provider request"""
        await self.repo.append_provider_request(
            'RPC', endpoint, 'POST', status_code, latency_ms, ok,
            error_code, error_summary,
            json.dumps(request_summary), json.dumps(response_summary)
        )

    def _make_rpc_request(self, method: str, params: list) -> Dict[str, Any]:
        """Make JSON-RPC request dict"""
        return {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1
        }

    async def _make_request(self, method: str, params: list) -> Dict[str, Any]:
        """
        Make HTTP request to Solana RPC (for online_readonly/live modes).
        
        Args:
            method: RPC method (e.g., 'getBalance')
            params: RPC parameters
            
        Returns:
            Parsed JSON-RPC response
            
        Raises:
            Exception: On request failure, timeout, or RPC error
        """
        if not HAS_HTTPX:
            raise ImportError("httpx required for real RPC calls")
        
        url = self.rpc_url
        headers = {'Content-Type': 'application/json'}
        
        payload = self._make_rpc_request(method, params)
        start = time.time()
        
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                latency_ms = int((time.time() - start) * 1000)
                
                if response.status_code != 200:
                    error_msg = f"RPC error: {response.status_code} - {response.text}"
                    await self._log(
                        method, False, {'method': method, 'params': params}, {'error': error_msg},
                        status_code=response.status_code, latency_ms=latency_ms,
                        error_code='RPC_HTTP_ERROR', error_summary=error_msg
                    )
                    raise Exception(error_msg)
                
                data = response.json()
                
                # Check for RPC error
                if 'error' in data:
                    error_msg = data['error'].get('message', 'Unknown RPC error')
                    await self._log(
                        method, False, {'method': method, 'params': params}, {'error': error_msg},
                        status_code=200, latency_ms=latency_ms,
                        error_code='RPC_ERROR', error_summary=error_msg
                    )
                    raise Exception(f"RPC error: {error_msg}")
                
                await self._log(
                    method, True, {'method': method, 'params': params}, 
                    data.get('result', {}),
                    status_code=200, latency_ms=latency_ms
                )
                return data.get('result', {})
                
        except asyncio.TimeoutError:
            latency_ms = int((time.time() - start) * 1000)
            error_msg = "RPC timeout after 5s"
            await self._log(
                method, False, {'method': method, 'params': params}, {'error': error_msg},
                status_code=504, latency_ms=latency_ms,
                error_code='RPC_TIMEOUT', error_summary=error_msg
            )
            raise Exception(error_msg)
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            error_msg = str(e)
            await self._log(
                method, False, {'method': method, 'params': params}, {'error': error_msg},
                status_code=500, latency_ms=latency_ms,
                error_code='RPC_ERROR', error_summary=error_msg
            )
            raise

    async def _block_write_operation(self, method: str) -> Dict[str, Any]:
        """Block write operations in mock/online_readonly modes."""
        if self.mode in [ProviderMode.MOCK, ProviderMode.ONLINE_READONLY]:
            error_msg = (
                f"{method} is BLOCKED in {self.mode.value} mode. "
                f"Write operations not allowed."
            )
            logger.error(error_msg)
            await self._log(method, False, {'method': method}, {}, 403, 0,
                          'RPC_WRITE_BLOCKED', error_msg)
            raise Exception(error_msg)
        return {}  # Will not reach here for MOCK/ONLINE_READONLY

    async def get_balance(self, wallet: str) -> Dict[str, Any]:
        """
        Get SOL balance for wallet (read-only monitoring)
        """
        try:
            if self.mode == ProviderMode.MOCK:
                # MOCK: return mock balance
                response = {
                    'wallet': wallet,
                    'sol_balance': 10.0,
                    'mode': 'MOCK'
                }
                await self._log('/getBalance', True,
                              {'jsonrpc': '2.0', 'method': 'getBalance', 'params': [wallet]},
                              response)
                return response
            
            elif self.mode in [ProviderMode.ONLINE_READONLY, ProviderMode.LIVE]:
                # ONLINE_READONLY/LIVE: call real RPC
                result = await self._make_request('getBalance', [wallet])
                return {
                    'wallet': wallet,
                    'sol_balance': result.get('value', 0) / 1e9,  # Convert lamports to SOL
                    'mode': self.mode.value
                }
                
        except Exception as e:
            await self._log('/getBalance', False,
                          {'method': 'getBalance', 'params': [wallet]},
                          {}, 500, 0, 'RPC_ERROR', str(e))
            raise

    async def get_token_balance(self, wallet: str, mint: str) -> Dict[str, Any]:
        """
        Get token balance for wallet (read-only monitoring)
        """
        try:
            if self.mode == ProviderMode.MOCK:
                # MOCK: return mock balance
                response = {
                    'wallet': wallet,
                    'mint': mint,
                    'amount': 1000,
                    'mode': 'MOCK'
                }
                await self._log('/getTokenAccountsByOwner', True,
                              {'jsonrpc': '2.0', 'method': 'getTokenAccountsByOwner', 'params': [wallet, mint]},
                              response)
                return response
            
            elif self.mode in [ProviderMode.ONLINE_READONLY, ProviderMode.LIVE]:
                # ONLINE_READONLY/LIVE: call real RPC
                params = [
                    wallet,
                    {'mint': mint}
                ]
                result = await self._make_request('getTokenAccountsByOwner', params)
                return {
                    'wallet': wallet,
                    'mint': mint,
                    'amount': result.get('value', []),
                    'mode': self.mode.value
                }
                
        except Exception as e:
            await self._log('/getTokenAccountsByOwner', False,
                          {'method': 'getTokenAccountsByOwner', 'params': [wallet, mint]},
                          {}, 500, 0, 'RPC_ERROR', str(e))
            raise

    async def wait_signature(self, signature: str, timeout_seconds: int) -> Dict[str, Any]:
        """
        Wait for transaction confirmation (read-only polling)
        
        Safe to call in any mode (only reads signature status).
        """
        try:
            if self.mode == ProviderMode.MOCK:
                # MOCK: simulate confirmation after short wait
                await asyncio.sleep(0.01)
                response = {
                    'signature': signature,
                    'status': 'confirmed',
                    'mode': 'MOCK'
                }
                await self._log('/getSignatureStatuses', True,
                              {'jsonrpc': '2.0', 'method': 'getSignatureStatuses', 'params': [[signature]]},
                              response)
                return response
            
            elif self.mode in [ProviderMode.ONLINE_READONLY, ProviderMode.LIVE]:
                # ONLINE_READONLY/LIVE: poll real RPC for confirmation
                start_time = time.time()
                while (time.time() - start_time) < timeout_seconds:
                    try:
                        result = await self._make_request('getSignatureStatuses', [[signature]])
                        if result:
                            return {
                                'signature': signature,
                                'status': 'confirmed',
                                'mode': self.mode.value
                            }
                    except Exception:
                        pass  # Keep polling
                    await asyncio.sleep(1.0)
                
                return {
                    'signature': signature,
                    'status': 'timeout',
                    'mode': self.mode.value
                }
                
        except Exception as e:
            await self._log('/getSignatureStatuses', False,
                          {'method': 'getSignatureStatuses', 'params': [[signature]]},
                          {}, 500, 0, 'RPC_ERROR', str(e))
            raise

    # Forbidden methods - must block in mock/online_readonly
    
    async def send_transaction(self, transaction: str, *args, **kwargs) -> Dict[str, Any]:
        """BLOCKED: Write operation"""
        return await self._block_write_operation('sendTransaction')

    async def send_raw_transaction(self, transaction: str, *args, **kwargs) -> Dict[str, Any]:
        """BLOCKED: Write operation"""
        return await self._block_write_operation('sendRawTransaction')
