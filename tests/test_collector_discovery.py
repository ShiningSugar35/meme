from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.app.collector.discovery import DiscoveryService
from backend.app.collector.models import ApiKeyRoles


class FakeClient:
    def __init__(self) -> None:
        self.endpoints = SimpleNamespace(trenches="/v1/trenches")
        self.calls: list[tuple[int, dict[str, object]]] = []

    async def request(self, slot, path, **kwargs):
        self.calls.append((slot.index, kwargs["json_body"]))
        return {"data": {"pump": {"items": [{"token_mint": "mint-1"}]}}}


async def no_sleep(_: float) -> None:
    return None


def test_twelve_key_role_layout_is_stable_and_redacted() -> None:
    roles = ApiKeyRoles.from_secrets([f"secret-{i}" for i in range(12)])
    assert [slot.index for slot in roles.discovery] == [0, 1, 2]
    assert roles.discovery_fallback.index == 3
    assert [slot.index for slot in roles.realtime] == [4, 5, 6, 7]
    assert [slot.index for slot in roles.realtime_fallback] == [8, 9]
    assert [slot.index for slot in roles.kline] == [10, 11]
    assert "secret-0" not in repr(roles.discovery[0])


def test_discovery_payload_has_all_launchpads_and_prefilters() -> None:
    roles = ApiKeyRoles.from_secrets([f"key-{i}" for i in range(12)])
    client = FakeClient()
    service = DiscoveryService(client, roles, retry_delay_seconds=0, sleeper=no_sleep)
    candidates = asyncio.run(service.discover("near_completion", limit=80))
    assert [candidate.address for candidate in candidates] == ["mint-1"]
    assert client.calls[0][0] == 1
    section = client.calls[0][1]["near_completion"]
    assert len(section["launchpad_platform"]) == 10
    assert section["max_rug_ratio"] == 0.2
    assert section["launchpad_platform_v2"] is True
    assert section["filters"] == ["offchain", "onchain"]

