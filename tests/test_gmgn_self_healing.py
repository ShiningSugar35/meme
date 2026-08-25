from __future__ import annotations

import httpx
import pytest

from backend.app.collector.client import CollectorEndpoints, HttpxTransport
from backend.app.collector.discovery import DiscoveryService
from backend.app.collector.enrichment import GMGNEnrichmentProvider
from backend.app.collector.errors import CollectorNetworkError, CollectorRateLimitError
from backend.app.collector.models import ApiKeyRoles
from backend.app.services.position_monitor import GMGNPositionMarketProvider


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def json(self) -> dict[str, object]:
        return {"code": 0, "message": "success", "data": {}}


class _FailingHttpxClient:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def request(self, method: str, url: str, **kwargs):
        self.calls += 1
        raise httpx.ConnectError("stale pool", request=httpx.Request(method, url))

    async def aclose(self) -> None:
        self.closed = True


class _HealthyHttpxClient:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def request(self, method: str, url: str, **kwargs):
        self.calls += 1
        return _Response()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_httpx_transport_recycles_owned_pool_and_retries_once(monkeypatch) -> None:
    failed = _FailingHttpxClient()
    healthy = _HealthyHttpxClient()
    clients = iter((failed, healthy))
    monkeypatch.setattr(httpx, "AsyncClient", lambda: next(clients))

    transport = HttpxTransport()
    response = await transport.request(
        "GET",
        "https://example.invalid/v1/test",
        headers={},
        params=None,
        json_body=None,
        timeout=1.0,
    )

    assert response.status_code == 200
    assert failed.calls == 1
    assert failed.closed is True
    assert healthy.calls == 1
    assert transport.recycle_count == 1
    await transport.close()
    assert healthy.closed is True


class _AlwaysNetworkFailClient:
    def __init__(self) -> None:
        self.endpoints = CollectorEndpoints()
        self.calls: list[int] = []

    def slot_rate_limit_remaining(self, slot) -> float:
        return 0.0

    async def request(self, slot, path, **kwargs):
        self.calls.append(slot.index)
        raise CollectorNetworkError(f"network failed on slot {slot.index}")


@pytest.mark.asyncio
async def test_discovery_network_failure_does_not_rotate_api_keys() -> None:
    client = _AlwaysNetworkFailClient()
    roles = ApiKeyRoles.from_secrets(("key-a", "key-b", "key-c"))
    service = DiscoveryService(client, roles, retry_delay_seconds=0)

    with pytest.raises(CollectorNetworkError):
        await service.discover("new_creation", limit=1)

    assert client.calls == [0]


@pytest.mark.asyncio
async def test_position_monitor_network_failure_does_not_walk_entire_key_pool() -> None:
    client = _AlwaysNetworkFailClient()
    roles = ApiKeyRoles.from_secrets(tuple(f"key-{index}" for index in range(12)))
    provider = GMGNPositionMarketProvider(client, roles)

    with pytest.raises(CollectorNetworkError):
        await provider.token_bundle("token-address")

    assert client.calls == [3]


class _FirstSlotRateLimitedClient:
    def __init__(self) -> None:
        self.endpoints = CollectorEndpoints()
        self.calls: list[int] = []

    async def request(self, slot, path, **kwargs):
        self.calls.append(slot.index)
        if slot.index == 0:
            raise CollectorRateLimitError("slot-local rate limit", reset_at=1_800_000_000)
        return {"code": 0, "message": "success", "data": {"price": 1.0}}


@pytest.mark.asyncio
async def test_position_monitor_rotates_immediately_after_slot_rate_limit() -> None:
    client = _FirstSlotRateLimitedClient()
    roles = ApiKeyRoles.from_secrets(("key-a", "key-b", "key-c"))
    provider = GMGNPositionMarketProvider(client, roles)

    bundle = await provider.token_bundle("token-address")

    assert client.calls == [0, 1]
    assert bundle["token_info"]["data"]["price"] == 1.0


class _CooldownAwareEnrichmentClient:
    def __init__(self) -> None:
        self.endpoints = CollectorEndpoints()
        self.calls: list[int] = []

    def slot_rate_limit_remaining(self, slot) -> float:
        return 30.0 if slot.index == 3 else 0.0

    async def request(self, slot, path, **kwargs):
        self.calls.append(slot.index)
        return {"code": 0, "message": "success", "data": {"ok": True}}


