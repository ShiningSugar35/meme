"""
GMGN Provider - Three Mode Support (mock/online_readonly/live)

Modes:
1. mock: Uses MockData, no external API calls
2. online_readonly: Calls real GMGN API for reading data only
3. live: Same as online_readonly for now, real trading writes are not implemented here

Safety:
- online_readonly/live modes require API key/client_id when the configured endpoint requires it
- No write operations in any mode in this provider
- API keys and client ids are masked in provider request logs
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
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

    Important GMGN compatibility note:
    GMGN's `/v1/trenches` OpenAPI gateway validates `chain` as a request-level
    parameter.  In practice that means `chain` must be present in the query string
    even when the endpoint is called with POST and the filter payload is JSON.
    This provider therefore sends `chain` in both places for trenches:
      - query string: chain=sol
      - JSON body: chain=sol, version=v2, new_creation={...}
    The duplication is intentional and prevents `400 BAD_REQUEST: missing chain`.
    """

    def __init__(self, repo: Repositories, mode: Optional[ProviderMode] = None):
        self.repo = repo
        self.mode = mode or settings.get_provider_mode()
        self.api_base_url = (settings.GMGN_API_BASE_URL or "https://api.gmgn.ai").rstrip("/")

        self.accounts: List[Dict[str, str]] = []
        for account in settings.get_gmgn_accounts():
            api_key = str(account.get("api_key") or "").strip()
            client_id = str(account.get("client_id") or account.get("public_key") or "").strip()
            private_key = str(account.get("private_key") or "").strip()
            if api_key or client_id:
                self.accounts.append(
                    {
                        "index": str(account.get("index") or len(self.accounts) + 1),
                        "api_key": api_key,
                        "client_id": client_id,
                        "private_key": private_key,
                    }
                )

        # Legacy public attributes kept for older call sites/tests.
        self.api_keys: List[str] = [a["api_key"] for a in self.accounts if a.get("api_key")]
        self.api_key = self.api_keys[0] if self.api_keys else None
        self._account_cursor = 0
        self._key_cursor = 0  # backward-compatible name; mirrors _account_cursor
        self._key_lock = asyncio.Lock()

        self.mock_data = None
        if self.mode == ProviderMode.MOCK:
            from .mock_data import MockData

            self.mock_data = MockData()
            logger.info("GMGN Provider initialized in MOCK mode")
        elif self.mode == ProviderMode.ONLINE_READONLY:
            if not HAS_HTTPX:
                raise ImportError("httpx required for online_readonly mode. Install with: pip install httpx")
            if not self.accounts:
                logger.warning("GMGN credentials not set. online_readonly mode may fail if the endpoint requires api_key/client_id.")
            logger.info(
                "GMGN Provider initialized in ONLINE_READONLY mode",
                api_base=self.api_base_url,
                gmgn_account_count=len(self.accounts),
            )
        elif self.mode == ProviderMode.LIVE:
            if not HAS_HTTPX:
                raise ImportError("httpx required for live mode. Install with: pip install httpx")
            if not self.accounts:
                raise ValueError("GMGN_API_KEY_N or GMGN_CLIENT_ID_N/GMGN_PUBLIC_KEY_N required for live mode")
            logger.info(
                "GMGN Provider initialized in LIVE mode",
                api_base=self.api_base_url,
                gmgn_account_count=len(self.accounts),
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
        """Log provider request with masked credentials."""
        safe_request = dict(request_summary or {})

        def _mask(value: Any) -> str:
            key = str(value or "")
            return key[:4] + "..." + key[-4:] if len(key) > 8 else "***"

        for secret_key in (
            "api_key",
            "client_id",
            "private_key",
            "x-api-key",
            "x-route-key",
            "x-apikey",
            "authorization",
            "X-APIKEY",
            "Authorization",
        ):
            if secret_key in safe_request and safe_request[secret_key]:
                safe_request[secret_key] = _mask(safe_request[secret_key])

        for container_key in ("params", "query", "json", "body"):
            nested = safe_request.get(container_key)
            if isinstance(nested, dict):
                nested = dict(nested)
                for secret_key in ("api_key", "client_id", "private_key"):
                    if nested.get(secret_key):
                        nested[secret_key] = _mask(nested[secret_key])
                safe_request[container_key] = nested

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

    async def _next_account(self) -> Tuple[Optional[int], Dict[str, str]]:
        if not self.accounts:
            return None, {}
        async with self._key_lock:
            idx = self._account_cursor % len(self.accounts)
            self._account_cursor += 1
            self._key_cursor = self._account_cursor
            return idx, self.accounts[idx]

    async def _next_api_key(self) -> Tuple[Optional[int], Optional[str]]:
        """Backward-compatible wrapper used by older tests."""
        idx, account = await self._next_account()
        return idx, account.get("api_key") if account else None

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.api_base_url}/{path.lstrip('/')}"

    @staticmethod
    def _retryable_status(status_code: int) -> bool:
        return status_code in (408, 425, 429, 500, 502, 503, 504)

    @staticmethod
    def _auth_headers(account: Dict[str, str]) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        api_key = str(account.get("api_key") or "").strip()
        client_id = str(account.get("client_id") or "").strip()
        if api_key:
            # Different GMGN gateways and examples use different header spellings.
            # Send common aliases; logs mask all of them.
            headers.update(
                {
                    "X-APIKEY": api_key,
                    "X-API-Key": api_key,
                    "x-api-key": api_key,
                    "x-route-key": api_key,
                    "Authorization": f"Bearer {api_key}",
                }
            )
        if client_id:
            headers.update({"X-Client-Id": client_id, "client-id": client_id})
        return headers

    @staticmethod
    def _auth_query(account: Dict[str, str]) -> Dict[str, Any]:
        # Timestamp is harmless and mirrors gmgn-cli style requests.
        # Prefer configured client_id/public_key; only fall back to a request UUID
        # if the account has no client_id at all.
        api_key = str(account.get("api_key") or "").strip()
        client_id = str(account.get("client_id") or "").strip() or str(uuid.uuid4())
        query: Dict[str, Any] = {
            "timestamp": str(int(time.time())),
            "client_id": client_id,
        }
        if api_key:
            query["api_key"] = api_key
        return query

    async def _make_request(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        method: str = "GET",
        query_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not HAS_HTTPX:
            raise ImportError("httpx required for real API calls")

        url = self._build_url(path)
        method = (method or "GET").upper()
        max_attempts = max(1, len(self.accounts))
        last_error: Optional[BaseException] = None
        base_params = dict(params or {})
        extra_query = {k: v for k, v in dict(query_params or {}).items() if v is not None and v != ""}

        async with httpx.AsyncClient(timeout=float(getattr(settings, "GMGN_TIMEOUT_SECONDS", 8.0) or 8.0)) as client:
            for attempt in range(max_attempts):
                key_index, account = await self._next_account()
                headers = self._auth_headers(account)
                auth_query = self._auth_query(account)
                query = {**auth_query, **extra_query}

                request_summary: Dict[str, Any] = {
                    "attempt": attempt + 1,
                    "max_attempts": max_attempts,
                    "method": method,
                    "account_index": (key_index + 1) if key_index is not None else None,
                    "has_api_key": bool(account.get("api_key")) if account else False,
                    "query": dict(query),
                    "params" if method != "POST" else "json": dict(base_params),
                }
                if account.get("api_key"):
                    request_summary["api_key"] = account.get("api_key")
                if account.get("client_id"):
                    request_summary["client_id"] = account.get("client_id")

                start = time.time()
                try:
                    if method == "POST":
                        response = await client.post(url, params=query, json=base_params, headers=headers)
                    else:
                        merged = {**query, **base_params}
                        response = await client.get(url, params=merged, headers=headers)
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
                        if (self._retryable_status(response.status_code) or response.status_code in (401, 403)) and attempt < max_attempts - 1:
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
                    error_msg = "GMGN API timeout"
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
            for key in ("tokens", "token", "list", "rows", "rank", "new", "pump", "complete", "completed"):
                val = payload.get(key)
                if isinstance(val, list):
                    summary[f"data.{key}.count"] = len(val)
        elif isinstance(payload, list):
            summary["data_count"] = len(payload)
        return summary

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_bool(value: Any) -> int:
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (int, float)):
            return 1 if value else 0
        if isinstance(value, str):
            return 1 if value.strip().lower() in {"1", "true", "yes", "y", "renounced", "locked", "burn", "burned"} else 0
        return 0

    @staticmethod
    def _first_present(raw: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
        for key in keys:
            if key in raw and raw.get(key) is not None and raw.get(key) != "":
                return raw.get(key)
        return default

    def _normalized_tax(self, value: Any) -> Optional[float]:
        tax = self._to_float(value)
        if tax is not None and tax > 1:
            tax = tax / 100.0
        return tax

    @staticmethod
    def _has_social(base: Dict[str, Any]) -> int:
        social_count = base.get("social_count")
        try:
            if social_count is not None and float(social_count) > 0:
                return 1
        except Exception:
            pass
        for key in (
            "has_social",
            "has_at_least_one_social",
            "has_twitter_or_telegram",
            "twitter_username",
            "twitter",
            "telegram",
            "website",
        ):
            value = base.get(key)
            if isinstance(value, bool):
                return 1 if value else 0
            if value not in (None, "", 0, "0", False):
                return 1
        return 0

    def _normalize_token_data(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize GMGN token/trenches-style responses to the internal schema."""
        raw = raw or {}
        token_info = raw.get("token") if isinstance(raw.get("token"), dict) else {}
        pool_info = raw.get("pool") if isinstance(raw.get("pool"), dict) else {}
        price_info = raw.get("price") if isinstance(raw.get("price"), dict) else {}
        stat_info = raw.get("stat") if isinstance(raw.get("stat"), dict) else {}
        security_info = raw.get("security") if isinstance(raw.get("security"), dict) else {}
        dev_info = raw.get("dev") if isinstance(raw.get("dev"), dict) else {}
        wallet_tags = raw.get("wallet_tags_stat") if isinstance(raw.get("wallet_tags_stat"), dict) else {}
        base = {**token_info, **pool_info, **price_info, **stat_info, **security_info, **dev_info, **wallet_tags, **raw}

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
        pool_address = self._first_present(base, ("pool_address", "pool", "pair_address", "pool_id", "biggest_pool_address"))
        pool_created_at = self._first_present(
            base,
            ("pool_created_at", "pool_created_timestamp", "creation_timestamp", "created_at", "open_timestamp"),
        )
        token_type = self._first_present(base, ("type", "trench_type", "category"), default="new_creation")
        latest_price_usd = self._to_float(self._first_present(base, ("price_usd", "price", "usd_price", "last_price"), default=raw.get("price")))
        price_sol = self._to_float(self._first_present(base, ("price_sol", "sol_price")))
        liquidity_usd = self._to_float(self._first_present(base, ("liquidity_usd", "liquidity", "usd_liquidity", "liquidity_in_usd", "pool_liquidity_usd")))
        volume_usd = self._to_float(self._first_present(base, ("volume_usd", "volume", "volume_24h", "volume_1h")))
        market_cap = self._to_float(self._first_present(base, ("market_cap", "marketcap", "fdv", "fully_diluted_valuation")))
        launchpad = self._first_present(base, ("launchpad", "launchpad_platform", "platform", "source_platform", "pool_platform"))

        return {
            "token_mint": token_mint,
            "pool_address": pool_address,
            "pool_created_at": pool_created_at,
            "type": token_type,
            "trench_type": token_type,
            "latest_price_usd": latest_price_usd,
            "price_usd": latest_price_usd,
            "price_sol": price_sol,
            "liquidity_usd": liquidity_usd,
            "sol_side_liquidity": self._to_float(self._first_present(base, ("sol_side_liquidity", "base_reserve_value", "base_reserve_usd"))),
            "volume_usd": volume_usd,
            "market_cap": market_cap,
            "symbol": self._first_present(base, ("symbol", "ticker")),
            "name": self._first_present(base, ("name", "token_name")),
            "launchpad": launchpad,
            "platform": launchpad,
            "top_10_holder_rate": self._to_float(self._first_present(base, ("top_10_holder_rate", "top10_holder_rate", "top10_holder_percent", "top_10_rate", "top_10_holder_ratio"))),
            "top1_holder_rate": self._to_float(self._first_present(base, ("top1_holder_rate", "top_1_holder_rate", "creator_balance_rate", "top_holder_rate"))),
            "renounced_mint": self._as_bool(self._first_present(base, ("renounced_mint", "mint_renounced", "mint_authority_renounced", "is_mint_renounced"))),
            "renounced_freeze_account": self._as_bool(self._first_present(base, ("renounced_freeze_account", "freeze_renounced", "freeze_authority_renounced", "is_freeze_renounced"))),
            "rug_ratio": self._to_float(self._first_present(base, ("rug_ratio", "max_rug_ratio", "max_rugged_ratio"))),
            "max_rug_ratio": self._to_float(self._first_present(base, ("max_rug_ratio", "rug_ratio", "max_rugged_ratio"))),
            "entrapment_ratio": self._to_float(self._first_present(base, ("entrapment_ratio", "max_entrapment_ratio"))),
            "max_entrapment_ratio": self._to_float(self._first_present(base, ("max_entrapment_ratio", "entrapment_ratio"))),
            "is_wash_trading": self._as_bool(self._first_present(base, ("is_wash_trading", "wash_trading", "wash_trading_detected"))),
            "rat_trader_amount_rate": self._to_float(self._first_present(base, ("rat_trader_amount_rate", "rat_trader_rate", "top_rat_trader_percentage"))),
            "suspected_insider_hold_rate": self._to_float(self._first_present(base, ("suspected_insider_hold_rate", "insider_hold_rate", "max_insider_ratio"))),
            "max_insider_ratio": self._to_float(self._first_present(base, ("max_insider_ratio", "suspected_insider_hold_rate", "insider_hold_rate"))),
            "bundler_trader_amount_rate": self._to_float(self._first_present(base, ("bundler_trader_amount_rate", "bundler_rate", "max_bundler_rate"))),
            "max_bundler_rate": self._to_float(self._first_present(base, ("max_bundler_rate", "bundler_trader_amount_rate", "bundler_rate"))),
            "fresh_wallet_rate": self._to_float(self._first_present(base, ("fresh_wallet_rate", "fresh_wallets_rate"))),
            "sell_tax": self._normalized_tax(self._first_present(base, ("sell_tax", "sell_tax_rate"), default=0)),
            "buy_tax": self._normalized_tax(self._first_present(base, ("buy_tax", "buy_tax_rate"), default=0)),
            "has_social": self._has_social(base),
            "has_at_least_one_social": self._has_social(base),
            "creator_token_status": self._first_present(base, ("creator_token_status", "creator_status")),
            "dev_team_hold_rate": self._to_float(self._first_present(base, ("dev_team_hold_rate", "dev_hold_rate", "creator_hold_rate"))),
            "dev_token_burn_ratio": self._to_float(self._first_present(base, ("dev_token_burn_ratio", "creator_token_burn_ratio"))),
            "burn_status": self._first_present(base, ("burn_status", "lp_burn_status", "burnt_status")),
            "sniper_count": self._to_float(self._first_present(base, ("sniper_count", "snipers", "sniper_trader_count"))),
            "raw_json": json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
        }

    def normalize_gmgn_trenches(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        return self._normalize_token_data(raw)

    def normalize_gmgn_token_snapshot(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        return self._normalize_token_data(raw)

    def normalize_gmgn_kline(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "open_time": raw.get("open_time") or raw.get("timestamp") or raw.get("time") or raw.get("t"),
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
        - {"data": {"rank": [...]}}
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

        for key in ("tokens", "token", "rank", "list", "rows", "items", "pairs", "results"):
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

    async def fetch_trenches(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        path = settings.GMGN_TRENCHES_PATH
        try:
            if self.mode == ProviderMode.MOCK:
                self.mock_data._maybe_refresh()
                tokens = list(self.mock_data.tokens.values())
                for t in tokens:
                    t["source_mode"] = "MOCK"
                await self._log_request(path, True, params, {"count": len(tokens)}, method="MOCK")
                return tokens

            if self.mode in (ProviderMode.ONLINE_READONLY, ProviderMode.LIVE):
                method = getattr(settings, "GMGN_TRENCHES_METHOD", "POST") or "POST"
                chain = str(params.get("chain") or "sol")
                body = self._build_trenches_v2_body(params)
                # Critical fix: POST body alone is not enough for GMGN trenches;
                # the API gateway expects `chain` as a request-level query param.
                data = await self._make_request(path, body, method=method, query_params={"chain": chain})

                tokens: List[Dict[str, Any]] = []
                for item in self._extract_trench_items(data):
                    normalized = self._normalize_token_data(item)
                    normalized["source_mode"] = "REAL"
                    tokens.append(normalized)

                await self._log_request(path, True, {"query": {"chain": chain}, "json": body}, {"count": len(tokens)}, method=method)
                return tokens

            return []

        except Exception as e:
            await self._log_request(path, False, params, {}, 500, 0, "GMGN_ERROR", str(e))
            logger.error(f"fetch_trenches failed, skipping round: {e}")
            return []

    def _build_trenches_v2_body(self, params: Dict[str, Any]) -> Dict[str, Any]:
        chain = str(params.get("chain") or "sol")
        trench_type = str(params.get("type") or "new_creation")
        platforms_raw = params.get("platforms") or params.get("launchpad_platform") or []
        if isinstance(platforms_raw, str):
            launchpad_platform = [p.strip() for p in platforms_raw.split(",") if p.strip()]
        else:
            launchpad_platform = [str(p).strip() for p in platforms_raw if p]

        if not launchpad_platform and chain == "sol":
            launchpad_platform = [
                "Pump.fun",
                "pump_mayhem",
                "pump_mayhem_agent",
                "pump_agent",
                "letsbonk",
                "bonkers",
                "bags",
                "memoo",
                "liquid",
                "bankr",
                "zora",
                "surge",
                "anoncoin",
                "moonshot_app",
                "Moonshot",
                "wendotdev",
                "heaven",
                "sugar",
                "token_mill",
                "believe",
                "trendsfun",
                "trends_fun",
                "jup_studio",
                "boop",
                "ray_launchpad",
                "meteora_virtual_curve",
                "xstocks",
            ]

        section: Dict[str, Any] = {
            "filters": ["offchain", "onchain"],
            "quote_address_type": [4, 5, 3, 1, 13, 0],
            "launchpad_platform_v2": True,
            "limit": int(params.get("limit") or 80),
        }
        if launchpad_platform:
            section["launchpad_platform"] = launchpad_platform

        min_created = params.get("min_created")
        max_created = params.get("max_created")
        if min_created is not None:
            section["min_created"] = str(min_created)
        if max_created is not None:
            section["max_created"] = str(max_created)

        body: Dict[str, Any] = {"version": "v2", "chain": chain}
        body[trench_type] = section
        return body

    @staticmethod
    def _extract_list_from_response(data: Any, list_keys: Iterable[str]) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if not isinstance(data, dict):
            return []
        payload = data.get("data", data)
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for key in list_keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
            for value in payload.values():
                if isinstance(value, list):
                    dict_items = [x for x in value if isinstance(x, dict)]
                    if dict_items:
                        return dict_items
        return []

    def _normalize_holder_data(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        raw = raw or {}
        amount = self._first_present(raw, ("amount", "balance", "token_amount", "ui_amount"))
        rate = self._first_present(raw, ("top1_holder_rate", "holder_rate", "rate", "percent", "percentage", "balance_rate"))
        return {
            "address": self._first_present(raw, ("address", "wallet", "owner", "holder_address")),
            "addr_type": self._first_present(raw, ("addr_type", "type", "address_type")) or 0,
            "amount": amount,
            "rate": rate,
            "top1_holder_rate": rate,
            "raw_json": json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
        }

    async def fetch_token_snapshot(self, token_mint: str) -> Dict[str, Any]:
        path = f"{settings.GMGN_TOKEN_INFO_PATH}/{token_mint}"
        params = {"chain": "sol", "address": token_mint}
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
                data = await self._make_request(path, params)
                raw = data.get("data", {}) if isinstance(data, dict) else {}
                snapshot = self._normalize_token_info(raw)
                if snapshot:
                    snapshot["source_mode"] = "REAL"
                await self._log_request(path, True, {"token_mint": token_mint}, snapshot)
                return snapshot

            return {}

        except Exception as e:
            await self._log_request(path, False, {"token_mint": token_mint}, {}, 500, 0, "GMGN_ERROR", str(e))
            logger.error(f"fetch_token_snapshot failed token={token_mint}: {e}")
            return {}

    def _normalize_token_info(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        # Token info/pool/security responses are close enough to trenches after
        # flattening that the generic normalizer gives the most complete internal schema.
        return self._normalize_token_data(raw)

    async def fetch_kline(self, token_mint: str, interval: str, limit: int) -> List[Dict[str, Any]]:
        """Fetch kline/candlestick data."""
        path = f"{settings.GMGN_KLINE_PATH}/{token_mint}"
        params = {"chain": "sol", "address": token_mint, "interval": interval, "resolution": interval, "limit": limit}
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
                raw_klines = self._extract_list_from_response(data, ("klines", "list", "rows", "items", "data"))
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

    async def fetch_top_holders(self, token_mint: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetch token top holders for Top1 addr_type=0 concentration checks."""
        path = f"{getattr(settings, 'GMGN_TOKEN_HOLDERS_PATH', '/v1/market/token_top_holders')}/{token_mint}"
        params: Dict[str, Any] = {"chain": "sol", "address": token_mint, "limit": int(limit or 20)}
        try:
            if self.mode == ProviderMode.MOCK:
                return []
            if self.mode in (ProviderMode.ONLINE_READONLY, ProviderMode.LIVE):
                data = await self._make_request(path, params)
                raw_holders = self._extract_list_from_response(data, ("holders", "list", "rows", "items", "data"))
                holders = [self._normalize_holder_data(item) for item in raw_holders]
                await self._log_request(path, True, params, {"count": len(holders)})
                return holders
            return []
        except Exception as e:
            await self._log_request(path, False, params, {}, 500, 0, "GMGN_ERROR", str(e))
            logger.error(f"fetch_top_holders failed token={token_mint}: {e}")
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
                path = f"{settings.GMGN_TOKEN_INFO_PATH}/{token_mint}"
                params = {"chain": "sol", "address": token_mint}
                data = await self._make_request(path, params)
                raw = data.get("data", {}) if isinstance(data, dict) else {}
                normalized = self._normalize_token_info(raw)
                return {
                    "price": normalized.get("price_usd") or normalized.get("latest_price_usd") or 0.0,
                    "price_usd": normalized.get("price_usd") or normalized.get("latest_price_usd"),
                    "price_sol": normalized.get("price_sol") or 0.0,
                    "sol_side_liquidity": normalized.get("sol_side_liquidity") or normalized.get("liquidity_usd") or 0,
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
