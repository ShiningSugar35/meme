"""
GMGN Provider - Three Mode Support (mock/online_readonly/live)

This version is aligned with GMGN OpenAPI / gmgn-skills:
- host: https://openapi.gmgn.ai
- normal read auth: X-APIKEY header + timestamp/client_id query params
- trenches: POST /v1/trenches?chain=sol with a structured body
- token snapshot: best-effort merge of token info/security/pool_info
- kline: GET /v1/market/token_kline

Modes:
1. mock: Uses MockData, no external API calls
2. online_readonly: Calls real GMGN API for reading data only
3. live: Same read path as online_readonly; writes are not implemented here
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
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


OPENAPI_DEFAULT_BASE_URL = "https://openapi.gmgn.ai"

DEFAULT_TRENCHES_PLATFORMS: Dict[str, List[str]] = {
    "sol": [
        "Pump.fun", "pump_mayhem", "pump_mayhem_agent", "pump_agent",
        "letsbonk", "bonkers", "bags", "memoo", "liquid", "bankr", "zora",
        "surge", "anoncoin", "moonshot_app", "wendotdev", "heaven", "sugar",
        "token_mill", "believe", "trendsfun", "trends_fun", "jup_studio",
        "Moonshot", "boop", "ray_launchpad", "meteora_virtual_curve", "xstocks",
    ],
    "bsc": [
        "fourmeme", "fourmeme_agent", "bn_fourmeme", "four_xmode_agent",
        "flap", "clanker", "lunafun",
    ],
    "base": [
        "clanker", "bankr", "flaunch", "zora", "zora_creator",
        "baseapp", "basememe", "virtuals_v2", "klik",
    ],
}

DEFAULT_TRENCHES_QUOTE_ADDRESS_TYPES: Dict[str, List[int]] = {
    "sol": [4, 5, 3, 1, 13, 0],
    "bsc": [6, 7, 1, 16, 8, 3, 9, 10, 2, 17, 18, 0],
    "base": [11, 3, 12, 13, 0],
}

TRENCH_TYPE_ALIASES = {
    "new": "new_creation",
    "new_creation": "new_creation",
    "near_completion": "near_completion",
    "pump": "near_completion",  # GMGN response key for near_completion
    "complete": "completed",
    "completed": "completed",
}


class GMGNOpenApiError(Exception):
    def __init__(self, message: str, *, status_code: int = 0, api_code: Any = None, api_error: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.api_code = api_code
        self.api_error = api_error


class GMGNProvider(MarketDataProvider):
    """
    GMGN Provider with three modes:
    - mock: Uses MockData
    - online_readonly: Real API calls, read-only
    - live: Real API calls for reading; trading writes are not implemented here
    """

    def __init__(self, repo: Repositories, mode: Optional[ProviderMode] = None):
        self.repo = repo
        self.mode = mode or settings.get_provider_mode()
        self.api_base_url = self._get_api_base_url()
        self.api_keys: List[str] = [
            k.get_secret_value().strip() if hasattr(k, "get_secret_value") else str(k).strip()
            for k in settings.get_gmgn_api_keys()
            if k and (k.get_secret_value().strip() if hasattr(k, "get_secret_value") else str(k).strip())
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
                logger.warning("GMGN_API_KEY_N not set. online_readonly mode will fail if the endpoint requires it.")
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

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _env(name: str, default: Optional[str] = None) -> Optional[str]:
        value = os.getenv(name)
        if value is None:
            return default
        value = value.strip()
        return value if value else default

    @classmethod
    def _split_csv_env(cls, name: str, default: Optional[Iterable[str]] = None) -> List[str]:
        raw = cls._env(name)
        if raw is None:
            return list(default or [])
        return [x.strip() for x in raw.split(",") if x.strip()]

    @classmethod
    def _int_env(cls, name: str, default: int) -> int:
        raw = cls._env(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    @staticmethod
    def _get_api_base_url() -> str:
        # The GMGN OpenAPI client in gmgn-skills uses https://openapi.gmgn.ai.
        # If the old public-web/trading host is still in .env, prefer the OpenAPI host.
        configured = (settings.GMGN_API_BASE_URL or "").strip().rstrip("/")
        if not configured:
            return OPENAPI_DEFAULT_BASE_URL
        if configured in {"https://api.gmgn.ai", "http://api.gmgn.ai"}:
            return OPENAPI_DEFAULT_BASE_URL
        return configured

    @staticmethod
    def _normalize_openapi_path(path: Optional[str], default: str) -> str:
        path = (path or default or "").strip()
        if not path:
            return default
        if path.startswith("http://") or path.startswith("https://"):
            return path
        # Old project defaults were /api/v1/...; OpenAPI uses /v1/...
        if path.startswith("/api/v1/"):
            path = "/v1/" + path[len("/api/v1/"):]
        if not path.startswith("/"):
            path = "/" + path
        # Specific legacy aliases from the original project.
        if path == "/v1/token/price":
            return "/v1/token/info"
        if path == "/v1/token/kline":
            return "/v1/market/token_kline"
        if path in {"/v1/token/top_holder", "/v1/token/topholders"}:
            return "/v1/token/top_holders"
        return path

    @staticmethod
    def _build_auth_query(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        query: Dict[str, Any] = dict(extra or {})
        query["timestamp"] = int(time.time())
        query["client_id"] = str(uuid.uuid4())
        return query

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

    @staticmethod
    def _retryable_api_error(api_error: Any) -> bool:
        return str(api_error or "") in {"RATE_LIMIT_EXCEEDED", "RATE_LIMIT_BANNED", "ERROR_RATE_LIMIT_BLOCKED"}

    # ------------------------------------------------------------------
    # Logging and request
    # ------------------------------------------------------------------

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

    async def _make_request(
        self,
        path: str,
        query: Optional[Dict[str, Any]] = None,
        *,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not HAS_HTTPX:
            raise ImportError("httpx required for real API calls")

        path = self._normalize_openapi_path(path, path)
        url = self._build_url(path)
        method = (method or "GET").upper()
        max_attempts = max(1, len(self.api_keys))
        last_error: Optional[BaseException] = None

        async with httpx.AsyncClient(timeout=10.0) as client:
            for attempt in range(max_attempts):
                key_index, api_key = await self._next_api_key()
                headers: Dict[str, str] = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
                request_query = self._build_auth_query(query)
                request_summary: Dict[str, Any] = {
                    "query": dict(query or {}),
                    "body_keys": list((body or {}).keys())[:30],
                    "attempt": attempt + 1,
                    "max_attempts": max_attempts,
                    "method": method,
                }

                if api_key:
                    headers["X-APIKEY"] = api_key
                    request_summary["api_key"] = api_key
                    request_summary["api_key_index"] = (key_index + 1) if key_index is not None else None

                start = time.time()
                try:
                    if method == "POST":
                        response = await client.post(url, params=request_query, json=body or {}, headers=headers)
                    else:
                        response = await client.get(url, params=request_query, headers=headers)
                    latency_ms = int((time.time() - start) * 1000)
                    text = response.text

                    try:
                        data = response.json()
                    except Exception:
                        error_msg = f"GMGN API non-JSON response: HTTP {response.status_code} - {text[:500]}"
                        await self._log_request(
                            path, False, request_summary, {"body": text[:500]},
                            status_code=response.status_code,
                            latency_ms=latency_ms,
                            error_code="GMGN_NON_JSON",
                            error_summary=error_msg,
                            method=method,
                        )
                        err = GMGNOpenApiError(error_msg, status_code=response.status_code)
                        last_error = err
                        if self._retryable_status(response.status_code) and attempt < max_attempts - 1:
                            continue
                        raise err

                    api_code = data.get("code") if isinstance(data, dict) else None
                    api_error = data.get("error") if isinstance(data, dict) else None
                    api_message = data.get("message") or data.get("msg") if isinstance(data, dict) else None

                    # GMGN OpenAPI returns HTTP 200 with code != 0 for business errors.
                    if response.status_code != 200 or (api_code is not None and api_code != 0):
                        error_msg = (
                            f"GMGN API error: HTTP {response.status_code}"
                            f" code={api_code} error={api_error} message={api_message} body={text[:500]}"
                        )
                        await self._log_request(
                            path,
                            False,
                            request_summary,
                            self._compact_response_summary(data),
                            status_code=response.status_code,
                            latency_ms=latency_ms,
                            error_code=str(api_error or "GMGN_HTTP_OR_API_ERROR"),
                            error_summary=error_msg,
                            method=method,
                        )
                        err = GMGNOpenApiError(
                            error_msg,
                            status_code=response.status_code,
                            api_code=api_code,
                            api_error=api_error,
                        )
                        last_error = err
                        if (
                            (self._retryable_status(response.status_code) or self._retryable_api_error(api_error))
                            and attempt < max_attempts - 1
                        ):
                            continue
                        raise err

                    await self._log_request(
                        path,
                        True,
                        request_summary,
                        self._compact_response_summary(data),
                        status_code=response.status_code,
                        latency_ms=latency_ms,
                        method=method,
                    )
                    return data if isinstance(data, dict) else {"code": 0, "data": data}

                except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException) as e:
                    latency_ms = int((time.time() - start) * 1000)
                    error_msg = "GMGN API timeout after 10s"
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
                    last_error = GMGNOpenApiError(error_msg, status_code=504)
                    if attempt < max_attempts - 1:
                        continue
                    raise last_error from e
                except Exception as e:
                    last_error = e
                    if isinstance(e, GMGNOpenApiError):
                        raise
                    if attempt < max_attempts - 1:
                        continue
                    raise

        raise last_error or GMGNOpenApiError("GMGN API request failed")

    @staticmethod
    def _compact_response_summary(data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            return {"type": type(data).__name__}
        summary: Dict[str, Any] = {"keys": list(data.keys())[:20]}
        payload = data.get("data", data)
        if isinstance(payload, dict):
            summary["data_keys"] = list(payload.keys())[:30]
            for key in (
                "tokens", "token", "list", "rows", "items", "pairs", "results",
                "new_creation", "new", "pump", "near_completion", "complete", "completed",
                "klines", "candles",
            ):
                val = payload.get(key)
                if isinstance(val, list):
                    summary[f"data.{key}.count"] = len(val)
        elif isinstance(payload, list):
            summary["data_count"] = len(payload)
        if "code" in data:
            summary["code"] = data.get("code")
        if "error" in data:
            summary["error"] = data.get("error")
        if "message" in data:
            summary["message"] = data.get("message")
        return summary

    # ------------------------------------------------------------------
    # Normalization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _as_bool(value: Any) -> int:
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (int, float)):
            return 1 if value else 0
        if isinstance(value, str):
            return 1 if value.strip().lower() in {
                "1", "true", "yes", "y", "renounced", "locked", "burn", "burned", "safe"
            } else 0
        return 0

    @staticmethod
    def _first_present(raw: Dict[str, Any], keys: Iterable[str]) -> Any:
        for key in keys:
            if key in raw and raw.get(key) is not None and raw.get(key) != "":
                return raw.get(key)
        return None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _rate_value(cls, value: Any) -> Optional[float]:
        """Normalize ratio-like values to 0..1 when values are percentage-like."""
        val = cls._to_float(value)
        if val is None:
            return None
        # GMGN fields are usually already ratios. Some variants may return 13 for 13%.
        if val > 1 and val <= 100:
            return val / 100.0
        return val

    @staticmethod
    def _to_iso_timestamp(value: Any) -> Optional[str]:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()

        # Numeric unix seconds/ms.
        try:
            if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
                n = float(value)
                if n > 10_000_000_000:  # milliseconds
                    n = n / 1000.0
                return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
        except Exception:
            pass

        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            return str(value)

    @staticmethod
    def _unwrap_data(data: Any) -> Any:
        if isinstance(data, dict) and "data" in data:
            return data.get("data")
        return data

    @staticmethod
    def _merge_dicts(*items: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for item in items:
            if isinstance(item, dict):
                payload = item.get("data", item)
                if isinstance(payload, dict):
                    merged.update(payload)
                else:
                    merged.update(item)
        return merged

    @classmethod
    def _build_trenches_body(cls, params: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
        params = dict(params or {})
        chain = str(params.pop("chain", None) or cls._env("GMGN_CHAIN", "sol") or "sol").strip().lower()

        types = params.pop("type", None) or params.pop("types", None)
        if isinstance(types, str):
            selected_types = [x.strip() for x in types.split(",") if x.strip()]
        elif isinstance(types, list):
            selected_types = [str(x).strip() for x in types if str(x).strip()]
        else:
            selected_types = cls._split_csv_env(
                "GMGN_TRENCHES_TYPES",
                ["new_creation", "near_completion", "completed"],
            )
        selected_types = [TRENCH_TYPE_ALIASES.get(x, x) for x in selected_types]

        platforms = params.pop("launchpad_platform", None) or params.pop("launchpad_platforms", None) or params.pop("platforms", None)
        if isinstance(platforms, str):
            selected_platforms = [x.strip() for x in platforms.split(",") if x.strip()]
        elif isinstance(platforms, list):
            selected_platforms = [str(x).strip() for x in platforms if str(x).strip()]
        else:
            selected_platforms = cls._split_csv_env(
                "GMGN_TRENCHES_PLATFORMS",
                DEFAULT_TRENCHES_PLATFORMS.get(chain, []),
            )

        limit_raw = params.pop("limit", None)
        try:
            limit = int(limit_raw) if limit_raw is not None else cls._int_env("GMGN_TRENCHES_LIMIT", 80)
        except Exception:
            limit = cls._int_env("GMGN_TRENCHES_LIMIT", 80)
        limit = max(1, min(limit, 80))

        quote_types = params.pop("quote_address_type", None)
        if isinstance(quote_types, str):
            quote_address_type = [int(x.strip()) for x in quote_types.split(",") if x.strip().isdigit()]
        elif isinstance(quote_types, list):
            quote_address_type = [int(x) for x in quote_types if str(x).strip().lstrip("-").isdigit()]
        else:
            quote_address_type = DEFAULT_TRENCHES_QUOTE_ADDRESS_TYPES.get(chain, [])

        section: Dict[str, Any] = {
            "filters": ["offchain", "onchain"],
            "launchpad_platform": selected_platforms,
            "quote_address_type": quote_address_type,
            "launchpad_platform_v2": True,
            "limit": limit,
        }

        # Optional OpenAPI server-side filters from gmgn-skills.
        optional_filter_map = {
            "GMGN_TRENCHES_FILTER_PRESET": "filter_preset",
            "GMGN_TRENCHES_SORT_BY": "sort_by",
            "GMGN_TRENCHES_MIN_HOLDERS": "min_holders",
            "GMGN_TRENCHES_MIN_SWAPS": "min_swaps",
            "GMGN_TRENCHES_MIN_SMART_DEGEN_COUNT": "min_smart_degen_count",
            "GMGN_TRENCHES_MIN_RENOWNED_COUNT": "min_renowned_count",
            "GMGN_TRENCHES_MIN_LIQUIDITY": "min_liquidity",
            "GMGN_TRENCHES_MIN_MARKET_CAP": "min_market_cap",
            "GMGN_TRENCHES_MAX_MARKET_CAP": "max_market_cap",
        }
        for env_name, body_key in optional_filter_map.items():
            raw = cls._env(env_name)
            if raw is not None:
                try:
                    section[body_key] = float(raw) if "." in raw else int(raw)
                except ValueError:
                    section[body_key] = raw

        # Preserve any ad-hoc filter params passed by DiscoveryRunner/tests.
        for key, value in params.items():
            if value is not None:
                section[key] = value

        body: Dict[str, Any] = {"version": "v2"}
        for typ in selected_types:
            body[typ] = dict(section)
        return chain, body

    def _normalize_token_data(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize token data from GMGN OpenAPI/gmgn-skills-style responses to internal schema."""
        raw = raw or {}
        token_info = raw.get("token") if isinstance(raw.get("token"), dict) else {}
        pool_info = raw.get("pool") if isinstance(raw.get("pool"), dict) else {}
        security_info = raw.get("security") if isinstance(raw.get("security"), dict) else {}
        base = {**token_info, **pool_info, **security_info, **raw}

        token_mint = self._first_present(
            base,
            (
                "token_mint", "token_address", "address", "mint", "ca", "contract_address",
                "base_address", "base_token_address", "baseTokenAddress",
            ),
        )
        pool_address = self._first_present(base, ("pool_address", "pool", "pair_address", "pool_id", "pairAddress"))
        raw_created_at = self._first_present(
            base,
            (
                "pool_created_at", "pool_created_timestamp", "pool_creation_timestamp",
                "creation_timestamp", "created_timestamp", "created_at", "open_timestamp",
                "launch_timestamp", "init_time",
            ),
        )
        pool_created_at = self._to_iso_timestamp(raw_created_at)

        price_usd = self._first_present(base, ("price_usd", "price", "usd_price", "last_price", "priceUsd"))
        price_sol = self._first_present(base, ("price_sol", "sol_price", "price_native", "native_price"))
        liquidity_usd = self._first_present(base, ("liquidity_usd", "liquidity", "liquidity_in_usd", "usd_liquidity"))
        sol_side_liquidity = self._first_present(base, ("sol_side_liquidity", "quote_reserve", "quote_liquidity", "sol_liquidity"))
        volume_usd = self._first_present(base, ("volume_usd", "volume", "volume_24h", "volume_1h", "usd_volume"))
        market_cap = self._first_present(base, ("market_cap", "marketcap", "usd_market_cap", "fdv", "fully_diluted_valuation"))
        trench_type = self._first_present(base, ("type", "trench_type", "category", "migrate_state", "migration_state"))
        trench_type = TRENCH_TYPE_ALIASES.get(str(trench_type), trench_type) if trench_type is not None else None

        top_10_holder_rate = self._rate_value(self._first_present(base, ("top_10_holder_rate", "top10_holder_rate", "top_10_holder_ratio", "top10_holder_ratio")))
        top1_holder_rate = self._rate_value(self._first_present(base, ("top1_holder_rate", "top_1_holder_rate", "creator_balance_rate", "creator_hold_rate")))

        max_rug_ratio = self._rate_value(self._first_present(base, ("max_rug_ratio", "rug_ratio", "rugRatio")))
        max_insider_ratio = self._rate_value(self._first_present(base, ("max_insider_ratio", "insider_ratio", "insider_trader_amount_rate")))
        max_entrapment_ratio = self._rate_value(self._first_present(base, ("max_entrapment_ratio", "entrapment_ratio")))
        rat_trader_amount_rate = self._rate_value(self._first_present(base, ("rat_trader_amount_rate", "ratTraderAmountRate")))
        suspected_insider_hold_rate = self._rate_value(self._first_present(base, ("suspected_insider_hold_rate", "suspectedInsiderHoldRate")))
        max_bundler_rate = self._rate_value(self._first_present(base, ("max_bundler_rate", "bundler_trader_amount_rate", "bundler_amount_rate")))
        fresh_wallet_rate = self._rate_value(self._first_present(base, ("fresh_wallet_rate", "freshWalletRate")))
        sell_tax = self._rate_value(self._first_present(base, ("sell_tax", "transfer_tax", "tax")))
        dev_team_hold_rate = self._rate_value(self._first_present(base, ("dev_team_hold_rate", "dev_hold_rate", "dev_team_rate")))
        dev_token_burn_ratio = self._rate_value(self._first_present(base, ("dev_token_burn_ratio", "token_burnt", "burn_ratio", "burn_rate")))

        has_social_raw = self._first_present(base, ("has_at_least_one_social", "has_social", "social", "website", "twitter", "telegram"))
        # Deliberately keep creator_token_status strict: no aliasing or True=>creator_close coercion.
        # The strategy asked for exact creator_token_status == "creator_close".
        creator_status = self._first_present(base, ("creator_token_status",))
        burn_status = self._first_present(base, ("burn_status", "dev_token_burn_status", "token_burn_status"))

        normalized = {
            "token_mint": token_mint,
            "pool_address": pool_address,
            "pool_created_at": pool_created_at,
            "type": trench_type,
            "trench_type": trench_type,
            "latest_price_usd": price_usd,
            "price_usd": price_usd,
            "price_sol": price_sol,
            "liquidity_usd": liquidity_usd,
            "sol_side_liquidity": sol_side_liquidity,
            "volume_usd": volume_usd,
            "market_cap": market_cap,
            "symbol": self._first_present(base, ("symbol", "ticker")),
            "name": self._first_present(base, ("name", "token_name")),
            "launchpad": self._first_present(base, ("launchpad", "platform", "launchpad_platform")),
            "platform": self._first_present(base, ("platform", "launchpad", "launchpad_platform")),
            "top_10_holder_rate": top_10_holder_rate,
            "top1_holder_rate": top1_holder_rate,
            "renounced_mint": self._as_bool(
                self._first_present(base, ("renounced_mint", "mint_renounced", "mint_authority_renounced", "is_mint_renounced"))
            ),
            "renounced_freeze_account": self._as_bool(
                self._first_present(base, ("renounced_freeze_account", "freeze_renounced", "freeze_authority_renounced", "is_freeze_renounced"))
            ),
            "max_rug_ratio": max_rug_ratio,
            "max_insider_ratio": max_insider_ratio,
            "max_entrapment_ratio": max_entrapment_ratio,
            "is_wash_trading": self._as_bool(self._first_present(base, ("is_wash_trading", "wash_trading", "washTrading"))),
            "rat_trader_amount_rate": rat_trader_amount_rate,
            "suspected_insider_hold_rate": suspected_insider_hold_rate,
            "max_bundler_rate": max_bundler_rate,
            "fresh_wallet_rate": fresh_wallet_rate,
            "sell_tax": sell_tax,
            "has_social": self._as_bool(has_social_raw),
            "has_at_least_one_social": self._as_bool(has_social_raw),
            "creator_token_status": creator_status,
            "burn_status": burn_status,
            "dev_team_hold_rate": dev_team_hold_rate,
            "dev_token_burn_ratio": dev_token_burn_ratio,
            "sniper_count": self._first_present(base, ("sniper_count", "snipers", "sniperCount")),
            "holder_count": self._first_present(base, ("holder_count", "holders", "holderCount")),
            "smart_degen_count": self._first_present(base, ("smart_degen_count", "smartDegenCount")),
            "renowned_count": self._first_present(base, ("renowned_count", "renownedCount")),
            "raw_json": json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
        }
        return {k: v for k, v in normalized.items() if v is not None}

    def normalize_gmgn_trenches(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        return self._normalize_token_data(raw)

    def normalize_gmgn_token_snapshot(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        return self._normalize_token_data(raw)

    def normalize_gmgn_kline(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "open_time": self._to_iso_timestamp(raw.get("open_time") or raw.get("timestamp") or raw.get("time") or raw.get("t")),
            "open": raw.get("open") or raw.get("o"),
            "high": raw.get("high") or raw.get("h"),
            "low": raw.get("low") or raw.get("l"),
            "close": raw.get("close") or raw.get("c"),
            "buy_volume": raw.get("buy_volume") or raw.get("buyVolume") or raw.get("buy_vol") or raw.get("buyVol"),
            "sell_volume": raw.get("sell_volume") or raw.get("sellVolume") or raw.get("sell_vol") or raw.get("sellVol"),
            "volume_usd": raw.get("volume_usd") or raw.get("volume") or raw.get("v"),
            "raw_json": json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
        }

    @staticmethod
    def _extract_trench_items(data: Any) -> List[Dict[str, Any]]:
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
                            item = {**item, "trench_type": category, "type": category}
                        grouped.append(item)
        return grouped

    @staticmethod
    def _extract_kline_items(data: Any) -> List[Dict[str, Any]]:
        payload = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("klines", "candles", "list", "items", "rows", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return []

    # ------------------------------------------------------------------
    # Public provider interface
    # ------------------------------------------------------------------

    async def fetch_trenches(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        path = self._normalize_openapi_path(
            self._env("GMGN_TRENCHES_PATH", getattr(settings, "GMGN_TRENCHES_PATH", None)),
            "/v1/trenches",
        )
        try:
            if self.mode == ProviderMode.MOCK:
                self.mock_data._maybe_refresh()
                tokens = list(self.mock_data.tokens.values())
                for t in tokens:
                    t["source_mode"] = "MOCK"
                await self._log_request(path, True, params, {"count": len(tokens)})
                return tokens

            if self.mode in (ProviderMode.ONLINE_READONLY, ProviderMode.LIVE):
                chain, body = self._build_trenches_body(params)
                method = self._env("GMGN_TRENCHES_METHOD", "POST") or "POST"
                data = await self._make_request(path, {"chain": chain}, method=method, body=body)

                tokens: List[Dict[str, Any]] = []
                for item in self._extract_trench_items(data):
                    normalized = self._normalize_token_data(item)
                    if not normalized.get("token_mint"):
                        continue
                    normalized["source_mode"] = "REAL"
                    tokens.append(normalized)

                await self._log_request(path, True, {"chain": chain, "params": params}, {"count": len(tokens)}, method=method)
                return tokens

            return []

        except Exception as e:
            await self._log_request(path, False, params, {}, 500, 0, "GMGN_ERROR", str(e), method="POST")
            logger.error(f"fetch_trenches failed, skipping round: {type(e).__name__}: {e}")
            return []

    async def fetch_token_snapshot(self, token_mint: str) -> Dict[str, Any]:
        info_path = self._normalize_openapi_path(
            self._env("GMGN_TOKEN_INFO_PATH", self._env("GMGN_TOKEN_PRICE_PATH", getattr(settings, "GMGN_TOKEN_PRICE_PATH", None))),
            "/v1/token/info",
        )
        security_path = self._normalize_openapi_path(self._env("GMGN_TOKEN_SECURITY_PATH"), "/v1/token/security")
        pool_path = self._normalize_openapi_path(self._env("GMGN_TOKEN_POOL_INFO_PATH"), "/v1/token/pool_info")
        chain = self._env("GMGN_CHAIN", "sol") or "sol"

        try:
            if self.mode == ProviderMode.MOCK:
                self.mock_data._maybe_refresh()
                t = self.mock_data.tokens.get(token_mint)
                if t:
                    t["source_mode"] = "MOCK"
                    await self._log_request(info_path, True, {"token_mint": token_mint}, t)
                    return t
                return {}

            if self.mode in (ProviderMode.ONLINE_READONLY, ProviderMode.LIVE):
                query = {"chain": chain, "address": token_mint}
                info: Dict[str, Any] = {}
                security: Dict[str, Any] = {}
                pool: Dict[str, Any] = {}

                try:
                    info = await self._make_request(info_path, query)
                except Exception as e:
                    logger.warning(f"GMGN token info failed token={token_mint}: {type(e).__name__}: {e}")

                for path, target_name in ((security_path, "security"), (pool_path, "pool")):
                    try:
                        data = await self._make_request(path, query)
                        if target_name == "security":
                            security = data
                        else:
                            pool = data
                    except Exception as e:
                        # These enrichments should not block the whole second-filter/risk scan.
                        logger.debug(f"GMGN {target_name} enrichment failed token={token_mint}: {type(e).__name__}: {e}")

                merged = self._merge_dicts(info, security, pool)
                if not merged:
                    return {}
                merged.setdefault("token_address", token_mint)
                snapshot = self._normalize_token_data(merged)
                snapshot["source_mode"] = "REAL"
                await self._log_request(
                    "GMGN_TOKEN_SNAPSHOT_MERGED",
                    True,
                    {"chain": chain, "token_mint": token_mint},
                    {"keys": list(snapshot.keys())[:50]},
                )
                return snapshot

            return {}

        except Exception as e:
            await self._log_request(info_path, False, {"token_mint": token_mint}, {}, 500, 0, "GMGN_ERROR", str(e))
            logger.error(f"fetch_token_snapshot failed token={token_mint}: {type(e).__name__}: {e}")
            return {}

    async def fetch_kline(self, token_mint: str, interval: str, limit: int) -> List[Dict[str, Any]]:
        path = self._normalize_openapi_path(
            self._env("GMGN_KLINE_PATH", getattr(settings, "GMGN_KLINE_PATH", None)),
            "/v1/market/token_kline",
        )
        chain = self._env("GMGN_CHAIN", "sol") or "sol"
        now = int(time.time())
        # Fetch a little more than requested so 1m/5m intervals have enough data.
        minutes_back = max(5, int(limit or 5) * 2)
        from_ts = int((datetime.now(timezone.utc) - timedelta(minutes=minutes_back)).timestamp())
        query = {
            "chain": chain,
            "address": token_mint,
            "resolution": interval,
            "from": from_ts,
            "to": now,
        }
        try:
            if self.mode == ProviderMode.MOCK:
                self.mock_data._maybe_refresh()
                klines = self.mock_data.klines.get(token_mint, [])
                if interval != "1m":
                    # Mock only stores 1m rows; runners can still derive 5m range from completed 1m candles.
                    klines = []
                for item in klines:
                    item["source_mode"] = "MOCK"
                await self._log_request(path, True, query, {"count": len(klines), "interval": interval})
                return klines

            if self.mode in (ProviderMode.ONLINE_READONLY, ProviderMode.LIVE):
                data = await self._make_request(path, query)
                klines: List[Dict[str, Any]] = []
                for item in self._extract_kline_items(data):
                    normalized = self.normalize_gmgn_kline(item)
                    normalized["source_mode"] = "REAL"
                    klines.append(normalized)
                if limit and len(klines) > limit:
                    klines = klines[-limit:]
                await self._log_request(path, True, query, {"count": len(klines)})
                return klines

            return []

        except Exception as e:
            await self._log_request(path, False, query, {}, 500, 0, "GMGN_ERROR", str(e))
            logger.error(f"fetch_kline failed token={token_mint}: {type(e).__name__}: {e}")
            return []


    async def fetch_top_holders(self, token_mint: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetch and normalize GMGN top-holder rows for a token.

        The strategy uses this only for the deferred top1(addr_type=0) check.
        It is intentionally separate from fetch_token_snapshot so holder scans do
        not run for every discovered token.
        """
        primary_path = self._normalize_openapi_path(self._env("GMGN_TOKEN_TOP_HOLDERS_PATH"), "/v1/token/top_holders")
        fallback_paths = [primary_path]
        for candidate in ("/v1/token/holders", "/v1/token/top_holders"):
            candidate = self._normalize_openapi_path(candidate, candidate)
            if candidate not in fallback_paths:
                fallback_paths.append(candidate)

        chain = self._env("GMGN_CHAIN", "sol") or "sol"
        query = {"chain": chain, "address": token_mint, "limit": max(1, int(limit or 20))}

        if self.mode == ProviderMode.MOCK:
            self.mock_data._maybe_refresh()
            token = self.mock_data.tokens.get(token_mint) or {}
            rate = self._rate_value(token.get("top1_holder_rate"))
            holders = [{
                "rank": 1,
                "address": f"MOCK_HOLDER_{token_mint}",
                "addr_type": 0,
                "top1_holder_rate": rate,
                "hold_rate": rate,
                "source_mode": "MOCK",
            }] if rate is not None else []
            await self._log_request(primary_path, True, query, {"count": len(holders)}, method="GET")
            return holders

        if self.mode not in (ProviderMode.ONLINE_READONLY, ProviderMode.LIVE):
            return []

        last_error: Optional[BaseException] = None
        for path in fallback_paths:
            try:
                data = await self._make_request(path, query)
                holders: List[Dict[str, Any]] = []
                for idx, item in enumerate(self._extract_holder_items(data), start=1):
                    normalized = self.normalize_gmgn_holder(item, idx)
                    normalized["source_mode"] = "REAL"
                    holders.append(normalized)
                holders.sort(key=lambda x: float(x.get("hold_rate") or -1.0), reverse=True)
                if limit and len(holders) > limit:
                    holders = holders[:limit]
                await self._log_request(path, True, query, {"count": len(holders)}, method="GET")
                return holders
            except Exception as e:
                last_error = e
                logger.debug(f"GMGN top holders failed path={path} token={token_mint}: {type(e).__name__}: {e}")
                continue

        await self._log_request(primary_path, False, query, {}, 500, 0, "GMGN_ERROR", str(last_error), method="GET")
        logger.warning(f"fetch_top_holders failed token={token_mint}: {type(last_error).__name__}: {last_error}")
        return []

    async def fetch_top1_holder_rate(self, token_mint: str, addr_type: int = 0) -> Dict[str, Any]:
        holders = await self.fetch_top_holders(token_mint, limit=20)
        for holder in holders:
            holder_addr_type = holder.get("addr_type")
            if holder_addr_type is None or int(holder_addr_type) == int(addr_type):
                if holder.get("top1_holder_rate") is not None:
                    return holder
        return {"addr_type": addr_type, "top1_holder_rate": None, "source_mode": "REAL" if self.mode != ProviderMode.MOCK else "MOCK"}

    async def fetch_latest_price(self, token_mint: str) -> Dict[str, Any]:
        path = self._normalize_openapi_path(
            self._env("GMGN_TOKEN_INFO_PATH", self._env("GMGN_TOKEN_PRICE_PATH", getattr(settings, "GMGN_TOKEN_PRICE_PATH", None))),
            "/v1/token/info",
        )
        chain = self._env("GMGN_CHAIN", "sol") or "sol"
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
                data = await self._make_request(path, {"chain": chain, "address": token_mint})
                raw = self._unwrap_data(data)
                raw = raw if isinstance(raw, dict) else {}
                normalized = self._normalize_token_data(raw)
                return {
                    "price": normalized.get("price_usd") or 0.0,
                    "price_usd": normalized.get("price_usd"),
                    "price_sol": normalized.get("price_sol") or 0.0,
                    "liquidity_usd": normalized.get("liquidity_usd") or 0,
                    "sol_side_liquidity": normalized.get("sol_side_liquidity") or 0,
                    "market_cap": normalized.get("market_cap") or 0,
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
            logger.error(f"fetch_latest_price failed token={token_mint}: {type(e).__name__}: {e}")
            raise
