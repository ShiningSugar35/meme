"""
GMGN Provider - Three Mode Support (mock/online_readonly/live)

Modes:
1. mock: Uses MockData, no external API calls
2. online_readonly: Calls real GMGN API for reading data only
3. live: (Future) Same as online_readonly for now, real trading not implemented

Safety:
- online_readonly/live modes require API key
- No write operations in any mode
- API keys are masked in logs
"""
import asyncio
from typing import Any, Dict, List, Optional
from .base import MarketDataProvider
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
    logger.warning("httpx not installed. online_readonly mode will not work for GMGN.")


class GMGNProvider(MarketDataProvider):
    """
    GMGN Provider with three modes:
    - mock: Uses MockData (default)
    - online_readonly: Real API calls, read-only
    - live: (Future) Real API calls, write operations
    """
    
    def __init__(self, repo: Repositories, mode: Optional[ProviderMode] = None):
        """
        Initialize GMGN Provider.
        
        Args:
            repo: Database repository
            mode: ProviderMode (mock/online_readonly/live). If None, uses settings.get_provider_mode()
        """
        self.repo = repo
        self.mode = mode or settings.get_provider_mode()
        self.api_base_url = settings.GMGN_API_BASE_URL or "https://api.gmgn.ai"
        self.api_key = settings.GMGN_API_KEY_1.get_secret_value() if settings.GMGN_API_KEY_1 else None
        
        # Set up mock data for mock mode
        self.mock_data = None
        if self.mode == ProviderMode.MOCK:
            from .mock_data import MockData
            self.mock_data = MockData()
            logger.info("GMGN Provider initialized in MOCK mode")
        elif self.mode == ProviderMode.ONLINE_READONLY:
            if not HAS_HTTPX:
                raise ImportError("httpx required for online_readonly mode. Install with: pip install httpx")
            if not self.api_key:
                logger.warning("GMGN_API_KEY_1 not set. online_readonly mode may fail.")
            logger.info(f"GMGN Provider initialized in ONLINE_READONLY mode - API: {self.api_base_url}")
        elif self.mode == ProviderMode.LIVE:
            if not HAS_HTTPX:
                raise ImportError("httpx required for live mode. Install with: pip install httpx")
            if not self.api_key:
                raise ValueError("GMGN_API_KEY_1 required for live mode")
            logger.info(f"GMGN Provider initialized in LIVE mode - API: {self.api_base_url}")

    async def _log_request(
        self, endpoint: str, ok: bool, 
        request_summary: Dict[str, Any], response_summary: Dict[str, Any],
        status_code: int = 200, latency_ms: int = 1,
        error_code: str = None, error_summary: str = None
    ):
        """Log provider request with masked API key"""
        # Mask API key in request summary
        safe_request = dict(request_summary)
        if 'api_key' in safe_request:
            key = safe_request['api_key']
            if len(key) > 8:
                safe_request['api_key'] = key[:4] + '...' + key[-4:]
        
        await self.repo.append_provider_request(
            'GMGN', endpoint, 'GET', status_code, latency_ms, ok,
            error_code, error_summary,
            json.dumps(safe_request), json.dumps(response_summary)
        )

    async def _make_request(self, path: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Make HTTP request to GMGN API (for online_readonly/live modes).
        
        Args:
            path: API path (e.g., '/api/v1/trenches')
            params: Query parameters
            
        Returns:
            Parsed JSON response
            
        Raises:
            Exception: On request failure, timeout, or API error
        """
        if not HAS_HTTPX:
            raise ImportError("httpx required for real API calls")
        
        url = f"{self.api_base_url}{path}"
        headers = {}
        if self.api_key:
            headers['Authorization'] = f"Bearer {self.api_key}"
        
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url, params=params, headers=headers)
                latency_ms = int((time.time() - start) * 1000)
                
                if response.status_code != 200:
                    error_msg = f"GMGN API error: {response.status_code} - {response.text}"
                    await self._log_request(
                        path, False, params or {}, {'error': error_msg},
                        status_code=response.status_code, latency_ms=latency_ms,
                        error_code='GMGN_HTTP_ERROR', error_summary=error_msg
                    )
                    raise Exception(error_msg)
                
                data = response.json()
                await self._log_request(
                    path, True, params or {}, data,
                    status_code=response.status_code, latency_ms=latency_ms
                )
                return data
                
        except asyncio.TimeoutError:
            latency_ms = int((time.time() - start) * 1000)
            error_msg = "GMGN API timeout after 5s"
            await self._log_request(
                path, False, params or {}, {'error': error_msg},
                status_code=504, latency_ms=latency_ms,
                error_code='GMGN_TIMEOUT', error_summary=error_msg
            )
            raise Exception(error_msg)
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            error_msg = str(e)
            await self._log_request(
                path, False, params or {}, {'error': error_msg},
                status_code=500, latency_ms=latency_ms,
                error_code='GMGN_ERROR', error_summary=error_msg
            )
            raise

    def _normalize_token_data(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize token data from GMGN API to internal schema.
        
        Returns:
            Dict with standardized fields
        """
        return {
            'token_mint': raw.get('token_mint') or raw.get('address'),
            'pool_address': raw.get('pool_address') or raw.get('pool'),
            'pool_created_at': raw.get('pool_created_at'),
            'latest_price_usd': raw.get('price_usd') or raw.get('price'),
            'liquidity_usd': raw.get('liquidity_usd') or raw.get('liquidity'),
            'volume_usd': raw.get('volume_usd') or raw.get('volume'),
            'market_cap': raw.get('market_cap'),
            'top_10_holder_rate': raw.get('top_10_holder_rate'),
            'top1_holder_rate': raw.get('top1_holder_rate'),
            'renounced_mint': 1 if raw.get('renounced_mint') else 0,
            'renounced_freeze_account': 1 if raw.get('renounced_freeze_account') else 0,
            'raw_json': json.dumps(raw) if raw else None,
        }

    async def fetch_trenches(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Fetch list of trending tokens.
        
        MOCK: Returns mock data from MockData
        ONLINE_READONLY: Calls real GMGN /trenches API
        LIVE: Same as ONLINE_READONLY
        """
        try:
            if self.mode == ProviderMode.MOCK:
                # MOCK: return mock data
                tokens = list(self.mock_data.tokens.values())
                await self._log_request(settings.GMGN_TRENCHES_PATH, True, params, {'count': len(tokens)})
                return tokens
            
            elif self.mode in [ProviderMode.ONLINE_READONLY, ProviderMode.LIVE]:
                # ONLINE_READONLY/LIVE: call real API
                path = settings.GMGN_TRENCHES_PATH
                data = await self._make_request(path, params)
                
                # Normalize response
                tokens = []
                raw_tokens = data.get('data', {}).get('tokens', []) if isinstance(data, dict) else []
                for t in raw_tokens:
                    tokens.append(self._normalize_token_data(t))
                
                await self._log_request(path, True, params, {'count': len(tokens)})
                return tokens
                
        except Exception as e:
            await self._log_request(settings.GMGN_TRENCHES_PATH, False, params, {}, 500, 0, 'GMGN_ERROR', str(e))
            # Skip this round, don't block system
            logger.error("fetch_trenches failed, skipping round", error=str(e))
            return []

    async def fetch_token_snapshot(self, token_mint: str) -> Dict[str, Any]:
        """
        Fetch token snapshot from GMGN.
        
        MOCK: Returns mock data
        ONLINE_READONLY: Calls real GMGN /token/price API
        LIVE: Same as ONLINE_READONLY
        """
        try:
            if self.mode == ProviderMode.MOCK:
                # MOCK: return mock snapshot
                t = self.mock_data.tokens.get(token_mint)
                if t:
                    await self._log_request(f"{settings.GMGN_TOKEN_PRICE_PATH}/{token_mint}", True, 
                                           {'token_mint': token_mint}, t)
                    return t
                return {}
            
            elif self.mode in [ProviderMode.ONLINE_READONLY, ProviderMode.LIVE]:
                # ONLINE_READONLY/LIVE: call real API
                path = f"{settings.GMGN_TOKEN_PRICE_PATH}/{token_mint}"
                data = await self._make_request(path)
                
                # Normalize response
                snapshot = self._normalize_token_data(data.get('data', {})) if isinstance(data, dict) else {}
                await self._log_request(path, True, {'token_mint': token_mint}, snapshot)
                return snapshot
                
        except Exception as e:
            await self._log_request(f"{settings.GMGN_TOKEN_PRICE_PATH}/{token_mint}", False, 
                                   {'token_mint': token_mint}, {}, 500, 0, 'GMGN_ERROR', str(e))
            # Return pass=false or empty on failure
            logger.error("fetch_token_snapshot failed", token=token_mint, error=str(e))
            return {}

    async def fetch_kline(self, token_mint: str, interval: str, limit: int) -> List[Dict[str, Any]]:
        """
        Fetch kline (candlestick) data.
        
        MOCK: Returns mock klines
        ONLINE_READONLY: Calls real GMGN /token/kline API
        LIVE: Same as ONLINE_READONLY
        """
        try:
            if self.mode == ProviderMode.MOCK:
                # MOCK: return mock klines
                k = self.mock_data.klines.get(token_mint, [])
                await self._log_request(f"{settings.GMGN_KLINE_PATH}/{token_mint}", True,
                                       {'token_mint': token_mint, 'interval': interval, 'limit': limit},
                                       {'count': len(k)})
                return k
            
            elif self.mode in [ProviderMode.ONLINE_READONLY, ProviderMode.LIVE]:
                # ONLINE_READONLY/LIVE: call real API
                path = f"{settings.GMGN_KLINE_PATH}/{token_mint}"
                params = {'interval': interval, 'limit': limit}
                data = await self._make_request(path, params)
                
                # Normalize response
                klines = data.get('data', {}).get('klines', []) if isinstance(data, dict) else []
                await self._log_request(path, True, params, {'count': len(klines)})
                return klines
                
        except Exception as e:
            await self._log_request(f"{settings.GMGN_KLINE_PATH}/{token_mint}", False,
                                   {'token_mint': token_mint, 'interval': interval},
                                   {}, 500, 0, 'GMGN_ERROR', str(e))
            # Return empty on failure
            logger.error("fetch_kline failed", token=token_mint, error=str(e))
            return []

    async def fetch_latest_price(self, token_mint: str) -> Dict[str, Any]:
        """
        Fetch latest price for token.
        
        MOCK: Returns mock price from MockData
        ONLINE_READONLY: Calls real GMGN /token/price API
        LIVE: Same as ONLINE_READONLY
        """
        try:
            if self.mode == ProviderMode.MOCK:
                # MOCK: return mock price with increment
                info = self.mock_data.latest.get(token_mint)
                if not info:
                    await self._log_request(f"/latest/{token_mint}", False, 
                                           {'token_mint': token_mint}, {}, 404, 1, 'NOT_FOUND', 'token not found')
                    raise Exception('token not found')
                
                # Increment call counter and bump price for PASS1
                info['calls'] += 1
                if token_mint == 'PASS1':
                    info['price'] += 0.05 * info['calls']
                
                await self._log_request(f"/latest/{token_mint}", True, 
                                       {'token_mint': token_mint}, info)
                
                # Normalize keys expected by downstream code
                return {
                    'price': info['price'],
                    'price_usd': info.get('price', None),
                    'price_sol': info['sol_price'],
                    'sol_side_liquidity': info['sol_liquidity'],
                }
            
            elif self.mode in [ProviderMode.ONLINE_READONLY, ProviderMode.LIVE]:
                # ONLINE_READONLY/LIVE: call real API
                path = f"{settings.GMGN_TOKEN_PRICE_PATH}/{token_mint}"
                data = await self._make_request(path)
                
                # Normalize response
                raw = data.get('data', {}) if isinstance(data, dict) else {}
                return {
                    'price': raw.get('price_usd') or raw.get('price', 0.0),
                    'price_usd': raw.get('price_usd'),
                    'price_sol': raw.get('price_sol') or raw.get('sol_price', 0.0),
                    'sol_side_liquidity': raw.get('sol_side_liquidity') or raw.get('liquidity', 0),
                    'raw_json': json.dumps(raw) if raw else None,
                }
                
        except Exception as e:
            await self._log_request(f"/latest/{token_mint}", False,
                                   {'token_mint': token_mint}, {}, 500, 0, 'GMGN_ERROR', str(e))
            raise
