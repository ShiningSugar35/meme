"""Stage 2.5 regression tests — thresholds, discovery multi-x, SIM buy/sell, price runner, smart degen."""

import asyncio
import math
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio

from ..config import settings, ProviderMode
from ..db.repositories import Repositories
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcRealProvider
from ..trading.executor import TradingPipeline
from ..strategy.thresholds import (
    compute_thresholds, StrategyThresholds, build_trench_filters_for_x,
    strip_internal_debug_fields, entry_size_usd,
)
from ..strategy.filters import evaluate_smart_degen, run_entry_local_risk_filter
from ..runners.active_position_price_runner import ActivePositionPriceRunner


# ============================================================================
# 1. Thresholds tests
# ============================================================================

class TestThresholds:
    def test_x_02_formulas(self):
        t = compute_thresholds(0.2)
        assert math.isclose(t.common_risk, 0.15, rel_tol=1e-9)
        assert math.isclose(t.min_liquidity, 5250.0, rel_tol=1e-9)
        assert math.isclose(t.min_top_holder_rate, 0.145, rel_tol=1e-9)
        assert math.isclose(t.max_top_holder_rate, 0.275, rel_tol=1e-9)
        assert math.isclose(t.max_fresh_wallet_rate, 0.15, rel_tol=1e-9)
        assert math.isclose(t.max_creator_balance_rate, 0.051, rel_tol=1e-9)
        assert t.min_holder_count_api == 30
        assert math.isclose(t.min_marketcap_api, 2900.0, rel_tol=1e-9)
        assert t.min_smart_degen_count_api == 1
        assert t.min_smart_degen_count_raw == 0.0
        assert math.isclose(t.min_volume_24h, 1200.0, rel_tol=1e-9)
        assert math.isclose(t.price_change_1h_min_pct, 5.0, rel_tol=1e-9)
        assert math.isclose(t.volume_per_swap_5m_min, 10.0, rel_tol=1e-9)
        assert math.isclose(t.swaps_5m_multiplier, 1.25, rel_tol=1e-9)

    def test_x_05_formulas(self):
        t = compute_thresholds(0.5)
        assert math.isclose(t.common_risk, 0.30, rel_tol=1e-9)
        assert math.isclose(t.min_liquidity, 4500.0, rel_tol=1e-9)
        assert math.isclose(t.min_top_holder_rate, 0.13, rel_tol=1e-9)
        assert math.isclose(t.max_top_holder_rate, 0.35, rel_tol=1e-9)
        assert math.isclose(t.min_holder_count_raw, 17.0, rel_tol=1e-9)
        assert t.min_holder_count_api == 18
        assert math.isclose(t.price_change_1h_min_pct, -10.0, rel_tol=1e-9)
        assert math.isclose(t.volume_per_swap_5m_min, 4.0, rel_tol=1e-9)
        assert math.isclose(t.swaps_5m_multiplier, 0.5, rel_tol=1e-9)
        assert t.min_smart_degen_count_api is None
        assert t.min_smart_degen_count_raw == -3.0

    def test_build_trench_filters_returns_constants(self):
        payload = build_trench_filters_for_x(0.2)
        assert "_x" not in payload
        assert "_computed_from_x" not in payload
        assert isinstance(payload["min_liquidity"], float)
        assert isinstance(payload["min_holder_count"], int)
        assert payload["min_liquidity"] == 5250.0
        assert payload["min_holder_count"] == 30

    def test_default_x_when_no_strategy(self):
        assert math.isclose(settings.STRATEGY_DEFAULT_X, 0.20, rel_tol=1e-9)


# ============================================================================
# 2. Smart degen required_count bounds (P1)
# ============================================================================

