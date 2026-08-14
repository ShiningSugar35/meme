from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.app.collector.discovery import DiscoveryService, extract_trench_candidates
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


def test_dynamic_key_role_layout_uses_all_configured_keys_and_redacts() -> None:
    roles = ApiKeyRoles.from_secrets([f"secret-{i}" for i in range(12)])
    assert [slot.index for slot in roles.discovery] == [0, 1]
    assert [slot.index for slot in roles.position_monitor] == list(range(12))
    assert roles.discovery_fallback.index == 2
    assert [slot.index for slot in roles.realtime] == list(range(12))
    assert [slot.index for slot in roles.realtime_fallback] == list(range(12))
    assert [slot.index for slot in roles.kline] == list(range(12))
    assert [slot.index for slot in roles.all_slots] == list(range(12))
    assert "secret-0" not in repr(roles.discovery[0])

    single = ApiKeyRoles.from_secrets(["one-key"])
    assert [slot.index for slot in single.discovery] == [0, 0]
    assert [slot.index for slot in single.position_monitor] == [0]
    assert single.discovery_fallback.index == 0


def test_discovery_payload_tracks_current_gmgn_contract_without_business_prefilters() -> None:
    roles = ApiKeyRoles.from_secrets([f"key-{i}" for i in range(12)])
    client = FakeClient()
    service = DiscoveryService(client, roles, retry_delay_seconds=0, sleeper=no_sleep)
    candidates = asyncio.run(service.discover("near_completion", limit=80))
    assert [candidate.address for candidate in candidates] == ["mint-1"]
    assert client.calls[0][0] == 1
    section = client.calls[0][1]["near_completion"]
    assert len(section["launchpad_platform"]) == 8
    assert "memoo" not in section["launchpad_platform"]
    assert "token_mill" not in section["launchpad_platform"]
    assert section["launchpad_platform_v2"] is True
    assert section["filters"] == ["offchain", "onchain"]
    assert section["quote_address_type"] == [4, 5, 3, 1, 13, 0]
    assert section["min_created"] == "1m"
    assert section["max_created"] == "240m"
    for business_filter in (
        "max_rug_ratio",
        "min_top_holder_rate",
        "max_top_holder_rate",
        "renounced_mint",
        "renounced_freeze_account",
    ):
        assert business_filter not in section


def test_discovery_parser_never_relabels_unrelated_sections() -> None:
    response = {
        "data": {
            "new_creation": [{"address": "new-only"}],
            "near_completion": [],
            "completed": [{"address": "completed-only"}],
        }
    }
    assert extract_trench_candidates(response, "near_completion") == []
    found = extract_trench_candidates(response, "new_creation")
    assert [candidate.address for candidate in found] == ["new-only"]

