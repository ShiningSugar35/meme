from __future__ import annotations

import asyncio

from backend.app.services import solana_rpc


def test_alchemy_method_costs_match_current_provider_contract() -> None:
    assert solana_rpc._alchemy_method_cost("getTransactionsForAddress") == 100.0
    assert solana_rpc._alchemy_method_cost("getTransaction") == 40.0
    assert solana_rpc._alchemy_method_cost("getSlot") == 20.0
    assert 0 < solana_rpc.ALCHEMY_SAFE_CU_PER_SECOND < 300.0


def test_alchemy_governor_is_shared_across_pools_on_same_event_loop() -> None:
    async def check() -> None:
        solana_rpc._ALCHEMY_GATES.clear()
        first = solana_rpc._alchemy_cu_gate()
        second = solana_rpc._alchemy_cu_gate()
        assert first is second
        assert first.rate_cu_per_second == solana_rpc.ALCHEMY_SAFE_CU_PER_SECOND

    asyncio.run(check())


def test_configured_rpc_endpoints_never_include_ankr() -> None:
    endpoints = solana_rpc.configured_rpc_endpoints()
    assert endpoints
    assert all(endpoint.provider in {"alchemy", "solana_public"} for endpoint in endpoints)
    assert all("ankr" not in endpoint.label.lower() for endpoint in endpoints)
