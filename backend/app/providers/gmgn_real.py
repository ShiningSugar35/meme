"""
GMGN Provider - Three Mode Support (mock/online_readonly/live)

Modes:
1. mock: Uses MockData, no external API calls
2. online_readonly: Calls real GMGN API for reading data only
3. live: Same as online_readonly for now, real trading not implemented here

Safety:
- online_readonly/live modes require API key when the configured endpoint requires it
- No write operations in any mode in this provider
- API keys are masked in provider request logs
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .base import MarketDataProvider
from ..config import ProviderMode, settings
from ..db.repositories import Repositories
from ..logging_config import logger

try:
    import httpx

    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False
    logger.warning("httpx not installed. online_readonly/live mode will not work for GMGN.")


class GMGNProvider(MarketDataProvider):
    """
    GMGN Provider with three modes:
    - mock: Uses MockData
    - online_readonly: Real API calls, read-only
    - live: Real API calls for reading; trading writes are not implemented here
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
        self.api_base_url = (settings.GMGN_API_BASE_URL or "https://api.gmgn.ai").rstrip("/")
        self.api_keys: List[str] = [
            k.get_secret_value() if hasattr(k, "get_secret_value") else str(k)
            for k in settings.get_gmgn_api_keys()
            if k
        ]
        self.api_key = self.api_keys[0] if self.api_keys else None  # legacy attribute
        self._key_cursor = 0
        self._key_lock = asyncio.Lock()

        self.mock_data = None
        if self.mode == ProviderMode.MOCK:
            from .mock_data import MockData

            self.mock_data = MockData()
            logger.info("GMGN Provider initialized in MOCK mode")
        elif self.mode == ProviderMode.ONLINE_READONLY:
            if not HAS_HTTPX:
                raise ImportError("httpx required for online_readonly mode. Install with: pip install httpx")
            if not self.api_keys:
                logger.warning("GMGN_API_KEY_N not set. online_readonly mode may fail if the endpoint requires it.")
            logger.info(
                "GMGN Provider initialized in ONLINE_READONLY mode",
                api_base=self.api_base_url,
                api_key_count=len(self.api_keys),
            )
        elif self.mode == ProviderMode.LIVE:
            if not HAS_HTTPX:
                raise ImportError("httpx required for live mode. Install with: pip install httpx")
            if not self.api_keys:
                raise ValueError("GMGN_API_KEY_N required for live mode")
            logger.info(
                "GMGN Provider initialized in LIVE mode",
                api_base=self.api_base_url,
                api_key_count=len(self.api_keys),
            )

    async def _log_request(
        self,
        endpoint: str,
        ok: bool,
        request_summary: Dict[str, Any],
        response_summary: Dict[str, Any],
        status_code: int = 200,
        latency_ms: int = 1,
        error_code: Optional[str] = None,
        error_summary: Optional[str] = None,
        method: str = "GET",
    ) -> None:
        """Log provider request with masked API key."""
        safe_request = dict(request_summary or {})
        if "api_key" in safe_request:
            key = str(safe_request["api_key"])
            safe_request["api_key"] = key[:4] + "..." + key[-4:] if len(key) > 8 else "***"

        await self.repo.append_provider_request(
            "GMGN",
            endpoint,
            method.upper(),
            status_code,
            latency_ms,
            ok,
            error_code,
            error_summary,
            json.dumps(safe_request, ensure_ascii=False, default=str),
            json.dumps(response_summary or {}, ensure_ascii=False, default=str),
        )

    async def _next_api_key(self) -> Tuple[Optional[int], Optional[str]]:
        if not self.api_keys:
            return None, None
        async with self._key_lock:
            idx = self._key_cursor % len(self.api_keys)
            self._key_cursor += 1
            return idx, self.api_keys[idx]

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.api_base_url}/{path.lstrip('/')}"

    @staticmethod
    def _retryable_status(status_code: int) -> bool:
        return status_code in (408, 425, 429, 500, 502, 503, 504)

    async def _make_request(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        method: str = "GET",
    ) -> Dict[str, Any]:
        """
        Make HTTP request to GMGN API.

        GMGN keys are load-balanced by round-robin across GMGN_API_KEY_N.
        On rate-limit / transient failures, the provider retries with the next key
        before giving up. The route key is sent as x-route-key and duplicated as
        x-api-key for compatibility with GMGN endpoint variants.
        """
        if not HAS_HTTPX:
            raise ImportError("httpx required for real API calls")

        url = self._build_url(path)
        params = params or {}
        method = (method or "GET").upper()
        max_attempts = max(1, len(self.api_keys))
        last_error: Optional[BaseException] = None

        async with httpx.AsyncClient(timeout=8.0) as client:
            for attempt in range(max_attempts):
                key_index, api_key = await self._next_api_key()
                headers: Dict[str, str] = {"Accept": "application/json"}
                request_summary: Dict[str, Any] = dict(params)
                request_summary["attempt"] = attempt + 1
                request_summary["max_attempts"] = max_attempts
                request_summary["method"] = method

                if api_key:
                    headers["x-route-key"] = api_key
                    headers["x-api-key"] = api_key
                    request_summary["api_key"] = api_key
                    request_summary["api_key_index"] = (key_index + 1) if key_index is not None else None

                start = time.time()
                try:
                    if method == "POST":
                        response = await client.post(url, json=params, headers=headers)
                    else:
                        response = await client.get(url, params=params, headers=headers)
                    latency_ms = int((time.time() - start) * 1000)

                    if response.status_code != 200:
                        body = response.text[:500]
                        error_msg = f"GMGN API error: {response.status_code} - {body}"
                        await self._log_request(
                            path,
                            False,
                            request_summary,
                            {"error": error_msg},
                            status_code=response.status_code,
                            latency_ms=latency_ms,
                            error_code="GMGN_HTTP_ERROR",
                            error_summary=error_msg,
                            method=method,
                        )
                        last_error = Exception(error_msg)
                        if self._retryable_status(response.status_code) and attempt < max_attempts - 1:
                            continue
                        raise last_error

                    data = response.json()
                    await self._log_request(
                        path,
                        True,
                        request_summary,
                        self._compact_response_summary(data),
                        status_code=response.status_code,
                        latency_ms=latency_ms,
                        method=method,
                    )
                    return data

                except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException) as e:
                    latency_ms = int((time.time() - start) * 1000)
                    error_msg = "GMGN API timeout after 8s"
                    await self._log_request(
                        path,
                        False,
                        request_summary,
                        {"error": error_msg},
                        status_code=504,
                        latency_ms=latency_ms,
                        error_code="GMGN_TIMEOUT",
                        error_summary=error_msg,
                        method=method,
                    )
                    last_error = Exception(error_msg)
                    if attempt < max_attempts - 1:
                        continue
                    raise last_error from e
                except Exception as e:
                    # HTTP non-200 errors have already been logged above; avoid losing
                    # the actual message in Loguru's extra fields.
                    last_error = e
                    if attempt < max_attempts - 1:
                        continue
                    raise

        raise last_error or Exception("GMGN API request failed")

    @staticmethod
    def _compact_response_summary(data: Any) -> Dict[str, Any]:
        """Avoid writing huge API payloads into provider_requests while preserving diagnostics."""
        if not isinstance(data, dict):
            return {"type": type(data).__name__}
        summary: Dict[str, Any] = {"keys": list(data.keys())[:20]}
        payload = data.get("data")
        if isinstance(payload, dict):
            summary["data_keys"] = list(payload.keys())[:20]
            for key in ("tokens", "list", "rows", "new", "pump", "complete", "completed"):
                val = payload.get(key)
                if isinstance(val, list):
                    summary[f"data.{key}.count"] = len(val)
        elif isinstance(payload, list):
            summary["data_count"] = len(payload)
        return summary

    @staticmethod
    def _as_bool(value: Any) -> int:
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (int, float)):
            return 1 if value else 0
        if isinstance(value, str):
            return 1 if value.strip().lower() in {"1", "true", "yes", "y", "renounced"} else 0
        return 0

    @staticmethod
    def _first_present(raw: Dict[str, Any], keys: Iterable[str]) -> Any:
        for key in keys:
            if key in raw and raw.get(key) is not None:
                return raw.get(key)
        return None

    def _normalize_token_data(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize token data from GMGN-style responses to the internal schema."""
        raw = raw or {}
        token_info = raw.get("token") if isinstance(raw.get("token"), dict) else {}
        base = {**token_info, **raw}

        token_mint = self._first_present(
            base,
            (
                "token_mint",
                "token_address",
                "address",
                "mint",
                "ca",
                "contract_address",
                "base_address",
            ),
        )
        pool_address = self._first_present(base, ("pool_address", "pool", "pair_address", "pool_id"))
        pool_created_at = self._first_present(
            base,
            ("pool_created_at", "pool_created_timestamp", "creation_timestamp", "created_at", "open_timestamp"),
        )

        latest_price_usd = self._first_present(base, ("price_usd", "price", "usd_price", "last_price"))
        liquidity_usd = self._first_present(base, ("liquidity_usd", "liquidity", "liquidity_in_usd"))
        volume_usd = self._first_present(base, ("volume_usd", "volume", "volume_24h", "volume_1h"))
        market_cap = self._first_present(base, ("market_cap", "marketcap", "fdv", "fully_diluted_valuation"))
        top_10_holder_rate = self._first_present(base, ("top_10_holder_rate", "top10_holder_rate", "top_10_holder_ratio"))
        top1_holder_rate = self._first_present(base, ("top1_holder_rate", "top_1_holder_rate", "creator_balance_rate"))

        return {
            "token_mint": token_mint,
            "pool_address": pool_address,
            "pool_created_at": pool_created_at,
            "latest_price_usd": latest_price_usd,
            "liquidity_usd": liquidity_usd,
            "volume_usd": volume_usd,
            "market_cap": market_cap,
            "symbol": self._first_present(base, ("symbol", "ticker")),
            "name": self._first_present(base, ("name", "token_name")),
            "launchpad": self._first_present(base, ("launchpad", "platform")),
            "trench_type": self._first_present(base, ("trench_type", "type", "category")),
            "top_10_holder_rate": top_10_holder_rate,
            "top1_holder_rate": top1_holder_rate,
            "renounced_mint": self._as_bool(
                self._first_present(base, ("renounced_mint", "mint_renounced", "mint_authority_renounced"))
            ),
            "renounced_freeze_account": self._as_bool(
                self._first_present(base, ("renounced_freeze_account", "freeze_renounced", "freeze_authority_renounced"))
            ),
            "raw_json": json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
        }

    def normalize_gmgn_trenches(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize trenches response."""
        return self._normalize_token_data(raw)

    def normalize_gmgn_token_snapshot(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize token snapshot/price response."""
        return self._normalize_token_data(raw)

    def normalize_gmgn_kline(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize kline/candlestick response."""
        return {
            "open_time": raw.get("open_time") or raw.get("timestamp") or raw.get("time"),
            "open": raw.get("open") or raw.get("o"),
            "high": raw.get("high") or raw.get("h"),
            "low": raw.get("low") or raw.get("l"),
            "close": raw.get("close") or raw.get("c"),
            "buy_volume": raw.get("buy_volume") or raw.get("buy_vol"),
            "sell_volume": raw.get("sell_volume") or raw.get("sell_vol"),
            "volume_usd": raw.get("volume_usd") or raw.get("volume") or raw.get("v"),
            "raw_json": json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
        }

    @staticmethod
    def _extract_trench_items(data: Any) -> List[Dict[str, Any]]:
        """
        Extract token items from common GMGN/trenches payload shapes.

        Supported examples:
        - {"data": {"tokens": [...]}}
        - {"data": {"list": [...]}}
        - {"data": [...]}
        - {"data": {"new": [...], "pump": [...], "complete": [...]}}
        - top-level list
        """
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if not isinstance(data, dict):
            return []

        payload = data.get("data", data)
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if not isinstance(payload, dict):
            return []

        for key in ("tokens", "token", "list", "rows", "items", "pairs", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

        grouped: List[Dict[str, Any]] = []
        category_alias = {
            "new": "new_creation",
            "new_creation": "new_creation",
            "pump": "near_completion",
            "near_completion": "near_completion",
            "complete": "completed",
            "completed": "completed",
        }
        for key, category in category_alias.items():
            value = payload.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        if "trench_type" not in item and "type" not in item and "category" not in item:
                            item = {**item, "trench_type": category}
                        grouped.append(item)
        return grouped

    async def fetch_trenches(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Fetch list of trending/trench tokens.

        MOCK: Returns mock data from MockData.
        ONLINE_READONLY/LIVE: Calls real GMGN endpoint. GMGN's trenches endpoint is
        POST in the official skill docs, so this method defaults to POST while still
        allowing override by setting GMGN_TRENCHES_METHOD in settings.
        """
        path = settings.GMGN_TRENCHES_PATH
        try:
            if self.mode == ProviderMode.MOCK:
                self.mock_data._maybe_refresh()
                tokens = list(self.mock_data.tokens.values())
                for t in tokens:
                    t["source_mode"] = "MOCK"
                await self._log_request(path, True, params, {"count": len(tokens)})
                return tokens

            if self.mode in (ProviderMode.ONLINE_READONLY, ProviderMode.LIVE):
                method = getattr(settings, "GMGN_TRENCHES_METHOD", "POST") or "POST"
                data = await self._make_request(path, params, method=method)

                tokens: List[Dict[str, Any]] = []
                for item in self._extract_trench_items(data):
                    normalized = self._normalize_token_data(item)
                    normalized["source_mode"] = "REAL"
                    tokens.append(normalized)

                await self._log_request(path, True, params, {"count": len(tokens)}, method=method)
                return tokens

            return []

        except Exception as e:
            await self._log_request(path, False, params, {}, 500, 0, "GMGN_ERROR", str(e))
            logger.error(f"fetch_trenches failed, skipping round: {e}")
            return []

    async def fetch_token_snapshot(self, token_mint: str) -> Dict[str, Any]:
        """Fetch token snapshot from GMGN."""
        path = f"{settings.GMGN_TOKEN_PRICE_PATH}/{token_mint}"
        try:
            if self.mode == ProviderMode.MOCK:
                self.mock_data._maybe_refresh()
                t = self.mock_data.tokens.get(token_mint)
                if t:
                    t["source_mode"] = "MOCK"
                    await self._log_request(path, True, {"token_mint": token_mint}, t)
                    return t
                return {}

            if self.mode in (ProviderMode.ONLINE_READONLY, ProviderMode.LIVE):
                data = await self._make_request(path)
                raw = data.get("data", {}) if isinstance(data, dict) else {}
                snapshot = self._normalize_token_data(raw)
                if snapshot:
                    snapshot["source_mode"] = "REAL"
                await self._log_request(path, True, {"token_mint": token_mint}, snapshot)
                return snapshot

            return {}

        except Exception as e:
            await self._log_request(path, False, {"token_mint": token_mint}, {}, 500, 0, "GMGN_ERROR", str(e))
            logger.error(f"fetch_token_snapshot failed token={token_mint}: {e}")
            return {}

    async def fetch_kline(self, token_mint: str, interval: str, limit: int) -> List[Dict[str, Any]]:
        """Fetch kline/candlestick data."""
        path = f"{settings.GMGN_KLINE_PATH}/{token_mint}"
        params = {"interval": interval, "limit": limit}
        try:
            if self.mode == ProviderMode.MOCK:
                self.mock_data._maybe_refresh()
                klines = self.mock_data.klines.get(token_mint, [])
                for item in klines:
                    item["source_mode"] = "MOCK"
                await self._log_request(path, True, params, {"count": len(klines)})
                return klines

            if self.mode in (ProviderMode.ONLINE_READONLY, ProviderMode.LIVE):
                data = await self._make_request(path, params)
                raw_klines = data.get("data", {}).get("klines", []) if isinstance(data, dict) else []
                klines: List[Dict[str, Any]] = []
                for item in raw_klines:
                    if isinstance(item, dict):
                        normalized = self.normalize_gmgn_kline(item)
                        normalized["source_mode"] = "REAL"
                        klines.append(normalized)
                await self._log_request(path, True, params, {"count": len(klines)})
                return klines

            return []

        except Exception as e:
            await self._log_request(path, False, params, {}, 500, 0, "GMGN_ERROR", str(e))
            logger.error(f"fetch_kline failed token={token_mint}: {e}")
            return []

    async def fetch_latest_price(self, token_mint: str) -> Dict[str, Any]:
        """Fetch latest price for token."""
        try:
            if self.mode == ProviderMode.MOCK:
                self.mock_data._maybe_refresh()
                info = self.mock_data.latest.get(token_mint)
                if not info:
                    await self._log_request(
                        f"/latest/{token_mint}",
                        False,
                        {"token_mint": token_mint},
                        {},
                        404,
                        1,
                        "NOT_FOUND",
                        "token not found",
                    )
                    raise Exception("token not found")

                info["calls"] += 1
                if token_mint == "PASS1":
                    info["price"] += 0.05 * info["calls"]

                await self._log_request(f"/latest/{token_mint}", True, {"token_mint": token_mint}, info)
                return {
                    "price": info["price"],
                    "price_usd": info.get("price"),
                    "price_sol": info["sol_price"],
                    "sol_side_liquidity": info["sol_liquidity"],
                    "source_mode": "MOCK",
                }

            if self.mode in (ProviderMode.ONLINE_READONLY, ProviderMode.LIVE):
                path = f"{settings.GMGN_TOKEN_PRICE_PATH}/{token_mint}"
                data = await self._make_request(path)
                raw = data.get("data", {}) if isinstance(data, dict) else {}
                return {
                    "price": raw.get("price_usd") or raw.get("price") or 0.0,
                    "price_usd": raw.get("price_usd") or raw.get("price"),
                    "price_sol": raw.get("price_sol") or raw.get("sol_price") or 0.0,
                    "sol_side_liquidity": raw.get("sol_side_liquidity") or raw.get("liquidity") or 0,
                    "raw_json": json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
                    "source_mode": "REAL",
                }

            return {}

        except Exception as e:
            await self._log_request(
                f"/latest/{token_mint}",
                False,
                {"token_mint": token_mint},
                {},
                500,
                0,
                "GMGN_ERROR",
                str(e),
            )
            raise