class TestSmartDegenBounds:
    def test_smart_degen_x_02_normal(self):
        holders = [
            {"address": "a1", "amount_percentage": 0.02, "usd_value": 200.0},
            {"address": "a2", "amount_percentage": 0.01, "usd_value": 100.0},
        ]
        res = asyncio.run(evaluate_smart_degen({"x": 0.2}, holders))
        assert res.feature_vector["required_count"] == 1
        assert res.passed

    def test_smart_degen_x_03_no_crash(self):
        holders = [
            {"address": "a1", "amount_percentage": 0.02, "usd_value": 200.0},
        ]
        res = asyncio.run(evaluate_smart_degen({"x": 0.3}, holders))
        assert "required_count" in res.feature_vector
        assert res.feature_vector["degen_hold_ok"] in (True, False)

    def test_smart_degen_x_05_no_crash(self):
        holders = [
            {"address": "a1", "amount_percentage": 0.02, "usd_value": 200.0},
        ]
        res = asyncio.run(evaluate_smart_degen({"x": 0.5}, holders))
        assert "required_count" in res.feature_vector

    def test_smart_degen_empty_holders_fails(self):
        res = asyncio.run(evaluate_smart_degen({"x": 0.2}, []))
        assert not res.passed


# ============================================================================
# 3. SIM create_position (P0)
# ============================================================================

@pytest_asyncio.fixture
async def sim_pipeline(repo):
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
    jito = JitoProvider(repo, mode=ProviderMode.MOCK)
    rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
    return TradingPipeline(repo, gmgn, jupiter, jito, rpc)


@pytest.mark.asyncio
async def test_sim_create_position_no_type_error(repo, sim_pipeline):
    """SIM strategy passes discovery and create_position does not throw TypeError."""
    strategy = {"id": 1, "config_version": 1, "x": 0.2, "is_live": False}
    result = await sim_pipeline.handle_token_second_filter_result(
        "PASS1", [strategy], snapshot_id=1
    )
    assert result["status"] == "OK"
    positions = await repo.list_positions_by_token_and_is_live("PASS1", False)
    assert len(positions) >= 1
    pos = positions[0]
    assert pos["entry_price_usd"] is not None and pos["entry_price_usd"] > 0
    assert pos["entry_token_amount"] is not None and pos["entry_token_amount"] > 0
    assert pos["remaining_token_amount"] == pos["entry_token_amount"]
    assert pos["remaining_value_usd"] is not None


# ============================================================================
# 4. SIM paper exit (P0)
# ============================================================================

@pytest.mark.asyncio
async def test_sim_50pct_exit_updates_remaining(repo):
    """50% SIM exit updates remaining_token_amount and remaining_value_usd."""
    from ..runners.position_risk_runner import PositionRiskRunner
    pos_id = await repo.create_position(
        token_mint="SIMEXIT", is_live=False,
        locked_strategy_config_json='{"x": 0.2}',
        status="POSITION_OPEN", entry_price_usd=0.01,
        entry_token_amount=1000.0, remaining_token_amount=1000.0,
        remaining_value_usd=10.0, account_type="SIM",
    )
    pos = await repo.get_position(pos_id)
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    runner = PositionRiskRunner(repo, gmgn)
    await runner._paper_exit(
        position=pos, exit_pct=0.5,
        reason_code="TEST_HALF_SELL", current_price_usd=0.01,
    )
    updated = await repo.get_position(pos_id)
    assert updated["status"] != "CLOSED"
    assert updated["remaining_token_amount"] == 500.0
    assert updated["remaining_value_usd"] == 5.0


@pytest.mark.asyncio
async def test_sim_100pct_exit_closes(repo):
    """100% SIM exit closes the position."""
    from ..runners.position_risk_runner import PositionRiskRunner
    pos_id = await repo.create_position(
        token_mint="SIMEXIT", is_live=False,
        locked_strategy_config_json='{"x": 0.2}',
        status="POSITION_OPEN", entry_price_usd=0.01,
        entry_token_amount=1000.0, remaining_token_amount=1000.0,
        remaining_value_usd=10.0, account_type="SIM",
    )
    pos = await repo.get_position(pos_id)
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    runner = PositionRiskRunner(repo, gmgn)
    await runner._paper_exit(
        position=pos, exit_pct=1.0,
        reason_code="TEST_FULL_EXIT", current_price_usd=0.01,
    )
    updated = await repo.get_position(pos_id)
    assert updated["status"] == "CLOSED"


