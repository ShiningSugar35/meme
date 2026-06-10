"""
Jupiter Provider - Three Mode Support (mock/online_readonly/live)

Modes:
1. mock: Returns mock data, no external API calls
2. online_readonly: Calls real Jupiter Swap API for quotes, read-only
3. live: Calls Jupiter Swap API to build a swap transaction

Important alignment:
- The configured base is expected to be https://api.jup.ag/swap/v1
- Quote path is /quote
- Swap path is /swap
- API key header is x-api-key
"""
import asyncio
from typing import Any, Dict, List, Optional
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
    logger.warning("httpx not installed. online_readonly/live mode will not work for Jupiter.")


class JupiterProvider(SwapProvider):
    def __init__(self, repo: Repositories, mode: ProviderMode = None):
        self.repo = repo
        self.mode = mode or settings.get_provider_mode()
        self.api_base_url = settings.get_jupiter_api_base_url()
        self.api_keys: List[str] = [
            k.get_secret_value() if hasattr(k, 'get_secret_value') else str(k)
            for k in settings.get_jupiter_api_keys()
        ]
        self.api_key = self.api_keys[0] if self.api_keys else None  # legacy attribute
        self._key_cursor = 0
        self._key_lock = asyncio.Lock()
        self._test_scenario = 'success'  # for tests only

        if self.mode == ProviderMode.MOCK:
            logger.info("Jupiter Provider initialized in MOCK mode - schema validation only")
        elif self.mode == ProviderMode.ONLINE_READONLY:
            if not HAS_HTTPX:
                raise ImportError("httpx required for online_readonly mode. Install with: pip install httpx")
            logger.info(
                "Jupiter Provider initialized in ONLINE_READONLY mode",
                api_base=self.api_base_url,
                api_key_count=len(self.api_keys),
            )
        elif self.mode == ProviderMode.LIVE:
            if not HAS_HTTPX:
                raise ImportError("httpx required for live mode. Install with: pip install httpx")
            if not self.api_keys:
                logger.warning("JUPITER_API_KEY_N not set. Using public API if endpoint permits it (rate limited).")
            logger.info(
                "Jupiter Provider initialized in LIVE mode",
                api_base=self.api_base_url,
                api_key_count=len(self.api_keys),
            )

    async def _log(
        self, endpoint: str, ok: bool, request_summary: Dict[str, Any],
        response_summary: Dict[str, Any], status_code: int = 200,
        latency_ms: int = 1, error_code: str = None, error_summary: str = None
    ):
        safe_request = dict(request_summary or {})
        if safe_request.get('api_key'):
            key = safe_request['api_key']
            safe_request['api_key'] = key[:4] + '...' + key[-4:] if len(key) > 8 else '****'
        await self.repo.append_provider_request(
            'JUPITER', endpoint, 'POST', status_code, latency_ms, ok,
            error_code, error_summary,
            json.dumps(safe_request), json.dumps(response_summary or {})
        )

    async def _next_api_key(self) -> tuple[Optional[int], Optional[str]]:
        if not self.api_keys:
            return None, None
        async with self._key_lock:
            idx = self._key_cursor % len(self.api_keys)
            self._key_cursor += 1
            return idx, self.api_keys[idx]

    def _build_url(self, path: str) -> str:
        if path.startswith('http://') or path.startswith('https://'):
            return path
        return f"{self.api_base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _retryable_status(status_code: int) -> bool:
        return status_code in (408, 425, 429, 500, 502, 503, 504)

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    async def _make_request(self, method: str, path: str, data: Dict[str, Any] = None,
                            params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not HAS_HTTPX:
            raise ImportError("httpx required for real API calls")

        url = self._build_url(path)
        max_attempts = max(1, len(self.api_keys))
        last_error: Optional[Exception] = None

        async with httpx.AsyncClient(timeout=10.0) as client:
            for attempt in range(max_attempts):
                key_index, api_key = await self._next_api_key()
                headers: Dict[str, str] = {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                }
                request_summary: Dict[str, Any] = {
                    'params': params,
                    'data': data,
                    'attempt': attempt + 1,
                    'max_attempts': max_attempts,
                }
                if api_key:
                    headers['x-api-key'] = api_key
                    request_summary['api_key'] = api_key
                    request_summary['api_key_index'] = (key_index + 1) if key_index is not None else None

                start = time.time()
                try:
                    if method.upper() == 'GET':
                        response = await client.get(url, params=params, headers=headers)
                    else:
                        response = await client.post(url, json=data, params=params, headers=headers)

                    latency_ms = int((time.time() - start) * 1000)
                    if response.status_code != 200:
                        error_msg = f"Jupiter API error: {response.status_code} - {response.text[:500]}"
                        await self._log(
                            path, False, request_summary, {'error': error_msg},
                            status_code=response.status_code, latency_ms=latency_ms,
                            error_code='JUPITER_HTTP_ERROR', error_summary=error_msg,
                        )
                        last_error = Exception(error_msg)
                        if self._retryable_status(response.status_code) and attempt < max_attempts - 1:
                            continue
                        raise last_error

                    result = response.json()
                    await self._log(path, True, request_summary, result, response.status_code, latency_ms)
                    return result

                except (asyncio.TimeoutError, TimeoutError) as e:
                    latency_ms = int((time.time() - start) * 1000)
                    error_msg = "Jupiter API timeout after 10s"
                    await self._log(
                        path, False, request_summary, {'error': error_msg},
                        status_code=504, latency_ms=latency_ms,
                        error_code='JUPITER_TIMEOUT', error_summary=error_msg,
                    )
                    last_error = Exception(error_msg)
                    if attempt < max_attempts - 1:
                        continue
                    raise last_error from e
                except Exception as e:
                    latency_ms = int((time.time() - start) * 1000)
                    error_msg = str(e)
                    await self._log(
                        path, False, request_summary, {'error': error_msg},
                        status_code=500, latency_ms=latency_ms,
                        error_code='JUPITER_ERROR', error_summary=error_msg,
                    )
                    last_error = e
                    if attempt < max_attempts - 1:
                        continue
                    raise

        raise last_error or Exception("Jupiter API request failed")

    def _validate_quote_response(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("Jupiter quote response is not a dict")
        required = ['inAmount', 'outAmount', 'routePlan']
        missing = [k for k in required if k not in data]
        if missing:
            raise ValueError(f"Jupiter quote missing required fields: {missing}")
        data['priceImpactPct'] = self._to_float(data.get('priceImpactPct'), 0.0)
        return data

    async def quote_exact_in(
        self, input_mint: str, output_mint: str, amount: int,
        slippage_bps: int
    ) -> Dict[str, Any]:
        try:
            if self.mode == ProviderMode.MOCK:
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
                await self._log('/quote', True,
                                {'input': input_mint, 'output': output_mint, 'amount': amount, 'slippageBps': slippage_bps},
                                {'impact': quote['priceImpactPct']})
            elif self.mode in [ProviderMode.ONLINE_READONLY, ProviderMode.LIVE]:
                params = {
                    'inputMint': input_mint,
                    'outputMint': output_mint,
                    'amount': amount,
                    'slippageBps': slippage_bps,
                    'swapMode': 'ExactIn',
                }
                data = await self._make_request('GET', '/quote', params=params)
                quote = self._validate_quote_response(data)
            else:
                raise ValueError(f"Unsupported mode: {self.mode}")

            price_impact = self._to_float(quote.get('priceImpactPct'), 0.0)
            quote['priceImpactPct'] = price_impact
            if price_impact > 0.10:  # TODO: make configurable per strategy
                await self._log('/quote', False,
                                {'input': input_mint, 'output': output_mint},
                                quote.get('summary', {}),
                                error_code='HIGH_PRICE_IMPACT',
                                error_summary=f"Price impact too high: {price_impact}")
                quote['error'] = 'HIGH_PRICE_IMPACT'

            return quote
        except Exception as e:
            await self._log('/quote', False,
                            {'input': input_mint, 'output': output_mint},
                            {}, 500, 0, 'JUPITER_ERROR', str(e))
            raise

    async def build_swap_instructions(
        self, quote: Dict[str, Any], user_public_key: str,
        extra: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            if not quote.get('inAmount') or not quote.get('outAmount'):
                raise ValueError("Quote missing inAmount or outAmount")

            if self.mode == ProviderMode.MOCK:
                instr = {
                    'instructions': [],
                    'addressLookupTableAddresses': [],
                    'swapTransaction': None,
                    'lastValidBlockHeight': None,
                    'prioritizationFeeLamports': None,
                    'computeUnitLimit': None,
                    'prioritizationType': None,
                    'dynamicSlippageReport': None,
                    'simulationError': None,
                    'mode': 'MOCK_NO_TRANSACTION'
                }
                await self._log('/swap', True, {'userPublicKey': user_public_key}, {'built': True, 'mode': 'MOCK'})
                return instr

            if self.mode == ProviderMode.ONLINE_READONLY:
                instr = {
                    'instructions': [],
                    'addressLookupTableAddresses': [],
                    'swapTransaction': None,
                    'lastValidBlockHeight': None,
                    'prioritizationFeeLamports': None,
                    'computeUnitLimit': None,
                    'prioritizationType': None,
                    'dynamicSlippageReport': None,
                    'simulationError': None,
                    'mode': 'ONLINE_READONLY_NO_TRANSACTION'
                }
                await self._log('/swap', True, {'userPublicKey': user_public_key}, {'built': True, 'mode': 'ONLINE_READONLY'})
                return instr

            if self.mode == ProviderMode.LIVE:
                json_data = {
                    'quoteResponse': quote,
                    'userPublicKey': user_public_key,
                    'wrapUnwrapSOL': True,
                    'dynamicComputeUnitLimit': True,
                    'dynamicSlippage': True,
                    'prioritizationFeeLamports': {
                        'priorityLevelWithMaxLamports': {
                            'maxLamports': int(getattr(settings, 'JUPITER_MAX_PRIORITY_FEE_LAMPORTS', 1_000_000)),
                            'priorityLevel': getattr(settings, 'JUPITER_PRIORITY_LEVEL', 'veryHigh'),
                        }
                    },
                }
                if extra:
                    json_data.update(extra)
                data = await self._make_request('POST', '/swap', data=json_data)
                instr = {
                    'instructions': data.get('instructions', []),
                    'addressLookupTableAddresses': data.get('addressLookupTableAddresses', []),
                    'swapTransaction': data.get('swapTransaction'),
                    'lastValidBlockHeight': data.get('lastValidBlockHeight'),
                    'prioritizationFeeLamports': data.get('prioritizationFeeLamports'),
                    'computeUnitLimit': data.get('computeUnitLimit'),
                    'prioritizationType': data.get('prioritizationType'),
                    'dynamicSlippageReport': data.get('dynamicSlippageReport'),
                    'simulationError': data.get('simulationError'),
                    'raw_swap_response': data,
                    'mode': 'LIVE',
                }
                await self._log('/swap', True, {'userPublicKey': user_public_key}, {'built': True, 'mode': 'LIVE'})
                return instr

            raise ValueError(f"Unsupported mode: {self.mode}")
        except Exception as e:
            await self._log('/swap', False, {'userPublicKey': user_public_key}, {}, 500, 0, 'JUPITER_ERROR', str(e))
            raise
