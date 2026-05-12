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
from typing import Any, Dict, List, Optional
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
        self.rpc_urls: List[str] = settings.get_rpc_http_urls() or ["https://api.mainnet-beta.solana.com"]
        self.rpc_url = self.rpc_urls[0]  # legacy attribute used by some old code/tests
        self.rpc_ws_url = settings.get_rpc_ws_url()
        self._rpc_cursor = 0
        self._rpc_lock = asyncio.Lock()
        
        if self.mode == ProviderMode.MOCK:
            logger.info("RPC Provider initialized in MOCK mode - no real RPC calls")
        elif self.mode == ProviderMode.ONLINE_READONLY:
            if not HAS_HTTPX:
                raise ImportError("httpx required for online_readonly mode. Install with: pip install httpx")
            logger.info(
                "RPC Provider initialized in ONLINE_READONLY mode",
                rpc_http_count=len(self.rpc_urls),
                ws_configured=bool(self.rpc_ws_url),
                ws_required=False,
            )
        elif self.mode == ProviderMode.LIVE:
            if not HAS_HTTPX:
                raise ImportError("httpx required for live mode. Install with: pip install httpx")
            logger.info(
                "RPC Provider initialized in LIVE mode",
                rpc_http_count=len(self.rpc_urls),
                ws_configured=bool(self.rpc_ws_url),
                ws_required=False,
            )

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

    @staticmethod
    def _mask_rpc_url(url: str) -> str:
        if not url:
            return ""
        if "/v2/" in url:
            prefix, key = url.rsplit("/v2/", 1)
            if len(key) > 8:
                return f"{prefix}/v2/{key[:4]}...{key[-4:]}"
            return f"{prefix}/v2/****"
        if "/solana/" in url:
            prefix, key = url.rsplit("/solana/", 1)
            if len(key) > 8:
                return f"{prefix}/solana/{key[:4]}...{key[-4:]}"
            return f"{prefix}/solana/****"
        return url

    async def _next_rpc_url(self) -> tuple[int, str]:
        async with self._rpc_lock:
            idx = self._rpc_cursor % len(self.rpc_urls)
            self._rpc_cursor += 1
            return idx, self.rpc_urls[idx]

    @staticmethod
    def _is_retryable_rpc_error(data: Dict[str, Any]) -> bool:
        err = data.get('error') if isinstance(data, dict) else None
        if not err:
            return False
        msg = str(err.get('message', '')).lower() if isinstance(err, dict) else str(err).lower()
        return any(token in msg for token in ['rate limit', 'too many request', 'temporarily unavailable', 'timeout', '429'])

    async def _make_request(self, method: str, params: list) -> Dict[str, Any]:
        """
        Make HTTP JSON-RPC request to Solana RPC.

        This provider is HTTP-only. It round-robins across SOLANA_RPC_HTTP_URLS
        (for example the four Alchemy HTTP endpoints) and retries another URL on
        timeout, HTTP 429/5xx, and retryable JSON-RPC errors.
        """
        if not HAS_HTTPX:
            raise ImportError("httpx required for real RPC calls")

        headers = {'Content-Type': 'application/json'}
        payload = self._make_rpc_request(method, params)
        request_summary_base = {'method': method, 'params': params}
        last_error: Optional[Exception] = None

        max_attempts = max(1, len(self.rpc_urls))
        async with httpx.AsyncClient(timeout=5.0) as client:
            for attempt in range(max_attempts):
                idx, url = await self._next_rpc_url()
                start = time.time()
                request_summary = {
                    **request_summary_base,
                    'rpc_url_index': idx + 1,
                    'rpc_url': self._mask_rpc_url(url),
                    'attempt': attempt + 1,
                    'max_attempts': max_attempts,
                }

                try:
                    response = await client.post(url, json=payload, headers=headers)
                    latency_ms = int((time.time() - start) * 1000)

                    retryable_http = response.status_code in (408, 425, 429, 500, 502, 503, 504)
                    if response.status_code != 200:
                        error_msg = f"RPC HTTP error: {response.status_code} - {response.text[:500]}"
                        await self._log(
                            method, False, request_summary, {'error': error_msg},
                            status_code=response.status_code, latency_ms=latency_ms,
                            error_code='RPC_HTTP_ERROR', error_summary=error_msg,
                        )
                        last_error = Exception(error_msg)
                        if retryable_http and attempt < max_attempts - 1:
                            continue
                        raise last_error

                    data = response.json()
                    if 'error' in data:
                        error_msg = data['error'].get('message', 'Unknown RPC error')
                        await self._log(
                            method, False, request_summary, {'error': error_msg, 'rpc_error': data.get('error')},
                            status_code=200, latency_ms=latency_ms,
                            error_code='RPC_ERROR', error_summary=error_msg,
                        )
                        last_error = Exception(f"RPC error: {error_msg}")
                        if self._is_retryable_rpc_error(data) and attempt < max_attempts - 1:
                            continue
                        raise last_error

                    await self._log(
                        method, True, request_summary, data.get('result', {}),
                        status_code=200, latency_ms=latency_ms,
                    )
                    return data.get('result', {})

                except (asyncio.TimeoutError, TimeoutError) as e:
                    latency_ms = int((time.time() - start) * 1000)
                    error_msg = "RPC timeout after 5s"
                    await self._log(
                        method, False, request_summary, {'error': error_msg},
                        status_code=504, latency_ms=latency_ms,
                        error_code='RPC_TIMEOUT', error_summary=error_msg,
                    )
                    last_error = Exception(error_msg)
                    if attempt < max_attempts - 1:
                        continue
                    raise last_error from e
                except Exception as e:
                    # Network errors are retryable; non-retryable HTTP/RPC errors already raised above.
                    latency_ms = int((time.time() - start) * 1000)
                    error_msg = str(e)
                    await self._log(
                        method, False, request_summary, {'error': error_msg},
                        status_code=500, latency_ms=latency_ms,
                        error_code='RPC_ERROR', error_summary=error_msg,
                    )
                    last_error = e
                    if attempt < max_attempts - 1:
                        continue
                    raise

        raise last_error or Exception("RPC request failed")

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
