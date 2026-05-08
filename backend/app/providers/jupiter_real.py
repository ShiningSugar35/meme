"""
Jupiter Provider - Three Mode Support (mock/online_readonly/live)

Modes:
1. mock: Returns mock data, no external API calls
2. online_readonly: Calls real Jupiter API for quotes, read-only
3. live: (Future) Real API calls, can execute trades

Safety:
- online_readonly/live modes require API access
- No write operations in mock/online_readonly modes
- priceImpactPct > threshold blocks trades
"""
import asyncio
from typing import Any, Dict
from .base import SwapProvider
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
    logger.warning("httpx not installed. online_readonly mode will not work for Jupiter.")


class JupiterProvider(SwapProvider):
    """
    Jupiter Provider with three modes:
    - mock: Returns mock data (default)
    - online_readonly: Real API calls, read-only (quotes only)
    - live: (Future) Real API calls, can execute trades
    """
    
    def __init__(self, repo: Repositories, mode: ProviderMode = None):
        """
        Initialize Jupiter Provider.
        
        Args:
            repo: Database repository
            mode: ProviderMode (mock/online_readonly/live). If None, uses settings.get_provider_mode()
        """
        self.repo = repo
        self.mode = mode or settings.get_provider_mode()
        self.api_base_url = settings.JUPITER_API_BASE_URL or "https://quote-api.jup.ag"
        self._test_scenario = 'success'  # for testing only
        
        if self.mode == ProviderMode.MOCK:
            logger.info("Jupiter Provider initialized in MOCK mode - schema validation only")
        elif self.mode == ProviderMode.ONLINE_READONLY:
            if not HAS_HTTPX:
                raise ImportError("httpx required for online_readonly mode. Install with: pip install httpx")
            logger.info(f"Jupiter Provider initialized in ONLINE_READONLY mode - API: {self.api_base_url}")
        elif self.mode == ProviderMode.LIVE:
            if not HAS_HTTPX:
                raise ImportError("httpx required for live mode. Install with: pip install httpx")
            logger.info(f"Jupiter Provider initialized in LIVE mode - API: {self.api_base_url}")

    async def _log(
        self, endpoint: str, ok: bool, request_summary: Dict[str, Any],
        response_summary: Dict[str, Any], status_code: int = 200,
        latency_ms: int = 1, error_code: str = None, error_summary: str = None
    ):
        """Log provider request"""
        await self.repo.append_provider_request(
            'JUPITER', endpoint, 'POST', status_code, latency_ms, ok,
            error_code, error_summary,
            json.dumps(request_summary), json.dumps(response_summary)
        )

    async def _make_request(
        self, method: str, path: str, 
        params: Dict[str, Any] = None, json_data: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        Make HTTP request to Jupiter API (for online_readonly/live modes).
        
        Args:
            method: HTTP method (GET, POST)
            path: API path (e.g., '/v6/quote')
            params: Query parameters
            json_data: JSON body for POST requests
            
        Returns:
            Parsed JSON response
            
        Raises:
            Exception: On request failure, timeout, or API error
        """
        if not HAS_HTTPX:
            raise ImportError("httpx required for real API calls")
        
        url = f"{self.api_base_url}{path}"
        headers = {'Content-Type': 'application/json'}
        
        # Add API key if available
        if settings.JUPITER_API_KEY_MEME1:
            headers['Authorization'] = f"Bearer {settings.JUPITER_API_KEY_MEME1.get_secret_value()}"
        
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                if method.upper() == 'GET':
                    response = await client.get(url, params=params, headers=headers)
                else:
                    response = await client.post(url, json=json_data, headers=headers)
                
                latency_ms = int((time.time() - start) * 1000)
                
                if response.status_code != 200:
                    error_msg = f"Jupiter API error: {response.status_code} - {response.text}"
                    await self._log(
                        path, False, params or json_data or {}, {'error': error_msg},
                        status_code=response.status_code, latency_ms=latency_ms,
                        error_code='JUPITER_HTTP_ERROR', error_summary=error_msg
                    )
                    raise Exception(error_msg)
                
                data = response.json()
                await self._log(
                    path, True, params or json_data or {}, data,
                    status_code=response.status_code, latency_ms=latency_ms
                )
                return data
                
        except asyncio.TimeoutError:
            latency_ms = int((time.time() - start) * 1000)
            error_msg = "Jupiter API timeout after 5s"
            await self._log(
                path, False, params or json_data or {}, {'error': error_msg},
                status_code=504, latency_ms=latency_ms,
                error_code='JUPITER_TIMEOUT', error_summary=error_msg
            )
            raise Exception(error_msg)
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            error_msg = str(e)
            await self._log(
                path, False, params or json_data or {}, {'error': error_msg},
                status_code=500, latency_ms=latency_ms,
                error_code='JUPITER_ERROR', error_summary=error_msg
            )
            raise

    def _validate_quote_response(self, quote: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate and normalize quote response.
        Extracts key fields: priceImpactPct, routePlan, outAmount, otherAmountThreshold.
        Logs only summary, not full quote.
        """
        # TODO: Confirm Jupiter API fields for priceImpactPct (may be 'priceImpactPct' or 'impactPct')
        return {
            'inputMint': quote.get('inputMint'),
            'outputMint': quote.get('outputMint'),
            'inAmount': quote.get('inAmount'),
            'outAmount': quote.get('outAmount'),  # Required for trade execution
            'otherAmountThreshold': quote.get('otherAmountThreshold'),  # Slippage-protected min out
            'swapMode': quote.get('swapMode'),
            'priceImpactPct': float(quote.get('priceImpactPct', 0)),  # Used for hard cap check
            'routePlan': quote.get('routePlan', []),  # Array of swap steps
            'contextSlot': quote.get('contextSlot'),
            'timeTaken': quote.get('timeTaken'),
            # Save only summary to logs, not full raw_json
            'summary': {
                'input': quote.get('inputMint'),
                'output': quote.get('outputMint'),
                'impact': quote.get('priceImpactPct'),
                'out': quote.get('outAmount'),
            }
        }

    async def quote_exact_in(
        self, input_mint: str, output_mint: str, amount: int,
        slippage_bps: int
    ) -> Dict[str, Any]:
        """
        Get a quote for exact input amount.
        
        MOCK: Returns mock quote (read-only safe)
        ONLINE_READONLY: Calls real Jupiter API, validates response
        LIVE: Same as ONLINE_READONLY
        """
        try:
            # Get quote (mock or real)
            quote = None
            if self.mode == ProviderMode.MOCK:
                # MOCK: return mock quote, support test scenarios
                price_impact = 0.15 if getattr(self, '_test_scenario', 'success') == 'high_impact' else 0.005
                quote = {
                    'inAmount': str(amount),
                    'outAmount': str(int(amount * 0.95)),
                    'otherAmountThreshold': str(int(amount * 0.94)),
                    'swapMode': 'ExactIn',
                    'priceImpactPct': price_impact,
                    'routePlan': [{'swapInfo': {'label': 'Orca', 'inputMint': input_mint, 'outputMint': output_mint}}],
                    'mode': 'MOCK'
                }
                await self._log('/v6/quote', True,
                              {'input': input_mint, 'output': output_mint, 'amount': amount, 'slippageBps': slippage_bps},
                              {'impact': quote['priceImpactPct']})
            elif self.mode in [ProviderMode.ONLINE_READONLY, ProviderMode.LIVE]:
                # ONLINE_READONLY/LIVE: call real API
                path = '/v6/quote'
                params = {
                    'inputMint': input_mint,
                    'outputMint': output_mint,
                    'amount': amount,
                    'slippageBps': slippage_bps,
                    'swapMode': 'ExactIn',
                }
                
                data = await self._make_request('GET', path, params=params)
                
                # Validate and normalize response
                quote = self._validate_quote_response(data)
            else:
                raise ValueError(f"Unsupported mode: {self.mode}")
            
            # Check price impact (default 10% = 0.10) for ALL modes
            if quote:
                price_impact = quote.get('priceImpactPct', 0)
                if price_impact > 0.10:  # TODO: make threshold configurable via strategy
                    await self._log('/v6/quote', False, 
                                  {'input': input_mint, 'output': output_mint}, 
                                  quote.get('summary', {}),
                                  error_code='HIGH_PRICE_IMPACT',
                                  error_summary=f"Price impact too high: {price_impact}")
                    quote['error'] = 'HIGH_PRICE_IMPACT'
            
            return quote
            
        except Exception as e:
            await self._log('/v6/quote', False,
                          {'input': input_mint, 'output': output_mint},
                          {}, 500, 0, 'JUPITER_ERROR', str(e))
            raise

    async def build_swap_instructions(
        self, quote: Dict[str, Any], user_public_key: str,
        extra: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Build swap transaction instructions.
        
        MOCK: Schema validation only, no real transaction
        ONLINE_READONLY: Schema validation only, no real transaction
        LIVE: (Future) Calls real Jupiter API to build transaction
        """
        try:
            # Basic schema validation (safe for all modes)
            if not quote.get('inAmount') or not quote.get('outAmount'):
                raise ValueError("Quote missing inAmount or outAmount")
            
            if self.mode == ProviderMode.MOCK:
                # MOCK: return mock instructions (no real transaction)
                instr = {
                    'instructions': [],
                    'addressLookupTableAddresses': [],
                    'swapTransaction': None,
                    'mode': 'MOCK_NO_TRANSACTION'
                }
                await self._log('/v6/swap', True,
                              {'userPublicKey': user_public_key},
                              {'built': True, 'mode': 'MOCK'})
                return instr
            
            elif self.mode == ProviderMode.ONLINE_READONLY:
                # ONLINE_READONLY: schema validation only, no real transaction
                instr = {
                    'instructions': [],
                    'addressLookupTableAddresses': [],
                    'swapTransaction': None,
                    'mode': 'ONLINE_READONLY_NO_TRANSACTION'
                }
                await self._log('/v6/swap', True,
                              {'userPublicKey': user_public_key},
                              {'built': True, 'mode': 'ONLINE_READONLY'})
                return instr
            
            elif self.mode == ProviderMode.LIVE:
                # LIVE: call real API (future implementation)
                path = '/v6/swap'
                json_data = {
                    'quoteResponse': quote,
                    'userPublicKey': user_public_key,
                    'wrapUnwrapSOL': True,
                }
                
                data = await self._make_request('POST', path, json_data=json_data)
                
                instr = {
                    'instructions': data.get('instructions', []),
                    'addressLookupTableAddresses': data.get('addressLookupTableAddresses', []),
                    'swapTransaction': data.get('swapTransaction'),
                    'mode': 'LIVE'
                }
                await self._log('/v6/swap', True,
                              {'userPublicKey': user_public_key},
                              {'built': True, 'mode': 'LIVE'})
                return instr
                
        except Exception as e:
            await self._log('/v6/swap', False,
                          {'userPublicKey': user_public_key},
                          {}, 500, 0, 'JUPITER_ERROR', str(e))
            raise