@pytest.mark.asyncio
async def test_sim_exit_no_unexpected_kwargs(repo):
    """Partial exit should not pass unsupported kwargs."""
    from ..runners.position_risk_runner import PositionRiskRunner
    pos_id = await repo.create_position(
        token_mint="SIMEXIT", is_live=False,
        locked_strategy_config_json='{"x": 0.2}',
        status="POSITION_OPEN", entry_price_usd=0.01,
        entry_token_amount=1000.0, remaining_token_amount=1000.0,
        remaining_value_usd=10.0, account_type="SIM",
    )
    pos = await repo.get_position(pos_id)
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    runner = PositionRiskRunner(repo, gmgn)
    await runner._paper_exit(
        position=pos, exit_pct=0.3,
        reason_code="TEST_PARTIAL", current_price_usd=0.01,
    )
    # Should not raise TypeError
    assert True


# ============================================================================
# 5. ActivePositionPriceRunner (P0)
# ============================================================================

class TestActivePriceRunnerRegistration:
    def test_worker_can_be_registered(self):
        assert hasattr(ActivePositionPriceRunner, "run_once")
        assert hasattr(ActivePositionPriceRunner, "__init__")

    @pytest.mark.asyncio
    async def test_worker_run_once_no_crash(self, repo):
        """ActivePositionPriceRunner.run_once does not crash with no positions."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)
        await runner.run_once()


# ============================================================================
# 6. TOP3 smart degen (P1)
# ============================================================================

@pytest.mark.asyncio
async def test_top3_wallet_disappears_triggers(repo):
    """Wallet not in current holders list -> treat as 100% reduction -> trigger."""
    from ..runners.position_risk_runner import PositionRiskRunner
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    runner = PositionRiskRunner(repo, gmgn)

    locked = json.dumps({
        "x": 0.2,
        "top3_smart_degen_snapshot": [
            {"address": "wallet1", "amount_percentage": 0.02, "usd_value": 200.0, "token_amount": 100.0},
        ],
    })
    pos_id = await repo.create_position(
        token_mint="TOP3TKN", is_live=False,
        locked_strategy_config_json=locked,
        status="POSITION_OPEN", entry_price_usd=0.01,
        entry_token_amount=1000.0, remaining_token_amount=1000.0,
        remaining_value_usd=10.0, account_type="SIM",
    )
    pos = await repo.get_position(pos_id)
    now = datetime.now(timezone.utc)

    import backend.app.providers.gmgn_real as gmgn_mod
    original = gmgn_mod.GMGNProvider.fetch_smart_degen_holders
    async def empty_fetch(*args, **kwargs):
        return []
    gmgn_mod.GMGNProvider.fetch_smart_degen_holders = empty_fetch
    try:
        result = await runner._check_top3_smart_degen_reduction(pos, now)
        assert result is not None
    finally:
        gmgn_mod.GMGNProvider.fetch_smart_degen_holders = original


@pytest.mark.asyncio
async def test_top3_reduction_25pct_triggers(repo):
    """Wallet reduction >25% should trigger 50% exit."""
    from ..runners.position_risk_runner import PositionRiskRunner
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    runner = PositionRiskRunner(repo, gmgn)

    locked = json.dumps({
        "x": 0.2,
        "top3_smart_degen_snapshot": [
            {"address": "wallet1", "amount_percentage": 0.02, "usd_value": 200.0, "token_amount": 100.0},
        ],
    })
    pos_id = await repo.create_position(
        token_mint="TOP3TKN", is_live=False,
        locked_strategy_config_json=locked,
        status="POSITION_OPEN", entry_price_usd=0.01,
        entry_token_amount=1000.0, remaining_token_amount=1000.0,
        remaining_value_usd=10.0, account_type="SIM",
    )
    pos = await repo.get_position(pos_id)
    now = datetime.now(timezone.utc)

    import backend.app.providers.gmgn_real as gmgn_mod
    original = gmgn_mod.GMGNProvider.fetch_smart_degen_holders
    async def reduced_holders(*args, **kwargs):
        return [{"address": "wallet1", "amount_percentage": 0.014, "usd_value": 140.0, "token_amount": 70.0}]
    gmgn_mod.GMGNProvider.fetch_smart_degen_holders = reduced_holders
    try:
        result = await runner._check_top3_smart_degen_reduction(pos, now)
        assert result is not None
    finally:
        gmgn_mod.GMGNProvider.fetch_smart_degen_holders = original


@pytest.mark.asyncio
async def test_top3_same_wallet_idempotent(repo):
    """Same wallet should not trigger twice after first trigger."""
    from ..runners.position_risk_runner import PositionRiskRunner
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    runner = PositionRiskRunner(repo, gmgn)

    locked = json.dumps({
        "x": 0.2,
        "top3_smart_degen_snapshot": [
            {"address": "wallet1", "amount_percentage": 0.02, "usd_value": 200.0, "token_amount": 100.0},
        ],
    })
    pos_id = await repo.create_position(
        token_mint="TOP3TKN", is_live=False,
        locked_strategy_config_json=locked,
        status="POSITION_OPEN", entry_price_usd=0.01,
        entry_token_amount=1000.0, remaining_token_amount=1000.0,
        remaining_value_usd=10.0, account_type="SIM",
    )
    pos = await repo.get_position(pos_id)
    now = datetime.now(timezone.utc)

    import backend.app.providers.gmgn_real as gmgn_mod
    original = gmgn_mod.GMGNProvider.fetch_smart_degen_holders
    async def reduced_holders(*args, **kwargs):
        return [{"address": "wallet1", "amount_percentage": 0.014, "usd_value": 140.0, "token_amount": 70.0}]
    gmgn_mod.GMGNProvider.fetch_smart_degen_holders = reduced_holders
    try:
        result1 = await runner._check_top3_smart_degen_reduction(pos, now)
        assert result1 is not None

        # Mark exit rule as executed
        await repo.mark_exit_rule_executed(pos_id, "TOP3_SMART_DEGEN_DUMP:wallet1")
        pos = await repo.get_position(pos_id)
        result2 = await runner._check_top3_smart_degen_reduction(pos, now)
        assert result2 is None
    finally:
        gmgn_mod.GMGNProvider.fetch_smart_degen_holders = original


# ============================================================================
# 7. Price field aliases
# ============================================================================

class TestPriceFieldAliases:
    def test_price_change_percent1h_direct(self):
        from ..strategy.filters import evaluate_price_activity_rules
        token = {"price_usd": 0.01}
        strategy = {"x": 0.2}
        latest = {
            "price_usd": 0.011,
            "price_change_percent1h": 10.5,
            "swaps_5m": 15,
            "swaps_1h": 100,
            "volume_5m": 2000,
        }
        res = asyncio.run(evaluate_price_activity_rules(token, strategy, latest))
        detail = next(d for d in res.details if d["rule"] == "price_change_1h")
        assert detail["passed"] is True
        assert detail["source"] == "direct_price_change_percent1h"

    def test_volume_5m_from_nested_pool(self):
        from ..strategy.filters import evaluate_price_activity_rules
        token = {"price_usd": 0.01}
        strategy = {"x": 0.2}
        latest = {
            "price_usd": 0.011,
            "price_change_percent1h": 10.5,
            "swaps_5m": 15,
            "swaps_1h": 100,
            "pool": {"volume_5m": 2000},
        }
        res = asyncio.run(evaluate_price_activity_rules(token, strategy, latest))
        vol_detail = next(d for d in res.details if d["rule"] == "volume_per_swap_5m")
        assert vol_detail["volume_5m"] == 2000

    def test_missing_price_change_logs(self):
        from ..strategy.filters import evaluate_price_activity_rules
        token = {"price_usd": 0.01}
        strategy = {"x": 0.2}
        latest = {
            "price_usd": 0.011,
            "swaps_5m": 15,
            "swaps_1h": 100,
            "volume_5m": 2000,
        }
        res = asyncio.run(evaluate_price_activity_rules(token, strategy, latest))
        detail = next(d for d in res.details if d["rule"] == "price_change_1h")
        assert detail["source"] == "missing" or detail["source"].startswith("computed")
