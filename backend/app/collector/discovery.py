"""GMGN Trenches production discovery and shadow Trending discovery."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from .client import GMGNDataClient
from .constants import DISCOVERY_TYPES, LAUNCHPADS, SOL_TRENCH_QUOTE_ADDRESS_TYPES
from .errors import CollectorError, CollectorNetworkError, CollectorRateLimitError, CollectorValidationError
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
    return result


TRENDING_ORDER_BY = ("volume", "smart_degen_count", "change5m")
TRENDING_INTERVALS = ("1m", "5m", "1h", "6h", "24h")


def extract_trending_candidates(data: Mapping[str, Any], order_by: str) -> list[TokenCandidate]:
    if order_by not in TRENDING_ORDER_BY:
        raise CollectorValidationError(f"Unsupported trending order: {order_by}")
    raw_items = _items(data, ("rank", "items", "list", "rows", "tokens", "data"))
    result: list[TokenCandidate] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        address = _address(raw)
        if not address or address in seen:
            continue
        seen.add(address)
        payload = dict(raw)
        payload["_discovery_source"] = f"trending:{order_by}"
        result.append(TokenCandidate(address, "trending", payload))
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
        self._reserved_weight: dict[int, float] = {slot.index: 0.0 for slot in (*roles.discovery, roles.discovery_fallback)}
        self._trending_tie_cursor = 0

    def _note_reserved_weight(self, slot, weight: float) -> None:
        self._reserved_weight[slot.index] = self._reserved_weight.get(slot.index, 0.0) + float(weight)

    def _trending_slots(self) -> list[Any]:
        unique: dict[int, Any] = {}
        for slot in (*self.roles.discovery, self.roles.discovery_fallback):
            unique.setdefault(slot.index, slot)
        slots = [slot for slot in unique.values() if self.client.slot_rate_limit_remaining(slot) <= 0]
        if not slots:
            return []
        cursor = self._trending_tie_cursor
        self._trending_tie_cursor += 1
        return sorted(
            slots,
            key=lambda slot: (self._reserved_weight.get(slot.index, 0.0), (slot.index - cursor) % max(1, len(unique))),
        )

    @staticmethod
    def trending_params(order_by: str, *, interval: str = "5m", limit: int = 80) -> dict[str, Any]:
        if order_by not in TRENDING_ORDER_BY:
            raise CollectorValidationError(f"Unsupported trending order: {order_by}")
        if interval not in TRENDING_INTERVALS:
            raise CollectorValidationError(f"Unsupported trending interval: {interval}")
        if not 1 <= limit <= 100:
            raise CollectorValidationError("GMGN trending limit must be between 1 and 100")
        return {
            "chain": "sol",
            "interval": interval,
            "order_by": order_by,
            "direction": "desc",
            "limit": limit,
            "filters": ["renounced", "frozen"],
            "platform": list(LAUNCHPADS),
        }

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
            "quote_address_type": list(SOL_TRENCH_QUOTE_ADDRESS_TYPES),
            "min_created": "2m",
            "max_created": "300m",
        }
        return {"version": "v2", token_type: section}

    async def discover(self, token_type: str, *, limit: int = 80) -> list[TokenCandidate]:
        body = self.request_body(token_type, limit)
        primary = self.roles.discovery[DISCOVERY_TYPES.index(token_type)]
        fallback = self.roles.discovery_fallback
        last_error: BaseException | None = None

        def cooling(slot) -> float:
            cooldown = getattr(self.client, "slot_rate_limit_remaining", None)
            return float(cooldown(slot)) if callable(cooldown) else 0.0

        primary_remaining = cooling(primary)
        fallback_remaining = cooling(fallback)
        if primary_remaining > 0 and fallback_remaining > 0:
            raise CollectorRateLimitError(
                f"GMGN discovery keys cooling down for {token_type}",
                reset_at=int(time.time() + max(primary_remaining, fallback_remaining)),
            )
        if primary_remaining <= 0:
            for attempt in range(3):
                try:
                    data = await self.client.request(
                        primary, self.client.endpoints.trenches, method="POST",
                        params={"chain": "sol"}, json_body=body,
                    )
                    self._note_reserved_weight(
                        primary, 3
                    )
                    return extract_trench_candidates(data, token_type)[:limit]
                except CollectorNetworkError:
                    raise
                except CollectorRateLimitError as exc:
                    last_error = exc
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < 2 and self.retry_delay_seconds:
                        await self._sleep(self.retry_delay_seconds)
        if cooling(fallback) <= 0:
            try:
                data = await self.client.request(
                    fallback, self.client.endpoints.trenches, method="POST",
                    params={"chain": "sol"}, json_body=body,
                )
                self._note_reserved_weight(
                    fallback, 3
                )
                return extract_trench_candidates(data, token_type)[:limit]
            except CollectorNetworkError:
                raise
            except Exception as exc:
                last_error = exc
        raise CollectorError(
            f"Discovery failed for {token_type} after primary retries and fallback"
        ) from last_error

    async def discover_trending(
        self,
        order_by: str,
        *,
        interval: str = "5m",
        limit: int = 80,
    ) -> list[TokenCandidate]:
        params = self.trending_params(order_by, interval=interval, limit=limit)
        slots = self._trending_slots()
        if not slots:
            remaining = [
                self.client.slot_rate_limit_remaining(slot)
                for slot in (*self.roles.discovery, self.roles.discovery_fallback)
            ]
            raise CollectorRateLimitError(
                f"GMGN discovery keys cooling down for trending:{order_by}",
                reset_at=int(time.time() + max(remaining or [300.0])),
            )
        last_error: BaseException | None = None
        weight = 1
        for slot in slots:
            try:
                data = await self.client.request(slot, self.client.endpoints.trending, params=params)
                self._note_reserved_weight(slot, weight)
                return extract_trending_candidates(data, order_by)[:limit]
            except CollectorNetworkError:
                raise
            except CollectorRateLimitError as exc:
                last_error = exc
                continue
            except Exception as exc:
                last_error = exc
                continue
        raise CollectorError(f"Trending discovery failed for {order_by}") from last_error

    async def discover_all(self, *, limit: int = 80) -> dict[str, list[TokenCandidate]]:
        # Sequential discovery preserves the dedicated key role semantics and
        # lets the shared 2-RPS limiter govern all traffic.
        result: dict[str, list[TokenCandidate]] = {}
        for token_type in DISCOVERY_TYPES:
            result[token_type] = await self.discover(token_type, limit=limit)
        return result

