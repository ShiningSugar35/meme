"""
Jito provider.

Aligned to Jito docs:
- Tip floor is read from settings.JITO_TIP_FLOOR_URL (default bundles.jito.wtf/api/v1/bundles/tip_floor).
- Bundle-related calls use the block-engine JSON-RPC endpoint /api/v1/bundles.
- send() returns mock success in mock, is blocked in online_readonly, and only sends in live mode.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from .base import ExecutionProvider
from ..config import ProviderMode, settings
from ..db.repositories import Repositories
from ..logging_config import logger

try:
    import httpx
    HAS_HTTPX = True
except ImportError:  # pragma: no cover - runtime dependency guard
    HAS_HTTPX = False
    logger.warning("httpx not installed. online_readonly/live mode will not work for Jito.")


class JitoProvider(ExecutionProvider):
    def __init__(self, repo: Repositories, mode: Optional[ProviderMode] = None):
        self.repo = repo
        self.mode = mode or settings.get_provider_mode()
        self.jito_enabled = settings.JITO_ENABLED
        self.block_engine_url = (settings.JITO_BLOCK_ENGINE_URL or "https://mainnet.block-engine.jito.wtf").rstrip("/")
        self.tip_floor_url = settings.JITO_TIP_FLOOR_URL or "https://bundles.jito.wtf/api/v1/bundles/tip_floor"
        self._tip_cache: Optional[Dict[str, Any]] = None
        self._tip_cache_time = 0.0
        self._tip_cache_ttl = max(1, int(getattr(settings, "TIP_FLOOR_REFRESH_SECONDS", 3)))

        if self.mode == ProviderMode.MOCK:
            logger.info("Jito Provider initialized in MOCK mode - send() returns mock success")
        elif self.mode == ProviderMode.ONLINE_READONLY:
            if not HAS_HTTPX:
                raise ImportError("httpx required for Jito online_readonly mode")
            logger.info("Jito Provider initialized in ONLINE_READONLY mode - send() is blocked")
        elif self.mode == ProviderMode.LIVE:
            if not self.jito_enabled:
                raise ValueError("Jito requires JITO_ENABLED=true")
            if not HAS_HTTPX:
                raise ImportError("httpx required for Jito live mode")
            logger.info("Jito Provider initialized in LIVE mode", block_engine_url=self.block_engine_url)
        else:
            raise ValueError(f"Unsupported provider mode: {self.mode}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_json(data: Any) -> str:
        return json.dumps(data if data is not None else {}, ensure_ascii=False, separators=(",", ":"))

    async def _log(
        self,
        endpoint: str,
        ok: bool,
        request_summary: Dict[str, Any],
        response_summary: Dict[str, Any],
        status_code: int = 200,
        latency_ms: int = 1,
        error_code: Optional[str] = None,
        error_summary: Optional[str] = None,
        method: str = "POST",
    ) -> None:
        await self.repo.append_provider_request(
            "JITO", endpoint, method, status_code, latency_ms, ok,
            error_code, error_summary,
            self._safe_json(request_summary), self._safe_json(response_summary),
        )

    async def _http_get(self, url: str, endpoint_name: str) -> Any:
        if not HAS_HTTPX:
            raise ImportError("httpx required for Jito HTTP calls")
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                response = await client.get(url, headers={"Accept": "application/json"})
            latency_ms = int((time.time() - start) * 1000)
            if response.status_code != 200:
                error_msg = f"Jito HTTP {response.status_code}: {response.text[:500]}"
                await self._log(endpoint_name, False, {}, {"error": error_msg}, response.status_code, latency_ms,
                                "JITO_HTTP_ERROR", error_msg, method="GET")
                raise RuntimeError(error_msg)
            data = response.json()
            await self._log(endpoint_name, True, {}, self._summarize(data), response.status_code, latency_ms, method="GET")
            return data
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            await self._log(endpoint_name, False, {}, {"error": str(e)}, 500, latency_ms,
                            "JITO_ERROR", str(e), method="GET")
            raise

    async def _block_engine_rpc(self, method: str, params: Optional[List[Any]] = None) -> Dict[str, Any]:
        if not HAS_HTTPX:
            raise ImportError("httpx required for Jito block-engine calls")
        url = f"{self.block_engine_url}/api/v1/bundles"
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
            latency_ms = int((time.time() - start) * 1000)
            if response.status_code != 200:
                error_msg = f"Jito block-engine HTTP {response.status_code}: {response.text[:500]}"
                await self._log(method, False, self._summarize_request(payload), {"error": error_msg},
                                response.status_code, latency_ms, "JITO_HTTP_ERROR", error_msg)
                raise RuntimeError(error_msg)
            data = response.json()
            if isinstance(data, dict) and data.get("error"):
                error_msg = json.dumps(data.get("error"), ensure_ascii=False)
                await self._log(method, False, self._summarize_request(payload), {"error": data.get("error")},
                                response.status_code, latency_ms, "JITO_RPC_ERROR", error_msg)
                raise RuntimeError(error_msg)
            await self._log(method, True, self._summarize_request(payload), self._summarize(data),
                            response.status_code, latency_ms)
            return data
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            await self._log(method, False, self._summarize_request(payload), {"error": str(e)},
                            500, latency_ms, "JITO_ERROR", str(e))
            raise

    @staticmethod
    def _summarize(data: Any) -> Dict[str, Any]:
        if isinstance(data, list):
            if data and isinstance(data[0], dict):
                return {"type": "list", "count": len(data), "keys": list(data[0].keys())[:20]}
            return {"type": "list", "count": len(data)}
        if isinstance(data, dict):
            summary = {"keys": list(data.keys())[:20]}
            if "result" in data:
                result = data.get("result")
                if isinstance(result, list):
                    summary["result_count"] = len(result)
                else:
                    summary["result"] = str(result)[:120]
            return summary
        return {"type": type(data).__name__}

    @staticmethod
    def _summarize_request(payload: Dict[str, Any]) -> Dict[str, Any]:
        method = payload.get("method")
        params = payload.get("params") or []
        summary = {"jsonrpc": payload.get("jsonrpc"), "method": method, "id": payload.get("id")}
        if method == "sendBundle" and params:
            first = params[0]
            summary["bundle_tx_count"] = len(first) if isinstance(first, list) else 1
            summary["encoding"] = params[1].get("encoding") if len(params) > 1 and isinstance(params[1], dict) else None
        else:
            summary["params_count"] = len(params)
        return summary

    @staticmethod
    def _first_tip_record(data: Any) -> Dict[str, Any]:
        if isinstance(data, list) and data:
            return data[0] if isinstance(data[0], dict) else {}
        if isinstance(data, dict):
            if isinstance(data.get("data"), list) and data["data"]:
                return data["data"][0] if isinstance(data["data"][0], dict) else {}
            if isinstance(data.get("data"), dict):
                return data["data"]
            return data
        return {}

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None or value == "":
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _normalize_tip_floor(self, data: Any) -> Dict[str, Any]:
        record = self._first_tip_record(data)
        keys = [
            "landed_tips_25th_percentile",
            "landed_tips_50th_percentile",
            "landed_tips_75th_percentile",
            "landed_tips_95th_percentile",
            "landed_tips_99th_percentile",
            "ema_landed_tips_50th_percentile",
        ]
        normalized = {k: self._to_float(record.get(k), 0.0) for k in keys}
        normalized["raw"] = data
        normalized["mode"] = self.mode.value if self.mode != ProviderMode.MOCK else "MOCK"
        return normalized

    def _normalize_bundle_result(self, data: Dict[str, Any]) -> Dict[str, Any]:
        result = data.get("result") if isinstance(data, dict) else data
        if isinstance(result, str):
            return {"ok": True, "bundle_id": result, "mode": self.mode.value, "raw": data}
        if isinstance(result, dict):
            return {
                "ok": True,
                "bundle_id": result.get("bundle_id") or result.get("bundleId") or result.get("uuid") or "",
                "signature": result.get("signature") or result.get("tx_signature") or "",
                "mode": self.mode.value,
                "raw": data,
            }
        return {"ok": True, "bundle_id": "", "mode": self.mode.value, "raw": data}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def get_tip_accounts(self) -> list:
        if self.mode == ProviderMode.MOCK:
            return [
                "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
                "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
            ]
        data = await self._block_engine_rpc("getTipAccounts", [])
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, list) else []

    async def choose_tip_lamports(self, percentile: str = "75th") -> dict:
        floor = await self.get_tip_floor()
        key_map = {
            "25th": "landed_tips_25th_percentile",
            "50th": "landed_tips_50th_percentile",
            "75th": "landed_tips_75th_percentile",
            "95th": "landed_tips_95th_percentile",
            "99th": "landed_tips_99th_percentile",
        }
        key = key_map.get(percentile, "landed_tips_75th_percentile")
        from ..trading.accounting import normalize_jito_tip_floor_to_lamports
        lamports = normalize_jito_tip_floor_to_lamports(floor.get(key))
        return {
            "jito_tip_lamports": lamports,
            "jito_tip_source": f"tip_floor.{key}",
            "tip_floor_raw": floor,
        }

    async def get_tip_floor(self) -> Dict[str, Any]:
        now = time.time()
        if self._tip_cache and (now - self._tip_cache_time) < self._tip_cache_ttl:
            return self._tip_cache

        if self.mode == ProviderMode.MOCK:
            response = {
                "landed_tips_25th_percentile": 1000.0,
                "landed_tips_50th_percentile": 2000.0,
                "landed_tips_75th_percentile": 3000.0,
                "landed_tips_95th_percentile": 5000.0,
                "landed_tips_99th_percentile": 8000.0,
                "ema_landed_tips_50th_percentile": 2000.0,
                "raw": {"mode": "MOCK"},
                "mode": "MOCK",
            }
            await self._log("tip_floor", True, {}, response, method="GET")
            self._tip_cache = response
            self._tip_cache_time = now
            return response

        data = await self._http_get(self.tip_floor_url, "tip_floor")
        normalized = self._normalize_tip_floor(data)
        self._tip_cache = normalized
        self._tip_cache_time = now
        return normalized

    async def simulate(self, transaction_or_bundle: Any) -> Dict[str, Any]:
        if self.mode == ProviderMode.MOCK:
            response = {"ok": True, "result": "simulated", "mode": "MOCK"}
            await self._log("simulateBundle", True, {"tx_or_bundle": "mock"}, response)
            return response

        # Jito simulation is a read-style RPC call. Still block arbitrary transaction submission in online_readonly.
        try:
            params = transaction_or_bundle if isinstance(transaction_or_bundle, list) else [transaction_or_bundle]
            data = await self._block_engine_rpc("simulateBundle", params)
            return {"ok": True, "result": data.get("result"), "mode": self.mode.value, "raw": data}
        except Exception as e:
            return {"ok": False, "error": "SIMULATE_FAILED", "message": str(e), "mode": self.mode.value}

    async def send(self, transaction_or_bundle: Any) -> Dict[str, Any]:
        if self.mode == ProviderMode.MOCK:
            response = {"ok": True, "bundle_id": "MOCK_BUNDLE", "jito_tip_lamports": 3000,
                        "jito_tip_source": "tip_floor.75th_MOCK", "mode": "MOCK"}
            await self._log("sendBundle", True, {"mode": "MOCK", "tx_or_bundle": "mock"}, response)
            return response

        if self.mode == ProviderMode.ONLINE_READONLY:
            error_msg = f"Jito send() is blocked in {self.mode.value} mode"
            await self._log("sendBundle", False, {"mode": self.mode.value}, {}, 403, 0,
                            "JITO_MODE_BLOCKED", error_msg)
            return {"ok": False, "error": "MODE_BLOCKED", "message": error_msg, "mode": self.mode.value}

        if self.mode != ProviderMode.LIVE:
            return {"ok": False, "error": "UNSUPPORTED_MODE", "message": str(self.mode)}

        if isinstance(transaction_or_bundle, dict) and transaction_or_bundle.get("transactions"):
            transactions = transaction_or_bundle["transactions"]
            encoding = transaction_or_bundle.get("encoding", "base64")
        elif isinstance(transaction_or_bundle, list):
            transactions = transaction_or_bundle
            encoding = "base64"
        else:
            transactions = [transaction_or_bundle]
            encoding = "base64"

        if not transactions or not all(isinstance(tx, str) and tx for tx in transactions):
            return {"ok": False, "error": "INVALID_BUNDLE", "message": "sendBundle requires non-empty encoded transaction strings"}

        tip_data = await self.choose_tip_lamports("75th")
        jito_tip_lamports = tip_data.get("jito_tip_lamports", 0)

        try:
            data = await self._block_engine_rpc("sendBundle", [transactions, {"encoding": encoding}])
            result = self._normalize_bundle_result(data)
            result["jito_tip_lamports"] = jito_tip_lamports
            result["jito_tip_source"] = tip_data.get("jito_tip_source")
            result["retry_count"] = 0
            return result
        except Exception as e:
            error_msg = str(e)
            if "InstructionError" in error_msg:
                return {"ok": False, "error": "INSTRUCTION_ERROR", "message": error_msg, "mode": "LIVE"}
            return {"ok": False, "error": "JITO_ERROR", "message": error_msg, "mode": "LIVE"}

    async def get_inflight_bundle_statuses(self, bundle_ids: List[str]) -> Dict[str, Any]:
        if self.mode == ProviderMode.MOCK:
            return {"ok": True, "value": [], "mode": "MOCK"}
        data = await self._block_engine_rpc("getInflightBundleStatuses", [bundle_ids])
        return {"ok": True, "value": data.get("result"), "mode": self.mode.value, "raw": data}

    async def get_bundle_statuses(self, bundle_ids: List[str]) -> Dict[str, Any]:
        if self.mode == ProviderMode.MOCK:
            return {"ok": True, "value": [], "mode": "MOCK"}
        data = await self._block_engine_rpc("getBundleStatuses", [bundle_ids])
        return {"ok": True, "value": data.get("result"), "mode": self.mode.value, "raw": data}
