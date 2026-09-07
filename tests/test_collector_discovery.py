from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.app.collector.discovery import DiscoveryService, extract_trench_candidates, extract_trending_candidates
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


def test_dynamic_key_role_layout_reserves_discovery_capacity_and_redacts() -> None:
    roles = ApiKeyRoles.from_secrets([f"secret-{i}" for i in range(12)])
    assert [slot.index for slot in roles.discovery] == [0, 1]
    assert [slot.index for slot in roles.position_monitor] == list(range(3, 12))
    assert roles.discovery_fallback.index == 2
    assert [slot.index for slot in roles.realtime] == list(range(3, 12))
    assert [slot.index for slot in roles.realtime_fallback] == list(range(3, 12))
    assert [slot.index for slot in roles.kline] == list(range(3, 12))
    assert [slot.index for slot in roles.kline_fallback] == list(range(3, 12))
    assert {slot.index for slot in (*roles.discovery, roles.discovery_fallback)}.isdisjoint(
        {slot.index for slot in roles.kline}
    )
    assert {slot.index for slot in roles.all_slots} == set(range(12))
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
    assert section["min_created"] == "3m"
    assert section["max_created"] == "300m"
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



class FakeTrendingClient:
    def __init__(self) -> None:
        self.endpoints = SimpleNamespace(trenches="/v1/trenches", trending="/v1/market/rank")
        self.calls: list[tuple[int, str, dict[str, object]]] = []

    def slot_rate_limit_remaining(self, _slot) -> float:
        return 0.0

    async def request(self, slot, path, **kwargs):
        self.calls.append((slot.index, path, dict(kwargs.get("params") or {})))
        if path == self.endpoints.trenches:
            body = kwargs["json_body"]
            if "new_creation" in body:
                return {"data": {"new_creation": [{"address": "new-1"}]}}
            return {"data": {"pump": {"items": [{"address": "pump-1"}]}}}
        order_by = kwargs["params"]["order_by"]
        return {"data": {"rank": [{"address": f"{order_by}-1", "volume": 10}]}}


def test_trending_contract_and_parser_use_official_rank_fields() -> None:
    response = {
        "data": {
            "rank": [
                {"address": "mint-a", "volume": 123, "smart_degen_count": 4, "price_change_percent5m": 12.5},
                {"address": "mint-a", "volume": 122},
                {"address": "mint-b", "volume": 99},
            ]
        }
    }
    candidates = extract_trending_candidates(response, "volume")
    assert [item.address for item in candidates] == ["mint-a", "mint-b"]
    assert candidates[0].token_type == "trending"
    assert candidates[0].raw["_discovery_source"] == "trending:volume"

    params = DiscoveryService.trending_params("change5m", interval="5m")
    assert params["chain"] == "sol"
    assert params["interval"] == "5m"
    assert params["order_by"] == "change5m"
    assert params["direction"] == "desc"
    assert "limit" not in params
    assert params["min_created"] == "3m"
    assert params["max_created"] == "300m"
    assert params["min_liquidity"] == 5_000.0
    assert params["min_marketcap"] == 5_000.0
    assert params["min_holder_count"] == 30
    assert params["max_holder_count"] == 999
    assert params["min_top10_holder_rate"] == 0.14
    assert params["max_top10_holder_rate"] == 0.25
    assert params["max_insider_rate"] < 0.2
    assert params["max_insider_rate"] > 0.199999999
    assert params["max_bundler_rate"] == 0.2
    assert params["filters"] == ["renounced", "frozen", "is_internal_market"]
    assert len(params["platform"]) == 8


def test_trending_discovery_balances_reserved_keys_by_documented_route_weight() -> None:
    roles = ApiKeyRoles.from_secrets([f"key-{i}" for i in range(12)])
    client = FakeTrendingClient()
    service = DiscoveryService(client, roles, retry_delay_seconds=0, sleeper=no_sleep)
    asyncio.run(service.discover("new_creation", limit=1))
    asyncio.run(service.discover("near_completion", limit=1))
    for order_by in ("volume", "smart_degen_count", "change5m"):
        found = asyncio.run(service.discover_trending(order_by, interval="5m"))
        assert len(found) == 1
    trench_slots = [slot for slot, path, _ in client.calls if path == "/v1/trenches"]
    trend_slots = [slot for slot, path, _ in client.calls if path == "/v1/market/rank"]
    assert trench_slots == [0, 1]
    # 3+3 weighted Trenches load on slots 0/1; three weight-1 Trending calls fill slot 2 to 3.
    assert trend_slots == [2, 2, 2]
    assert service._reserved_weight == {0: 3.0, 1: 3.0, 2: 3.0}
