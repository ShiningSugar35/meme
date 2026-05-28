"""GMGN market-data provider.

Key fixes in this replacement:
- K-line path uses the canonical settings.GMGN_KLINE_PATH / get_gmgn_kline_path().
- It never references the removed/legacy field GMGN_TOKEN_KLINE_PATH directly.
- K-line/token-info endpoints use query parameters (chain/address/limit) rather
  than appending the token mint to the path, while still accepting legacy payload
  shapes returned by GMGN.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import httpx
    HAS_HTTPX = True
except Exception:  # pragma: no cover
    httpx = None
    HAS_HTTPX = False

from ..config import ProviderMode, settings
from ..db.repositories import Repositories
from ..logging_config import logger
from .base import MarketDataProvider
from .mock_data import MockData
from .rate_limiter import get_rate_limiter, _endpoint_weight


class GMGNAPIError(Exception):
    def __init__(self, message: str, *, status_code: Optional[int] = None, path: Optional[str] = None, method: str = "GET", retryable: Optional[bool] = None):
        super().__init__(message)
        self.status_code = status_code
        self.path = path
        self.method = method
        self.retryable = retryable


class GMGNProvider(MarketDataProvider):
    def __init__(self, repo: Repositories, mode: Optional[ProviderMode] = None):
        self.repo = repo
        self.mode = mode or settings.get_provider_mode()
        self.api_base_url = (settings.GMGN_API_BASE_URL or "").rstrip("/")
        self.credentials = settings.get_gmgn_credentials()
        self.api_keys = [c.get("api_key", "") for c in self.credentials if c.get("api_key")]
        self.client_ids = [c.get("client_id", "") for c in self.credentials if c.get("client_id")]
        self._key_cursor = 0
        self.mock_data = MockData()
        self.rate_limiter = get_rate_limiter(credential_count=max(len(self.credentials or []), 12))

        if self.mode in (ProviderMode.ONLINE_READONLY, ProviderMode.LIVE):
            if not HAS_HTTPX:
                raise ImportError("httpx required for live/online_readonly GMGN mode. Install with: pip install httpx")
            if not self.api_base_url:
                raise ValueError("GMGN_API_BASE_URL required for live/online_readonly mode")
            if not (self.credentials or self.api_keys or self.client_ids):
                raise ValueError("GMGN_API_KEY_N or GMGN_CLIENT_ID_N required for live/online_readonly mode")
            logger.info("GMGN Provider initialized in real mode", api_base=self.api_base_url, credential_count=len(self.credentials))

    async def _log_request(
        self,
        endpoint: str,
        ok: bool,
        request_summary: Dict[str, Any] | None,
        response_summary: Dict[str, Any] | None,
        status_code: Optional[int] = 200,
        latency_ms: Optional[int] = 0,
        error_code: Optional[str] = None,
        error_summary: Optional[str] = None,
        method: str = "GET",
    ) -> None:
        def mask(v: Any) -> Any:
            s = str(v or "")
            if not s:
                return s
            return s[:4] + "..." + s[-4:] if len(s) > 8 else "***"

        req = dict(request_summary or {})
        for k in ("api_key", "x-api-key", "x-route-key", "client_id", "private_key"):
            if k in req:
                req[k] = mask(req[k])
        if "credential_slot" in req:
            req["credential_slot"] = int(req["credential_slot"])
        try:
            await self.repo.append_provider_request(
                "GMGN",
                endpoint,
                method.upper(),
                status_code,
                latency_ms,
                bool(ok),
                error_code,
                error_summary,
                json.dumps(req, ensure_ascii=False, default=str),
                json.dumps(response_summary or {}, ensure_ascii=False, default=str),
            )
        except Exception:
            pass

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not self.api_base_url:
            raise GMGNAPIError("GMGN_API_BASE_URL is not configured", path=path, retryable=False)
        return f"{self.api_base_url}/{path.lstrip('/')}"

    @staticmethod
    def _retryable_status(status_code: int) -> bool:
        return status_code in (401, 403, 408, 425, 429, 500, 502, 503, 504)

    def _next_credential(self) -> Dict[str, Any]:
        creds = self.credentials or [{"api_key": k, "client_id": ""} for k in self.api_keys] or [{"api_key": "", "client_id": c} for c in self.client_ids]
        if not creds:
            return {}
        item = creds[self._key_cursor % len(creds)]
        self._key_cursor += 1
        return item

    def _credential_by_slot(self, slot: int) -> Optional[Dict[str, Any]]:
        creds = self.credentials or [{"api_key": k, "client_id": ""} for k in self.api_keys] or [{"api_key": "", "client_id": c} for c in self.client_ids]
        if not creds or slot < 0 or slot >= len(creds):
            return None
        return creds[slot]

    @staticmethod
    def _compact_response_summary(data: Any) -> Dict[str, Any]:
        if isinstance(data, dict):
            summary: Dict[str, Any] = {"keys": list(data.keys())[:30]}
            inner = data.get("data")
            if isinstance(inner, list):
                summary["data_count"] = len(inner)
            elif isinstance(inner, dict):
                summary["data_keys"] = list(inner.keys())[:30]
                for k in ("items", "list", "rows", "klines", "holders"):
                    if isinstance(inner.get(k), list):
                        summary[f"{k}_count"] = len(inner[k])
            return summary
        if isinstance(data, list):
            return {"list_count": len(data)}
        return {"type": type(data).__name__}

    async def _make_request(self, path: str, params: Optional[Dict[str, Any]] = None, *, method: str = "GET", json_body: Optional[Dict[str, Any]] = None, credential_slot: Optional[int] = None) -> Dict[str, Any]:
        if not HAS_HTTPX:
            raise ImportError("httpx required for real API calls")
        method = (method or "GET").upper()
        cleaned = {k: v for k, v in dict(params or {}).items() if v is not None and v != ""}
        url = self._build_url(path)
        creds = self.credentials or [{"api_key": k, "client_id": ""} for k in self.api_keys] or [{"api_key": "", "client_id": c} for c in self.client_ids]
        creds_available = len(creds or [])
        if credential_slot is not None:
            cred = self._credential_by_slot(credential_slot)
            if not cred:
                raise GMGNAPIError(f"Invalid credential_slot {credential_slot} (available: 0-{creds_available-1})", path=path, method=method, retryable=False)
            attempts_to_make = [(credential_slot, cred)]
        else:
            attempts_to_make = [(i % max(creds_available, 1), creds[i % max(creds_available, 1)] if creds_available else {}) for i in range(max(1, creds_available))]

        last_exc: Optional[BaseException] = None
        last_status: Optional[int] = None

        for slot, cred in attempts_to_make:
            if not cred:
                continue
            api_key = cred.get("api_key") or ""

            # Rate limiter check
            if slot < 999:
                ok_to_proceed = await self.rate_limiter.acquire(slot, path)
                if not ok_to_proceed:
                    cred_meta = self.rate_limiter.slots.get(slot)
                    block_reason = "slot_cooldown" if (cred_meta and cred_meta.is_cooldown()) else "bucket_empty"
                    self.rate_limiter.slots[slot].rate_limited_count += 1
                    logged_block = {
                        "credential_slot": slot,
                        "credential_role": cred_meta.role if cred_meta else "unknown",
                        "endpoint_weight": _endpoint_weight(path),
                        "block_reason": block_reason,
                        "path": path,
                    }
                    await self._log_request(
                        path, False, logged_block, {},
                        status_code=429, latency_ms=0,
                        error_code="RATE_LIMIT_LOCAL",
                        error_summary=f"local rate limiter: {block_reason}",
                        method=method,
                    )
                    msg = f"slot {slot} local rate limited ({block_reason}) for {path}"
                    if credential_slot is not None:
                        raise GMGNAPIError(msg, path=path, method=method, retryable=True, status_code=429)
                    last_exc = GMGNAPIError(msg, path=path, method=method, retryable=True, status_code=429)
                    continue

            started = time.perf_counter()

            auth_query = {"timestamp": str(int(time.time())), "client_id": str(uuid.uuid4())}
            headers = {"X-APIKEY": api_key, "Content-Type": "application/json"}
            request_params = {**cleaned, **auth_query}
            logged_request = dict(request_params)
            logged_request["credential_slot"] = slot
            cred_meta = self.rate_limiter.slots.get(slot)
            logged_request["credential_role"] = cred_meta.role if cred_meta else "unknown"
            if credential_slot is not None:
                logged_request["credential_role"] += " (explicit)"
            logged_request["endpoint_weight"] = _endpoint_weight(path)
            logged_request["api_key"] = "***"
            tag = request_params.get("tag") or (json_body or {}).get("tag") or (cleaned or {}).get("tag")
            if tag:
                logged_request["feature_stage"] = "smart_degen_filter" if str(tag) == "smart_degen" else str(tag)

            try:
                timeout = float(getattr(settings, "GMGN_TIMEOUT_SECONDS", 8.0) or 8.0)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    if method == "POST":
                        post_params = dict(auth_query)
                        if json_body is not None:
                            post_body = json_body
                            if "chain" in cleaned:
                                post_params["chain"] = cleaned["chain"]
                        else:
                            post_body = dict(cleaned)
                            if "chain" in post_body:
                                post_params["chain"] = post_body.pop("chain")
                        resp = await client.post(url, params=post_params, json=post_body, headers=headers)
                        logged_request["body_summary"] = {
                            "new_creation": {
                                k: v for k, v in post_body.get("new_creation", {}).items()
                                if k in ("launchpad_platform", "launchpad_platform_v2", "min_created", "max_created", "limit", "filters", "quote_address_type")
                            }
                        } if isinstance(post_body.get("new_creation"), dict) else {"body_type": type(post_body).__name__}
                    else:
                        resp = await client.get(url, params=request_params, headers=headers)
                latency = int((time.perf_counter() - started) * 1000)

                try:
                    data = resp.json()
                except Exception:
                    data = {"raw_text": resp.text[:2000]}

                last_status = resp.status_code
                if resp.status_code >= 400:
                    if resp.status_code == 429 and slot < 999:
                        cooldown_s = await self.rate_limiter.report_429(slot, path, headers=dict(resp.headers), body=data if isinstance(data, dict) else None)
                        logged_request["rate_limit_reset"] = True
                        logged_request["cooldown_until"] = cooldown_s
                    retryable = self._retryable_status(resp.status_code)
                    await self._log_request(path, False, logged_request, self._compact_response_summary(data), resp.status_code, latency, "HTTP_ERROR", str(data)[:500], method)
                    await self.rate_limiter.report_failure(slot)
                    err = GMGNAPIError(f"GMGN HTTP {resp.status_code}: {str(data)[:500]}", status_code=resp.status_code, path=path, method=method, retryable=retryable)
                    last_exc = err
                    if credential_slot is not None:
                        raise err
                    if retryable:
                        continue
                    raise err

                await self.rate_limiter.report_success(slot)
                await self._log_request(path, True, logged_request, self._compact_response_summary(data), resp.status_code, latency, method=method)
                return data if isinstance(data, dict) else {"data": data}
            except GMGNAPIError:
                raise
            except Exception as exc:
                latency = int((time.perf_counter() - started) * 1000)
                last_exc = exc
                if slot < 999:
                    await self.rate_limiter.report_failure(slot)
                await self._log_request(path, False, logged_request, {}, None, latency, "REQUEST_ERROR", str(exc) or repr(exc), method)
                if credential_slot is not None:
                    raise GMGNAPIError(f"GMGN request failed (slot={credential_slot}): {exc}", path=path, method=method, retryable=True)
                continue

        if isinstance(last_exc, GMGNAPIError):
            raise last_exc

        detail = str(last_exc) if last_exc else 'unknown'
        if not detail or detail.strip() == '':
            detail = repr(last_exc) if last_exc else 'unknown'
        if not detail or detail.strip() == '':
            detail = type(last_exc).__name__ if last_exc else 'unknown'
        raise GMGNAPIError(
            f"GMGN request failed after {creds_available} attempt(s) "
            f"(creds={creds_available}, last_status={last_status}): {detail}",
            path=path, method=method, retryable=True,
        )

    @staticmethod
    def _first_present(data: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
        for key in keys:
            value = data.get(key)
            if value is not None and value != "":
                return value
        return default

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _unwrap_data(data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        cur: Any = data.get("data", data)
        # GMGN variants sometimes wrap useful payload one more level.
        if isinstance(cur, dict):
            for key in ("token", "info", "pool", "security", "result"):
                if isinstance(cur.get(key), dict):
                    return cur[key]
        return cur

    @staticmethod
    def _extract_items(data: Any, preferred_keys: Iterable[str] = ("items", "list", "rows", "tokens", "data")) -> List[Any]:
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        for key in preferred_keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = GMGNProvider._extract_items(value, preferred_keys)
                if nested:
                    return nested
        inner = data.get("data")
        if inner is not data:
            return GMGNProvider._extract_items(inner, preferred_keys)
        return []

    @classmethod
    def _normalize_token_data(cls, raw: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, Any] = {
            "token_mint": cls._first_present(raw, ["token_mint", "token_address", "address", "mint", "base_address"]),
            "pool_address": cls._first_present(raw, ["pool_address", "pair_address", "pool", "pair", "address_pair"]),
            "pool_created_at": cls._first_present(raw, ["pool_created_at", "creation_time", "created_at", "open_time", "launch_time", "created_timestamp"]),
            "type": cls._first_present(raw, ["type", "trench_type", "category"]),
            "launchpad": cls._first_present(raw, ["launchpad_platform", "launchpad", "platform", "source_platform", "pool_platform"]),
            "platform": cls._first_present(raw, ["launchpad_platform", "platform", "launchpad", "source_platform", "pool_platform"]),
            "symbol": cls._first_present(raw, ["symbol", "base_symbol"]),
            "name": cls._first_present(raw, ["name", "base_name"]),
            "liquidity_usd": cls._to_float(cls._first_present(raw, ["liquidity_usd", "liquidity", "pool_liquidity_usd", "reserve_usd"])),
            "sol_side_liquidity": cls._to_float(cls._first_present(raw, ["sol_side_liquidity", "sol_liquidity", "quote_reserve", "quote_liquidity"])),
            "volume_usd": cls._to_float(cls._first_present(raw, ["volume_usd", "volume", "volume_24h", "volume_h24", "volume_1h"])),
            "market_cap": cls._to_float(cls._first_present(raw, ["market_cap", "marketcap", "fdv", "fully_diluted_valuation", "usd_market_cap"])),
            "price_usd": cls._to_float(cls._first_present(raw, ["price_usd", "price", "usd_price"])),
            "price_sol": cls._to_float(cls._first_present(raw, ["price_sol", "sol_price", "native_price"])),
            "top_10_holder_rate": cls._to_float(cls._first_present(raw, ["top_10_holder_rate", "top10_holder_rate", "top10_holder_percent", "top_10_rate"])),
            "top1_holder_rate": cls._to_float(cls._first_present(raw, ["top1_holder_rate", "top_1_holder_rate", "top_holder_rate"])),
            "renounced_mint": cls._first_present(raw, ["renounced_mint", "mint_renounced", "is_mint_renounced"]),
            "renounced_freeze_account": cls._first_present(raw, ["renounced_freeze_account", "freeze_renounced", "is_freeze_renounced", "freeze_authority_renounced"]),
            "max_rug_ratio": cls._to_float(cls._first_present(raw, ["max_rug_ratio", "rug_ratio", "max_rugged_ratio"])),
            "max_insider_ratio": cls._to_float(cls._first_present(raw, ["max_insider_ratio", "insider_ratio"])),
            "max_entrapment_ratio": cls._to_float(cls._first_present(raw, ["max_entrapment_ratio", "entrapment_ratio"])),
            "is_wash_trading": cls._first_present(raw, ["is_wash_trading", "wash_trading", "wash_trading_detected"]),
            "rat_trader_amount_rate": cls._to_float(cls._first_present(raw, ["rat_trader_amount_rate", "rat_trader_rate"])),
            "suspected_insider_hold_rate": cls._to_float(cls._first_present(raw, ["suspected_insider_hold_rate", "insider_hold_rate"])),
            "max_bundler_rate": cls._to_float(cls._first_present(raw, ["max_bundler_rate", "bundler_rate", "bundler_trader_amount_rate"])),
            "fresh_wallet_rate": cls._to_float(cls._first_present(raw, ["fresh_wallet_rate", "fresh_wallets_rate"])),
            "sell_tax": cls._to_float(cls._first_present(raw, ["sell_tax", "sell_tax_rate"])),
            "has_social": cls._first_present(raw, ["has_social", "has_at_least_one_social", "has_twitter_or_telegram"]),
            "creator_token_status": cls._first_present(raw, ["creator_token_status", "creator_status"]),
            "dev_team_hold_rate": cls._to_float(cls._first_present(raw, ["dev_team_hold_rate", "dev_hold_rate", "creator_hold_rate"])),
            "dev_token_burn_ratio": cls._to_float(cls._first_present(raw, ["dev_token_burn_ratio", "burn_ratio", "lp_burn_ratio"])),
            "burn_status": cls._first_present(raw, ["burn_status", "lp_burn_status", "burnt_status"]),
            "sniper_count": cls._to_float(cls._first_present(raw, ["sniper_count", "snipers", "sniper_trader_count"])),
        }
        # Keep explicit falsy values; drop only None/empty for DB columns except raw_json.
        out = {k: v for k, v in out.items() if v is not None and v != ""}
        out["raw_json"] = json.dumps(raw, ensure_ascii=False, default=str)
        return out

    @classmethod
    def normalize_gmgn_kline(cls, raw: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        ts = cls._first_present(raw, ["open_time", "time", "timestamp", "t"])
        if isinstance(ts, (int, float)):
            # GMGN may return seconds or milliseconds.
            sec = ts / 1000 if ts > 10_000_000_000 else ts
            try:
                ts = datetime.fromtimestamp(float(sec), timezone.utc).isoformat()
            except Exception:
                ts = str(ts)
        return {
            "open_time": str(ts or ""),
            "open": cls._to_float(cls._first_present(raw, ["open", "o"])),
            "high": cls._to_float(cls._first_present(raw, ["high", "h"])),
            "low": cls._to_float(cls._first_present(raw, ["low", "l"])),
            "close": cls._to_float(cls._first_present(raw, ["close", "c"])),
            "buy_volume": cls._to_float(cls._first_present(raw, ["buy_volume", "buyVolume", "buy_vol", "buyVol"])),
            "sell_volume": cls._to_float(cls._first_present(raw, ["sell_volume", "sellVolume", "sell_vol", "sellVol"])),
            "volume_usd": cls._to_float(cls._first_present(raw, ["volume_usd", "volume", "vol_usd", "v"])),
            "raw_json": json.dumps(raw, ensure_ascii=False, default=str),
        }

    @staticmethod
    def _extract_v2_trench_items(data: Any) -> List[Dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        inner = data.get("data")
        if not isinstance(inner, dict):
            return []
        items: List[Dict[str, Any]] = []
        for key in ("new_creation", "pump", "completed"):
            category = inner.get(key)
            if isinstance(category, list):
                for item in category:
                    if isinstance(item, dict):
                        items.append(item)
        return items

    async def fetch_trenches(self, params: Dict[str, Any], credential_slot: Optional[int] = None) -> List[Dict[str, Any]]:
        path = settings.GMGN_TRENCHES_PATH
        if self.mode == ProviderMode.MOCK:
            self.mock_data._maybe_refresh()
            tokens = list(self.mock_data.tokens.values())
            for t in tokens:
                t["source_mode"] = "MOCK"
            await self._log_request(path, True, params, {"count": len(tokens)}, method="MOCK")
            return tokens

        chain = params.get("chain", "sol")
        min_created = params.get("min_created")
        max_created = params.get("max_created")
        platforms = params.get("platforms", [])

        body: Dict[str, Any] = {
            "version": "v2",
            "new_creation": {
                "filters": ["offchain", "onchain"],
                "launchpad_platform_v2": True,
                "quote_address_type": [4, 5, 3, 1, 13, 0],
                "limit": 80,
                "renounced_mint": 1,
                "renounced_freeze_account": 1,
            }
        }
        if platforms:
            body["new_creation"]["launchpad_platform"] = list(platforms) if isinstance(platforms, list) else [str(platforms)]
        if min_created is not None:
            body["new_creation"]["min_created"] = f"{int(min_created)}s"
        if max_created is not None:
            body["new_creation"]["max_created"] = f"{int(max_created)}s"

        data = await self._make_request(path, {"chain": chain}, method="POST", json_body=body, credential_slot=credential_slot)
        items = self._extract_v2_trench_items(data)
        out: List[Dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                normalized = self._normalize_token_data(item)
                if normalized:
                    normalized["source_mode"] = "REAL"
                    normalized["type"] = "new_creation"
                    out.append(normalized)
        return out

    async def fetch_token_snapshot(self, token_mint: str) -> Dict[str, Any]:
        if self.mode == ProviderMode.MOCK:
            self.mock_data._maybe_refresh()
            token = dict(self.mock_data.tokens.get(token_mint) or {})
            if token:
                token["source_mode"] = "MOCK"
                await self._log_request("mock/token_snapshot", True, {"token_mint": token_mint}, {"token": token_mint}, method="MOCK")
            return token

        params = {"chain": "sol", "address": token_mint}
        raw_bundle: Dict[str, Any] = {}
        # Main info is required. Security/pool enrichments are best-effort because
        # some GMGN plans expose only a subset of endpoints.
        info_path = getattr(settings, "GMGN_TOKEN_INFO_PATH", None) or settings.GMGN_TOKEN_PRICE_PATH
        info = await self._make_request(info_path, params, method="GET")
        raw_bundle["token_info"] = info
        merged: Dict[str, Any] = {}
        if isinstance(self._unwrap_data(info), dict):
            merged.update(self._unwrap_data(info))

        for label, path in (
            ("security", getattr(settings, "GMGN_TOKEN_SECURITY_PATH", "")),
            ("pool_info", getattr(settings, "GMGN_TOKEN_POOL_INFO_PATH", "")),
        ):
            if not path:
                continue
            try:
                data = await self._make_request(path, params, method="GET")
                raw_bundle[label] = data
                val = self._unwrap_data(data)
                if isinstance(val, dict):
                    merged.update(val)
            except GMGNAPIError as exc:
                raw_bundle[label] = {"error": str(exc), "status_code": exc.status_code, "path": exc.path}
            except Exception as exc:
                raw_bundle[label] = {"error": str(exc)}

        snapshot = self._normalize_token_data(merged)
        snapshot.setdefault("token_mint", token_mint)
        snapshot["source_mode"] = "REAL"
        snapshot["raw_json"] = json.dumps(raw_bundle, ensure_ascii=False, default=str)
        return snapshot

    async def fetch_kline(self, token_mint: str, interval: str, limit: int, from_ts: Optional[int] = None, to_ts: Optional[int] = None, credential_slot: Optional[int] = None) -> List[Dict[str, Any]]:
        if self.mode == ProviderMode.MOCK:
            self.mock_data._maybe_refresh()
            klines = [dict(k) for k in self.mock_data.klines.get(token_mint, [])]
            for item in klines:
                item["source_mode"] = "MOCK"
            await self._log_request("mock/kline", True, {"token_mint": token_mint, "interval": interval, "limit": limit}, {"count": len(klines)}, method="MOCK")
            return klines

        path = settings.get_gmgn_kline_path() if hasattr(settings, "get_gmgn_kline_path") else (getattr(settings, "GMGN_KLINE_PATH", None) or "/v1/market/token_kline")
        params: Dict[str, Any] = {"chain": "sol", "address": token_mint, "resolution": interval, "limit": int(limit)}
        if from_ts is not None:
            params["from"] = int(from_ts)
        if to_ts is not None:
            params["to"] = int(to_ts)
        data = await self._make_request(path, params, method="GET", credential_slot=credential_slot)
        root = data.get("data", data) if isinstance(data, dict) else data
        raw_klines = self._extract_items(root, ("klines", "list", "items", "rows", "data"))
        klines: List[Dict[str, Any]] = []
        for item in raw_klines:
            if isinstance(item, dict):
                normalized = self.normalize_gmgn_kline(item)
                normalized["source_mode"] = "REAL"
                klines.append(normalized)
        return klines

    async def fetch_latest_price(self, token_mint: str, credential_slot: Optional[int] = None) -> Dict[str, Any]:
        if self.mode == ProviderMode.MOCK:
            self.mock_data._maybe_refresh()
            info = self.mock_data.latest.get(token_mint)
            if not info:
                raise GMGNAPIError("token not found", status_code=404, path="mock/latest", retryable=False)
            info["calls"] = int(info.get("calls", 0)) + 1
            price = info.get("price_usd") or info.get("price") or 0.0
            await self._log_request("mock/latest", True, {"token_mint": token_mint}, {"price": price}, method="MOCK")
            return {
                "price": price,
                "price_usd": price,
                "price_sol": info.get("price_sol") or info.get("sol_price") or 0.0,
                "liquidity_usd": info.get("liquidity_usd") or 0.0,
                "sol_side_liquidity": info.get("sol_side_liquidity") or info.get("sol_liquidity") or 0.0,
                "market_cap": info.get("market_cap"),
                "source_mode": "MOCK",
            }

        path = getattr(settings, "GMGN_TOKEN_PRICE_PATH", None) or getattr(settings, "GMGN_TOKEN_INFO_PATH", "/v1/token/info")
        params = {"chain": "sol", "address": token_mint}
        data = await self._make_request(path, params, method="GET", credential_slot=credential_slot)
        # Use data.data directly instead of _unwrap_data: the info response has a
        # nested "price":{…} object at the top level and _unwrap_data may return
        # the wrong sub-object (e.g. "pool") instead of the full data payload.
        raw = data.get("data", data) if isinstance(data, dict) else data
        raw = raw if isinstance(raw, dict) else {}

        # GMGN /v1/token/info returns price as a nested object: data.price.price
        price_nested = raw.get("price")
        if isinstance(price_nested, dict):
            price_usd = self._to_float(self._first_present(price_nested, ["price_usd", "price", "usd_price"]))
            price_sol = self._to_float(self._first_present(price_nested, ["price_sol", "sol_price", "native_price"]))
        else:
            price_usd = self._to_float(self._first_present(raw, ["price_usd", "price", "usd_price"]))
            price_sol = self._to_float(self._first_present(raw, ["price_sol", "sol_price", "native_price"]))

        # Some fields (e.g. quote_reserve) live only inside the nested pool object
        pool_obj = raw.get("pool") if isinstance(raw, dict) else None
        if not isinstance(pool_obj, dict):
            pool_obj = {}

        liquidity_usd = (
            self._to_float(self._first_present(raw, ["liquidity_usd", "liquidity", "reserve_usd"]))
            or self._to_float(self._first_present(pool_obj, ["liquidity_usd", "liquidity", "reserve_usd"]))
        )
        sol_side_liquidity = (
            self._to_float(self._first_present(raw, ["sol_side_liquidity", "sol_liquidity", "quote_reserve"]))
            or self._to_float(self._first_present(pool_obj, ["sol_side_liquidity", "sol_liquidity", "quote_reserve"]))
        )
        market_cap = self._to_float(self._first_present(raw, ["market_cap", "marketcap", "fdv"]))

        return {
            "price": price_usd or 0.0,
            "price_usd": price_usd,
            "price_sol": price_sol or 0.0,
            "liquidity_usd": liquidity_usd,
            "sol_side_liquidity": sol_side_liquidity,
            "market_cap": market_cap,
            "swaps_5m": self._to_float(self._first_present(price_nested, ["swaps_5m", "swaps5m"])) if isinstance(price_nested, dict) else self._to_float(self._first_present(raw, ["swaps_5m", "swaps5m"])),
            "swaps_1h": self._to_float(self._first_present(price_nested, ["swaps_1h", "swaps1h"])) if isinstance(price_nested, dict) else self._to_float(self._first_present(raw, ["swaps_1h", "swaps1h"])),
            "price_1m": self._to_float(self._first_present(price_nested, ["price_1m", "price1m"])) if isinstance(price_nested, dict) else self._to_float(self._first_present(raw, ["price_1m", "price1m"])),
            "price_5m": self._to_float(self._first_present(price_nested, ["price_5m", "price5m"])) if isinstance(price_nested, dict) else self._to_float(self._first_present(raw, ["price_5m", "price5m"])),
            "price_1h": self._to_float(self._first_present(price_nested, ["price_1h", "price1h"])) if isinstance(price_nested, dict) else self._to_float(self._first_present(raw, ["price_1h", "price1h"])),
            "raw_json": json.dumps(data, ensure_ascii=False, default=str),
            "source_mode": "REAL",
        }

    async def fetch_top_holders(self, token_mint: str, limit: int = 20, credential_slot: Optional[int] = None) -> List[Dict[str, Any]]:
        if self.mode == ProviderMode.MOCK:
            return [{"addr_type": 0, "top1_holder_rate": 0.04, "rate": 0.04, "source_mode": "MOCK"}]

        path = getattr(settings, "GMGN_TOKEN_HOLDERS_PATH", "/v1/market/token_top_holders")
        params = {"chain": "sol", "address": token_mint, "limit": int(limit)}
        data = await self._make_request(path, params, method="GET", credential_slot=credential_slot)
        items = self._extract_items(data, ("holders", "list", "items", "rows", "data"))
        out: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            rate = self._to_float(self._first_present(item, ["top1_holder_rate", "rate", "amount_percentage", "percentage", "hold_rate"]))
            addr_type = self._first_present(item, ["addr_type", "address_type", "type"], 0)
            try:
                addr_type = int(addr_type)
            except Exception:
                addr_type = 0
            out.append({
                **item,
                "addr_type": addr_type,
                "top1_holder_rate": rate,
                "rate": rate,
                "source_mode": "REAL",
                "raw_json": json.dumps(item, ensure_ascii=False, default=str),
            })
        return out

    async def fetch_smart_degen_holders(self, token_mint: str, limit: int = 20, credential_slot: Optional[int] = None) -> List[Dict[str, Any]]:
        if self.mode == ProviderMode.MOCK:
            return [
                {"addr_type": 0, "amount_percentage": 0.03, "usd_value": 300.0, "source_mode": "MOCK"},
                {"addr_type": 0, "amount_percentage": 0.02, "usd_value": 200.0, "source_mode": "MOCK"},
            ]

        path = getattr(settings, "GMGN_TOKEN_HOLDERS_PATH", "/v1/market/token_top_holders")
        params = {"chain": "sol", "address": token_mint, "limit": int(limit), "tag": "smart_degen"}
        data = await self._make_request(path, params, method="GET", credential_slot=credential_slot)
        items = self._extract_items(data, ("holders", "list", "items", "rows", "data"))
        out: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            addr_type = self._first_present(item, ["addr_type", "address_type", "type"], 0)
            try:
                addr_type = int(addr_type)
            except Exception:
                addr_type = 0
            if addr_type != 0:
                continue
            amount_percentage = self._to_float(self._first_present(item, ["amount_percentage", "rate", "hold_rate"]))
            usd_value = self._to_float(self._first_present(item, ["usd_value", "usd_val"]))
            buy_volume_cur = self._to_float(self._first_present(item, ["buy_volume_cur", "buy_volume"]))
            sell_volume_cur = self._to_float(self._first_present(item, ["sell_volume_cur", "sell_volume"]))
            out.append({
                "address": item.get("address") or item.get("wallet"),
                "addr_type": addr_type,
                "amount_percentage": amount_percentage,
                "usd_value": usd_value,
                "buy_volume_cur": buy_volume_cur,
                "sell_volume_cur": sell_volume_cur,
                "tags": item.get("tags") or [],
                "source_mode": "REAL",
                "raw_json": json.dumps(item, ensure_ascii=False, default=str),
            })
        return out
