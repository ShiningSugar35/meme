"""Trenches discovery for the three lifecycle sections."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from .client import GMGNDataClient
from .constants import DISCOVERY_TYPES, LAUNCHPADS, TRENCH_PREFILTERS
from .errors import CollectorError, CollectorValidationError
from .models import ApiKeyRoles, TokenCandidate


def _items(value: Any, preferred: Sequence[str] = ("items", "list", "rows", "tokens", "data")) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, Mapping):
        return []
    for key in preferred:
        nested = value.get(key)
        if isinstance(nested, list):
            return nested
        if isinstance(nested, Mapping):
            found = _items(nested, preferred)
            if found:
                return found
    return []


def _address(raw: Mapping[str, Any]) -> str:
    for key in ("token_mint", "token_address", "address", "mint", "base_address"):
        value = raw.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def extract_trench_candidates(data: Mapping[str, Any], requested_type: str) -> list[TokenCandidate]:
    inner: Any = data.get("data", data)
    if not isinstance(inner, Mapping):
        return []
    keys = [requested_type]
    if requested_type == "near_completion":
        keys.insert(0, "pump")  # GMGN returns this lifecycle under `pump`.
    result: list[TokenCandidate] = []
    seen: set[str] = set()
    for key in keys:
        section = inner.get(key)
        for raw in _items(section):
            if not isinstance(raw, Mapping):
                continue
            address = _address(raw)
            if not address or address in seen:
                continue
            seen.add(address)
            result.append(TokenCandidate(address, requested_type, dict(raw)))
    if not result:
        for raw in _items(inner):
            if not isinstance(raw, Mapping):
                continue
            address = _address(raw)
            if address and address not in seen:
                seen.add(address)
                result.append(TokenCandidate(address, requested_type, dict(raw)))
    return result


class DiscoveryService:
    def __init__(
        self,
        client: GMGNDataClient,
        roles: ApiKeyRoles,
        *,
        retry_delay_seconds: float = 10.0,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.roles = roles
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self._sleep = sleeper

    @staticmethod
    def request_body(token_type: str, limit: int = 80) -> dict[str, Any]:
        if token_type not in DISCOVERY_TYPES:
            raise CollectorValidationError(f"Unsupported discovery type: {token_type}")
        if not 1 <= limit <= 80:
            raise CollectorValidationError("GMGN trenches limit must be between 1 and 80")
        section: dict[str, Any] = {
            "filters": ["offchain", "onchain"],
            "launchpad_platform_v2": True,
            "limit": limit,
            "launchpad_platform": list(LAUNCHPADS),
            **TRENCH_PREFILTERS,
        }
        return {"version": "v2", token_type: section}

    async def discover(self, token_type: str, *, limit: int = 80) -> list[TokenCandidate]:
        body = self.request_body(token_type, limit)
        primary = self.roles.discovery[DISCOVERY_TYPES.index(token_type)]
        last_error: BaseException | None = None
        for attempt in range(3):
            try:
                data = await self.client.request(
                    primary,
                    self.client.endpoints.trenches,
                    method="POST",
                    params={"chain": "sol"},
                    json_body=body,
                )
                return extract_trench_candidates(data, token_type)[:limit]
            except Exception as exc:
                last_error = exc
                if attempt < 2 and self.retry_delay_seconds:
                    await self._sleep(self.retry_delay_seconds)
        try:
            data = await self.client.request(
                self.roles.discovery_fallback,
                self.client.endpoints.trenches,
                method="POST",
                params={"chain": "sol"},
                json_body=body,
            )
            return extract_trench_candidates(data, token_type)[:limit]
        except Exception as exc:
            last_error = exc
        raise CollectorError(
            f"Discovery failed for {token_type} after primary retries and fallback"
        ) from last_error

    async def discover_all(self, *, limit: int = 80) -> dict[str, list[TokenCandidate]]:
        # Sequential discovery preserves the dedicated key role semantics and
        # lets the shared 2-RPS limiter govern all traffic.
        result: dict[str, list[TokenCandidate]] = {}
        for token_type in DISCOVERY_TYPES:
            result[token_type] = await self.discover(token_type, limit=limit)
        return result

