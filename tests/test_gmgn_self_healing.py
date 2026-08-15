from __future__ import annotations

import httpx
import pytest

from backend.app.collector.client import CollectorEndpoints, HttpxTransport
from backend.app.collector.discovery import DiscoveryService
from backend.app.collector.errors import CollectorNetworkError
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

    assert client.calls == [0]
