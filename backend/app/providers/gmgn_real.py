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
        self.accounts: List[Dict[str, str]] = []
        for account in settings.get_gmgn_accounts():
            api_key = str(account.get("api_key") or "").strip()
            client_id = str(account.get("client_id") or account.get("public_key") or "").strip()
            private_key = str(account.get("private_key") or "").strip()
            if api_key or client_id:
                self.accounts.append({
                    "index": str(account.get("index") or len(self.accounts) + 1),
                    "api_key": api_key,
                    "client_id": client_id,
                    "private_key": private_key,
                })
        # Legacy public attribute kept for older call sites/tests.
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
        """Log provider request with masked API key."""
        safe_request = dict(request_summary or {})
        for secret_key in ("api_key", "client_id", "x-api-key", "x-route-key", "authorization"):
            if secret_key in safe_request and safe_request[secret_key]:
                key = str(safe_request[secret_key])
                safe_request[secret_key] = key[:4] + "..." + key[-4:] if len(key) > 8 else "***"
        for container_key in ("params", "json"):
            nested = safe_request.get(container_key)
            if isinstance(nested, dict):
                nested = dict(nested)
                for secret_key in ("api_key", "client_id"):
                    if nested.get(secret_key):
                        key = str(nested[secret_key])
                        nested[secret_key] = key[:4] + "..." + key[-4:] if len(key) > 8 else "***"
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
        return {
            "X-APIKEY": account.get("api_key") or "",
            "Content-Type": "application/json",
        }

    async def _make_request(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        method: str = "GET",
    ) -> Dict[str, Any]:
        if not HAS_HTTPX:
            raise ImportError("httpx required for real API calls")

        url = self._build_url(path)
        method = (method or "GET").upper()
        max_attempts = max(1, len(self.accounts))
        last_error: Optional[BaseException] = None
        base_params = dict(params or {})

        async with httpx.AsyncClient(timeout=float(getattr(settings, "GMGN_TIMEOUT_SECONDS", 8.0) or 8.0)) as client:
            for attempt in range(max_attempts):
                key_index, account = await self._next_account()
                headers = self._auth_headers(account)

                # timestamp + random client_id per request (matches gmgn-cli behaviour)
                auth_query = {
                    "timestamp": str(int(time.time())),
                    "client_id": str(uuid.uuid4()),
                }
                request_summary: Dict[str, Any] = {
                    "attempt": attempt + 1,
                    "max_attempts": max_attempts,
                    "method": method,
                    "account_index": (key_index + 1) if key_index is not None else None,
                    "has_api_key": bool(account.get("api_key")) if account else False,
                    "params" if method != "POST" else "json": dict(base_params),
                }
                if account.get("api_key"):
                    request_summary["api_key"] = account.get("api_key")

                start = time.time()
                try:
                    if method == "POST":
                        response = await client.post(url, params=auth_query, json=base_params, headers=headers)
                    else:
                        merged = {**auth_query, **base_params}
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
            for key in ("tokens", "list", "rows", "new", "pump", "complete", "completed"):
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
                body = self._build_trenches_v2_body(params)
                data = await self._make_request(path, body, method=method)

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

    def _build_trenches_v2_body(self, params: Dict[str, Any]) -> Dict[str, Any]:
        chain = str(params.get("chain") or "sol")
        trench_type = str(params.get("type") or "new_creation")
        platforms_raw = params.get("platforms") or params.get("launchpad_platform") or []
        if isinstance(platforms_raw, str):
            launchpad_platform = [p.strip() for p in platforms_raw.split(",") if p.strip()]
        else:
            launchpad_platform = [str(p).strip() for p in platforms_raw if p]

        if not launchpad_platform:
            sol_platforms = [
                "Pump.fun", "pump_mayhem", "pump_mayhem_agent", "pump_agent",
                "letsbonk", "bonkers", "bags", "memoo", "liquid", "bankr", "zora",
                "surge", "anoncoin", "moonshot_app", "wendotdev", "heaven", "sugar",
                "token_mill", "believe", "trendsfun", "trends_fun", "jup_studio",
                "Moonshot", "boop", "ray_launchpad", "meteora_virtual_curve", "xstocks",
            ]
            launchpad_platform = sol_platforms

        section: Dict[str, Any] = {
            "filters": ["offchain", "onchain"],
            "launchpad_platform": launchpad_platform,
            "quote_address_type": [4, 5, 3, 1, 13, 0],
            "launchpad_platform_v2": True,
            "limit": 80,
        }
        min_created = params.get("min_created")
        max_created = params.get("max_created")
        if min_created is not None:
            section["min_created"] = str(min_created)
        if max_created is not None:
            section["max_created"] = str(max_created)
        body: Dict[str, Any] = {"version": "v2"}
        body[trench_type] = section
        if not body.get("chain"):
            body["chain"] = chain
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
                params = {"chain": "sol", "address": token_mint}
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
        raw = raw or {}
        price_obj = raw.get("price") if isinstance(raw.get("price"), dict) else {}
        pool_obj = raw.get("pool") if isinstance(raw.get("pool"), dict) else {}
        dev_obj = raw.get("dev") if isinstance(raw.get("dev"), dict) else {}
        stat_obj = raw.get("stat") if isinstance(raw.get("stat"), dict) else {}
        wallet_tags = raw.get("wallet_tags_stat") if isinstance(raw.get("wallet_tags_stat"), dict) else {}

        token_mint = self._first_present(raw, ("address", "token_address", "token_mint", "mint"))

        # Security fields are embedded differently; also try token security endpoint fields
        buy_tax = _to_float(raw.get("buy_tax") or "0") or 0.0
        sell_tax = _to_float(raw.get("sell_tax") or "0") or 0.0
        if buy_tax > 1:
            buy_tax = buy_tax / 100.0
        if sell_tax > 1:
            sell_tax = sell_tax / 100.0

        return {
            "token_mint": token_mint,
            "pool_address": self._first_present(raw, ("biggest_pool_address", "pool_address")),
            "pool_created_at": self._first_present(raw, ("open_timestamp", "creation_timestamp", "pool_creation_timestamp")),
            "latest_price_usd": _to_float(self._first_present(price_obj, ("price", "usd_price"), default=raw.get("price"))),
            "price_usd": _to_float(self._first_present(price_obj, ("price", "usd_price"), default=raw.get("price"))),
            "liquidity_usd": _to_float(self._first_present(pool_obj, ("liquidity", "usd_liquidity"), default=raw.get("liquidity"))),
            "volume_usd": _to_float(self._first_present(price_obj, ("volume_1h", "volume_24h"), default=raw.get("volume"))),
            "market_cap": _to_float(raw.get("market_cap")),
            "symbol": self._first_present(raw, ("symbol", "ticker")),
            "name": self._first_present(raw, ("name", "token_name")),
            "launchpad": self._first_present(raw, ("launchpad", "launchpad_platform")),
            "type": "new_creation",  # trenches data carries this; token/info does not
            "top_10_holder_rate": _to_float(self._first_present(dev_obj, ("top_10_holder_rate",), default=raw.get("top_10_holder_rate"))),
            "top1_holder_rate": None,
            "renounced_mint": self._as_bool(raw.get("renounced_mint")),
            "renounced_freeze_account": self._as_bool(raw.get("renounced_freeze_account")),
            "is_wash_trading": 0,
            "rug_ratio": _to_float(raw.get("rug_ratio")),
            "rat_trader_amount_rate": _to_float(stat_obj.get("top_rat_trader_percentage") or raw.get("rat_trader_amount_rate")),
            "suspected_insider_hold_rate": _to_float(raw.get("suspected_insider_hold_rate")),
            "bundler_trader_amount_rate": _to_float(raw.get("bundler_rate") or raw.get("bundler_trader_amount_rate")),
            "fresh_wallet_rate": _to_float(stat_obj.get("fresh_wallet_rate") or raw.get("fresh_wallet_rate")),
            "sell_tax": sell_tax,
            "buy_tax": buy_tax,
            "has_social": 0,
            "creator_token_status": self._first_present(raw, ("creator_token_status",)),
            "dev_team_hold_rate": _to_float(stat_obj.get("dev_team_hold_rate") or raw.get("dev_team_hold_rate")),
            "burn_status": self._first_present(raw, ("burn_status",)),
            "sniper_count": _to_float(raw.get("sniper_count")),
            "sol_side_liquidity": _to_float(self._first_present(pool_obj, ("base_reserve_value", "sol_side_liquidity"))),
            "raw_json": json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
        }

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

    async def fetch_top_holders(self, token_mint: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetch token top holders for Top1 addr_type=0 concentration checks."""
        path = f"{getattr(settings, 'GMGN_TOKEN_HOLDERS_PATH', '/v1/market/token_top_holders')}/{token_mint}"
        params: Dict[str, Any] = {"chain": "sol", "address": token_mint, "limit": int(limit or 20)}
        try:
            if self.mode == ProviderMode.MOCK:
                return []
            if self.mode in (ProviderMode.ONLINE_READONLY, ProviderMode.LIVE):
                data = await self._make_request(path, params)
                raw_holders = self._extract_list_from_response(data, ("holders", "list", "rows", "data"))
                holders = [self._normalize_holder_data(item) for item in raw_holders]
                await self._log_request(path, True, params, {"count": len(holders)})
                return holders
            return []
        except Exception as e:
            await self._log_request(path, False, params, {}, 500, 0, "GMGN_ERROR", str(e))
            logger.error(f"fetch_top_holders failed token={token_mint}: {e}")
            return []

    async def fetch_latest_price(self, token_mint: str) -> Dict[str, Any]:
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
                price_obj = raw.get("price") if isinstance(raw.get("price"), dict) else {}
                pool_obj = raw.get("pool") if isinstance(raw.get("pool"), dict) else {}
                price_usd = _to_float(self._first_present(price_obj, ("price", "usd_price"), default=raw.get("price"))) or 0.0
                price_sol = _to_float(self._first_present(raw, ("price_sol", "sol_price",))) or 0.0
                liquidity_usd = _to_float(self._first_present(pool_obj, ("liquidity",), default=raw.get("liquidity"))) or 0.0
                sol_side_liq = _to_float(self._first_present(pool_obj, ("base_reserve_value", "sol_side_liquidity"))) or 0.0
                decimals = (raw.get("decimals") or 9)
                return {
                    "price": price_usd,
                    "price_usd": price_usd,
                    "price_sol": price_sol,
                    "liquidity_usd": liquidity_usd,
                    "sol_side_liquidity": sol_side_liq,
                    "token_decimals": decimals if isinstance(decimals, int) else int(decimals),
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
