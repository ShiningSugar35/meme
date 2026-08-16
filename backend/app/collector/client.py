"""GMGN data client with injected credentials and transport."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .errors import (
    CollectorAPIError,
    CollectorNetworkError,
    CollectorRateLimitError,
    CollectorValidationError,
)
from .models import ApiSlot, TransportResponse
from .rate_limit import AsyncRateLimiter


class AsyncHttpTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, Any] | None,
        json_body: Mapping[str, Any] | None,
        timeout: float,
    ) -> TransportResponse: ...


class HttpxTransport:
    """Optional production transport; importing collector does not require httpx."""

    def __init__(self, client: Any | None = None) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency wiring
            raise RuntimeError("httpx is required to use HttpxTransport") from exc
        self._httpx = httpx
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._recycle_lock = asyncio.Lock()
        self._recycle_count = 0

    async def _recycle_after_transport_failure(self, failed_client: Any) -> None:
        """Replace a poisoned/stale owned connection pool once per failure generation."""
        if not self._owns_client:
            return
        async with self._recycle_lock:
            if self._client is not failed_client:
                return
            try:
                await failed_client.aclose()
            finally:
                self._client = self._httpx.AsyncClient()
                self._recycle_count += 1

    @property
    def recycle_count(self) -> int:
        return self._recycle_count

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, Any] | None,
        json_body: Mapping[str, Any] | None,
        timeout: float,
    ) -> TransportResponse:
        request_kwargs = {
            "headers": dict(headers),
            "params": dict(params or {}),
            "json": dict(json_body) if json_body is not None else None,
            "timeout": timeout,
        }
        client = self._client
        try:
            response = await client.request(method, url, **request_kwargs)
        except self._httpx.TransportError:
            if not self._owns_client:
                raise
            # Long-lived httpx pools can remain poisoned after a transient
            # route/interface change. Rebuild the pool and retry exactly once.
            await self._recycle_after_transport_failure(client)
            response = await self._client.request(method, url, **request_kwargs)
        try:
            data: Any = response.json()
        except Exception:
            data = {"message": "GMGN returned a non-JSON response"}
        return TransportResponse(
            status_code=response.status_code,
            data=data,
            headers=dict(response.headers),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@dataclass(frozen=True, slots=True)
class CollectorEndpoints:
    trenches: str = "/v1/trenches"
    token_info: str = "/v1/token/info"
    token_security: str = "/v1/token/security"
    token_pool_info: str = "/v1/token/pool_info"
    top_holders: str = "/v1/market/token_top_holders"
    kline: str = "/v1/market/token_kline"
    trending: str = "/v1/market/rank"
    signal: str = "/v1/market/token_signal"
    hot_searches: str = "/v1/market/hot_searches"
    created_tokens: str = "/v1/user/created_tokens"


def _extract_reset_at(data: Any, headers: Mapping[str, str]) -> int | None:
    candidates: list[Any] = []
    for key, value in headers.items():
        if key.lower() in {"x-ratelimit-reset", "ratelimit-reset", "retry-after"}:
            candidates.append(value)
    if isinstance(data, Mapping):
        candidates.extend((data.get("reset_at"), data.get("resetAt")))
        nested = data.get("data")
        if isinstance(nested, Mapping):
            candidates.extend((nested.get("reset_at"), nested.get("resetAt")))
    for value in candidates:
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            continue
        # Retry-After may be a short relative duration.
        if parsed < 10_000_000:
            return int(time.time()) + parsed
        if parsed > 10_000_000_000:
            parsed //= 1_000
        return parsed
    match = re.search(r"reset_at[^0-9]*([0-9]{10,13})", str(data))
    if match:
        parsed = int(match.group(1))
        return parsed // 1_000 if parsed > 10_000_000_000 else parsed
    return None


def _response_code(data: Any) -> str | None:
    if not isinstance(data, Mapping) or data.get("code") in (None, "", 0, "0"):
        return None
    return str(data.get("code"))


def _safe_message(data: Any) -> str:
    if isinstance(data, Mapping):
        for key in ("message", "msg", "error", "error_status"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)[:300]
    return "GMGN request failed"


class GMGNDataClient:
    def __init__(
        self,
        *,
        base_url: str,
        transport: AsyncHttpTransport,
        limiter: AsyncRateLimiter | None = None,
        endpoints: CollectorEndpoints | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        if not base_url.strip():
            raise CollectorValidationError("GMGN base URL is required")
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.limiter = limiter or AsyncRateLimiter(2.0)
        self.endpoints = endpoints or CollectorEndpoints()
        self.timeout_seconds = timeout_seconds
        self._slot_locks: dict[int, asyncio.Lock] = {}

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def route_weight(path: str) -> int:
        """GMGN documented leaky-bucket weights, applied atop the configured shared gate."""
        normalized = path.lower()
        if "token_top_holders" in normalized or "token_top_traders" in normalized:
            return 5
        if "trenches" in normalized or "token_signal" in normalized or "hot_searches" in normalized:
            return 3
        if "token_kline" in normalized or "created_tokens" in normalized:
            return 2
        return 1

    async def request(
        self,
        slot: ApiSlot,
        path: str,
        *,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        lock = self._slot_locks.setdefault(slot.index, asyncio.Lock())
        async with lock:
            await self.limiter.acquire(self.route_weight(path))
            request_params = {
                key: value
                for key, value in dict(params or {}).items()
                if value not in (None, "")
            }
            request_params.update(
                timestamp=int(time.time()),
                client_id=str(uuid.uuid4()),
            )
            # Never include this mapping in an exception or log message.
            headers = {
                "X-APIKEY": slot.secret,
                "x-api-key": slot.secret,
                "Content-Type": "application/json",
            }
            try:
                response = await self.transport.request(
                    method.upper(),
                    self._url(path),
                    headers=headers,
                    params=request_params,
                    json_body=json_body,
                    timeout=timeout_seconds or self.timeout_seconds,
                )
            except CollectorRateLimitError:
                raise
            except Exception as exc:
                raise CollectorNetworkError(
                    f"GMGN network request failed (slot={slot.index}, path={path})"
                ) from exc

            code = _response_code(response.data)
            message = _safe_message(response.data)
            rate_limited = response.status_code == 429 or code == "429" or (
                "rate_limit" in message.lower() or "too many" in message.lower()
            )
            if rate_limited:
                reset_at = _extract_reset_at(response.data, response.headers)
                self.limiter.note_rate_limit(reset_at)
                raise CollectorRateLimitError(
                    f"GMGN rate limited (slot={slot.index}, path={path})",
                    reset_at=reset_at,
                )
            if response.status_code >= 400:
                error_type = CollectorValidationError if response.status_code in (400, 422) else CollectorAPIError
                if error_type is CollectorValidationError:
                    raise error_type(
                        f"GMGN rejected request (status={response.status_code}, path={path}): {message}"
                    )
                raise error_type(
                    f"GMGN API failure (status={response.status_code}, path={path}): {message}",
                    status_code=response.status_code,
                    code=code,
                )
            if code and code.lower() not in {"success"}:
                raise CollectorAPIError(
                    f"GMGN business response failed (path={path}): {message}",
                    status_code=response.status_code,
                    code=code,
                )
            if isinstance(response.data, Mapping):
                return response.data
            return {"data": response.data}