@pytest.mark.asyncio
async def test_enrichment_skips_cooling_primary_without_fallback_delay() -> None:
    client = _CooldownAwareEnrichmentClient()
    roles = ApiKeyRoles.from_secrets(tuple(f"key-{index}" for index in range(12)))
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    provider = GMGNEnrichmentProvider(
        client, roles, primary_attempts=2, primary_retry_seconds=2.0,
        fallback_delay_seconds=2.0, sleeper=sleep,
    )
    result = await provider._realtime_request("/v1/token/info", params={"chain": "sol"})

    assert result["data"]["ok"] is True
    assert client.calls == [4]
    assert waits == []


class _AlwaysRateLimitedDiscoveryClient:
    def __init__(self) -> None:
        self.endpoints = CollectorEndpoints()
        self.calls: list[int] = []
        self.cooling: set[int] = set()

    def slot_rate_limit_remaining(self, slot) -> float:
        return 30.0 if slot.index in self.cooling else 0.0

    async def request(self, slot, path, **kwargs):
        self.calls.append(slot.index)
        self.cooling.add(slot.index)
        raise CollectorRateLimitError("rate limited", reset_at=1_800_000_000)


@pytest.mark.asyncio
async def test_discovery_rate_limit_does_not_retry_same_key_or_sleep() -> None:
    client = _AlwaysRateLimitedDiscoveryClient()
    roles = ApiKeyRoles.from_secrets(("key-a", "key-b", "key-c"))
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    service = DiscoveryService(client, roles, retry_delay_seconds=10.0, sleeper=sleep)

    with pytest.raises(Exception):
        await service.discover("new_creation", limit=1)

    assert client.calls == [0, 2]
    assert waits == []


class _FullPoolRateLimitedClient:
    def __init__(self) -> None:
        self.endpoints = CollectorEndpoints()
        self.calls: list[int] = []

    async def request(self, slot, path, **kwargs):
        self.calls.append(slot.index)
        raise CollectorRateLimitError("rate limited", reset_at=1_800_000_000 + slot.index)


@pytest.mark.asyncio
async def test_position_monitor_full_pool_rate_limit_opens_nonblocking_circuit() -> None:
    client = _FullPoolRateLimitedClient()
    roles = ApiKeyRoles.from_secrets(("key-a", "key-b", "key-c"))
    provider = GMGNPositionMarketProvider(client, roles)

    with pytest.raises(CollectorRateLimitError) as first:
        await provider.token_bundle("token-address")
    first_calls = list(client.calls)
    with pytest.raises(CollectorRateLimitError) as second:
        await provider.token_bundle("token-address")

    assert first_calls == [0, 1, 2]
    assert client.calls == first_calls
    assert first.value.reset_at == 1_800_000_002
    assert second.value.reset_at == 1_800_000_002


class _RateLimitedThenHealthyEnrichmentClient:
    def __init__(self) -> None:
        self.endpoints = CollectorEndpoints()
        self.calls: list[int] = []

    def slot_rate_limit_remaining(self, slot) -> float:
        return 0.0

    async def request(self, slot, path, **kwargs):
        self.calls.append(slot.index)
        if slot.index in {3, 4}:
            raise CollectorRateLimitError("rate limited", reset_at=1_800_000_000)
        return {"code": 0, "message": "success", "data": {"ok": True}}


@pytest.mark.asyncio
async def test_enrichment_does_not_sleep_between_rate_limited_fallback_keys() -> None:
    client = _RateLimitedThenHealthyEnrichmentClient()
    roles = ApiKeyRoles.from_secrets(tuple(f"key-{index}" for index in range(12)))
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    provider = GMGNEnrichmentProvider(
        client, roles, primary_attempts=2, primary_retry_seconds=2.0,
        fallback_delay_seconds=2.0, sleeper=sleep,
    )
    result = await provider._realtime_request("/v1/token/info", params={"chain": "sol"})

    assert result["data"]["ok"] is True
    assert client.calls == [3, 4, 5]
    assert waits == []


@pytest.mark.asyncio
async def test_discovery_all_cooling_keys_preserves_rate_limit_type_without_network_call() -> None:
    client = _AlwaysRateLimitedDiscoveryClient()
    roles = ApiKeyRoles.from_secrets(("key-a", "key-b", "key-c"))
    client.cooling.update({0, roles.discovery_fallback.index})
    service = DiscoveryService(client, roles, retry_delay_seconds=10.0)

    with pytest.raises(CollectorRateLimitError) as exc_info:
        await service.discover("new_creation", limit=1)

    assert client.calls == []
    assert exc_info.value.reset_at is not None
