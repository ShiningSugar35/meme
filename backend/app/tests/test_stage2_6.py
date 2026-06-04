"""Stage 2.6 regression tests — trenches pushdown, default x, price_change, top1 normalize, SIM/LIVE isolation."""

import asyncio
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from ..config import settings, ProviderMode
from ..db.repositories import Repositories
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcRealProvider
from ..runners.discovery_runner import DiscoveryRunner
from ..runners.active_position_price_runner import ActivePositionPriceRunner
from ..runners.position_risk_runner import PositionRiskRunner
from ..strategy.thresholds import (
    compute_thresholds, build_trench_filters_for_x,
    strip_internal_debug_fields, normalize_rate_fraction,
    KNOWN_TRENCH_FILTER_KEYS,
)
from ..strategy.filters import evaluate_price_activity_rules, evaluate_top1_holder
from ..trading.executor import TradingPipeline


# ============================================================================
# Issue 1: Trenches params pushdown to GMGN
# ============================================================================

class TestTrenchesPushdown:
    """Verify that trenches filter constants reach the GMGN POST body."""

    def test_discovery_runner_has_callable_run_once(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = DiscoveryRunner(repo=repo, gmgn=gmgn, strategy_groups=[])
        assert callable(runner.run_once)

    def test_build_trench_filters_x02_constants(self):
        """x=0.2 produces expected GMGN-sendable constants."""
        payload = build_trench_filters_for_x(0.2)
        assert math.isclose(payload["max_rug_ratio"], 0.15, rel_tol=1e-9)
        assert math.isclose(payload["max_entrapment_ratio"], 0.15, rel_tol=1e-9)
        assert math.isclose(payload["max_insider_ratio"], 0.15, rel_tol=1e-9)
        assert math.isclose(payload["max_bundler_rate"], 0.15, rel_tol=1e-9)
        assert math.isclose(payload["min_liquidity"], 5000.0, rel_tol=1e-9)
        assert math.isclose(payload["min_top_holder_rate"], 0.145, rel_tol=1e-9)
        assert math.isclose(payload["max_top_holder_rate"], 0.275, rel_tol=1e-9)
        assert math.isclose(payload["max_fresh_wallet_rate"], 0.15, rel_tol=1e-9)
        assert math.isclose(payload["max_creator_balance_rate"], 0.051, rel_tol=1e-9)
        assert payload["min_holder_count"] == 30
        assert math.isclose(payload["min_marketcap"], 2900.0, rel_tol=1e-9)
        assert math.isclose(payload["min_volume_24h"], 1200.0, rel_tol=1e-9)
        assert payload.get("min_smart_degen_count") == 1

    def test_no_internal_fields_in_stripped_payload(self):
        """build_trench_filters_for_x must strip _x, _computed_from_x, _strategy_group_ids."""
        payload = build_trench_filters_for_x(0.2)
        assert "_x" not in payload
        assert "_computed_from_x" not in payload
        assert "_strategy_group_ids" not in payload
        assert "trench_filters" not in payload

    def test_strip_internal_debug_fields_removes_underscore_prefixed(self):
        raw = {"max_rug_ratio": 0.15, "_x": 0.2, "_computed_from_x": 0.2, "_strategy_group_ids": [1]}
        cleaned = strip_internal_debug_fields(raw)
        assert "max_rug_ratio" in cleaned
        assert "_x" not in cleaned
        assert "_computed_from_x" not in cleaned
        assert "_strategy_group_ids" not in cleaned

    def test_known_filter_keys_set(self):
        """KNOWN_TRENCH_FILTER_KEYS covers all GMGN-sendable constants."""
        payload = build_trench_filters_for_x(0.2)
        for k in KNOWN_TRENCH_FILTER_KEYS:
            assert k in payload, f"KNOWN_TRENCH_FILTER_KEYS missing key {k}"

    @pytest.mark.asyncio
    async def test_discovery_runner_send_params_no_internal_fields(self, repo):
        """DiscoveryRunner._fetch_trenches_two_group must strip internal fields before sending."""
        mock_gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)

        strategy_group = {"id": 1, "config_version": 1, "x": 0.2, "is_live": False}
        runner = DiscoveryRunner(
            repo=repo,
            gmgn=mock_gmgn,
            strategy_groups=[strategy_group],
        )
        runner._load_enabled_strategy_groups = AsyncMock(return_value=[strategy_group])

        # Spy on _try_fetch_group to capture the params sent to fetch_trenches
        original_try = runner._try_fetch_group
        captured_params = []

        async def spy_try_fetch(group_name, platforms, request_slot, role, custom_params=None):
            captured_params.append(dict(custom_params or {}))
            return await original_try(group_name, platforms, request_slot, role, custom_params=custom_params)

        runner._try_fetch_group = spy_try_fetch

        await runner.run_once()

        for p in captured_params:
            assert "_x" not in p, f"params contains _x: {p}"
            assert "_computed_from_x" not in p, f"params contains _computed_from_x: {p}"
            assert "_strategy_group_ids" not in p, f"params contains _strategy_group_ids: {p}"
            assert "trench_filters" not in p, f"params contains trench_filters: {p}"
            for k in KNOWN_TRENCH_FILTER_KEYS:
                if k in ("min_smart_degen_count",):
                    continue
                assert k in p, f"params missing {k}: {p.keys()}"

    @pytest.mark.asyncio
    async def test_fetch_trenches_two_group_strips_internal_request_fields(self, repo, monkeypatch):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = DiscoveryRunner(repo=repo, gmgn=gmgn, strategy_groups=[])
        monkeypatch.setattr(type(settings), "get_provider_mode", lambda self: ProviderMode.ONLINE_READONLY)
        monkeypatch.setattr(settings, "GMGN_DISCOVERY_GROUP_DELAY_SECONDS", 0.0)

        captured_params = []

        async def spy_try_fetch(group_name, platforms, request_slot, role, custom_params=None):
            captured_params.append(dict(custom_params or {}))
            return [], {
                "group_name": group_name,
                "platforms": platforms,
                "slot": request_slot,
                "role": role,
                "ok": True,
                "raw_count": 0,
                "unique_count": 0,
                "duplicate_count": 0,
                "status_code": None,
                "error": None,
                "latency_ms": 0,
            }

        runner._try_fetch_group = spy_try_fetch
        await runner._fetch_trenches_two_group(custom_params={
            "trench_filters": build_trench_filters_for_x(0.2),
            "_x": 0.2,
            "_computed_from_x": 0.2,
            "_debug_x": 0.2,
            "_strategy_group_ids": [1],
        })

        assert captured_params
        for params in captured_params:
            assert "trench_filters" not in params
            assert not any(key.startswith("_") for key in params), params

    @pytest.mark.asyncio
    async def test_run_once_uses_default_x_for_trenches_filters(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        strategy_group = {"id": 1, "config_version": 1, "is_live": False}
        runner = DiscoveryRunner(repo=repo, gmgn=gmgn, strategy_groups=[strategy_group])
        runner._load_enabled_strategy_groups = AsyncMock(return_value=[strategy_group])
        captured_custom_params = []

        async def fake_fetch(custom_params=None):
            captured_custom_params.append(dict(custom_params or {}))
            return [], {"groups": [], "unique_fetched_count": 0, "raw_fetched_count": 0}

        runner._fetch_trenches_two_group = fake_fetch
        await runner.run_once()

        assert captured_custom_params
        params = captured_custom_params[0]
        assert math.isclose(params["_x"], settings.STRATEGY_DEFAULT_X, rel_tol=1e-9)
        filters = params["trench_filters"]
        assert math.isclose(filters["max_rug_ratio"], 0.15, rel_tol=1e-9)
        assert math.isclose(filters["min_liquidity"], 5000.0, rel_tol=1e-9)

    @pytest.mark.asyncio
    async def test_save_top3_baselines_does_not_run_discovery_loop(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = DiscoveryRunner(repo=repo, gmgn=gmgn, strategy_groups=[])
        runner._run_once_locked = AsyncMock()
        position_id = await repo.create_position(
            token_mint="BASELINE_SAVE_TEST", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="POSITION_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
        )

        await runner._save_top3_smart_money_baselines(position_id, "BASELINE_SAVE_TEST", [
            {"address": "wallet1", "amount_percentage": 2.0, "usd_value": 200.0},
        ])

        runner._run_once_locked.assert_not_called()
        baselines = await repo.get_position_smart_money_baselines(position_id)
        assert len(baselines) == 1
        assert math.isclose(baselines[0]["baseline_amount_percentage"], 0.02, rel_tol=1e-9)

    @pytest.mark.asyncio
    async def test_save_top3_baselines_does_not_lookup_position_by_token(self):
        class BaselineRepo:
            def __init__(self):
                self.inserted = []

            async def delete_smart_money_baselines_for_position(self, position_id):
                self.deleted_position_id = position_id

            async def insert_smart_money_baseline(self, *args):
                self.inserted.append(args)

            async def list_positions_by_token(self, *args, **kwargs):
                raise AssertionError("must not lookup positions by token")

        fake_repo = BaselineRepo()
        runner = DiscoveryRunner(repo=fake_repo, gmgn=None, strategy_groups=[])

        await runner._save_top3_smart_money_baselines(123, "NO_LOOKUP_TEST", [
            {"address": "wallet1", "amount_percentage": 0.02, "usd_value": 200.0},
        ])

        assert fake_repo.deleted_position_id == 123
        assert fake_repo.inserted[0][0] == 123


# ============================================================================
# Issue 2: Default x=0.2
# ============================================================================

class TestDefaultX:
    def test_strategy_default_x_is_02(self):
        assert math.isclose(settings.STRATEGY_DEFAULT_X, 0.20, rel_tol=1e-9)

    def test_unique_x_values_fallback_to_default(self):
        """Strategy group with null x falls back to STRATEGY_DEFAULT_X."""
        groups = [
            {"id": 1, "x": None, "is_live": False},
            {"id": 2, "x": 0.5, "is_live": False},
        ]
        xs = DiscoveryRunner._unique_x_values(groups)
        assert len(xs) == 2
        assert math.isclose(xs[0], 0.2, rel_tol=1e-9)
        assert math.isclose(xs[1], 0.5, rel_tol=1e-9)


class TestTop3BaselineFallback:
    @pytest.mark.asyncio
    async def test_baseline_table_fallback_no_unboundlocalerror(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = PositionRiskRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="TOP3_FALLBACK_TEST", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="POSITION_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
        )
        await repo.insert_smart_money_baseline(
            pos_id, "TOP3_FALLBACK_TEST", "wallet1", 1, 0.02, 200.0,
        )
        pos = await repo.get_position(pos_id)
        gmgn.fetch_smart_degen_holders = AsyncMock(return_value=[
            {"address": "wallet1", "amount_percentage": 0.02, "usd_value": 200.0},
        ])

        result = await runner._check_top3_smart_degen_reduction(pos, datetime.now(timezone.utc))

        assert result is None

    @pytest.mark.asyncio
    async def test_fallback_amount_percentage_reduction_triggers(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = PositionRiskRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="TOP3_PERCENT_TEST", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="POSITION_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
        )
        await repo.insert_smart_money_baseline(
            pos_id, "TOP3_PERCENT_TEST", "wallet1", 1, 0.02, 200.0,
        )
        pos = await repo.get_position(pos_id)
        gmgn.fetch_smart_degen_holders = AsyncMock(return_value=[
            {"address": "wallet1", "amount_percentage": 0.014, "usd_value": 140.0},
        ])

        result = await runner._check_top3_smart_degen_reduction(pos, datetime.now(timezone.utc))

        assert result is not None

    @pytest.mark.asyncio
    async def test_current_percentage_normalizes_percent_units(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = PositionRiskRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="TOP3_PERCENT_UNITS_TEST", is_live=False,
            locked_strategy_config_json=json.dumps({
                "x": 0.2,
                "top3_smart_degen_snapshot": [
                    {"address": "wallet1", "amount_percentage": 0.02, "usd_value": 200.0},
                ],
            }),
            status="POSITION_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
        )
        pos = await repo.get_position(pos_id)
        gmgn.fetch_smart_degen_holders = AsyncMock(return_value=[
            {"address": "wallet1", "amount_percentage": 1.4, "usd_value": 140.0},
        ])

        result = await runner._check_top3_smart_degen_reduction(pos, datetime.now(timezone.utc))

        assert result is not None


# ============================================================================
# Issue 3: price_change_percent1h reading logic
# ============================================================================

class TestPriceChangePercent1h:
    def test_top_level_takes_priority(self):
        """When top-level price_change_percent1h exists, nested must NOT overwrite."""
        token = {"price_usd": 0.01}
        strategy = {"x": 0.2}
        latest = {
            "price_usd": 0.011,
            "price_change_percent1h": 20.0,
            "swaps_5m": 15,
            "swaps_1h": 100,
            "volume_5m": 2000,
            "pool": {"price_change_percent1h": 5.0},
        }
        res = asyncio.run(evaluate_price_activity_rules(token, strategy, latest))
        detail = next(d for d in res.details if d["rule"] == "price_change_1h")
        assert detail["passed"] is True
        assert detail["pct_change"] == 20.0
        assert detail["source"] == "direct_price_change_percent1h"

    def test_nested_fallback_when_top_missing(self):
        """When top-level lacks price_change_percent1h, nested container is used."""
        token = {"price_usd": 0.01}
        strategy = {"x": 0.2}
        latest = {
            "price_usd": 0.011,
            "swaps_5m": 15,
            "swaps_1h": 100,
            "volume_5m": 2000,
            "pool": {"price_change_percent1h": 20.0},
        }
        res = asyncio.run(evaluate_price_activity_rules(token, strategy, latest))
        detail = next(d for d in res.details if d["rule"] == "price_change_1h")
        assert detail["passed"] is True
        assert detail["pct_change"] == 20.0
        assert detail["source"] == "direct_price_change_percent1h"

    def test_both_missing_computed_from_price_1h(self):
        """When neither top nor nested has the field, falls back to computed."""
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
        assert detail["source"] in ("missing", "computed_from_price_1h")


# ============================================================================
# Issue 4: Top1 holder rate normalization
# ============================================================================

class TestNormalizeRateFraction:
    def test_none_returns_none(self):
        assert normalize_rate_fraction(None) is None

    def test_decimal_kept(self):
        assert normalize_rate_fraction(0.05) == 0.05

    def test_percentage_divided(self):
        assert normalize_rate_fraction(5.0) == 0.05

    def test_boundary_zero(self):
        assert normalize_rate_fraction(0.0) == 0.0

    def test_boundary_one(self):
        assert normalize_rate_fraction(1.0) == 1.0

    def test_edge_101(self):
        assert math.isclose(normalize_rate_fraction(10.1), 0.101, rel_tol=1e-9)

    def test_out_of_range_returns_none(self):
        assert normalize_rate_fraction(150.0) is None

class TestTop1HolderNormalization:
    def test_x02_top1_passes_at_005(self):
        """x=0.2, top1 rate=5% (0.05) should pass (threshold=0.051)."""
        t = compute_thresholds(0.2)
        assert math.isclose(t.top1_addr_type0_max, 0.051, rel_tol=1e-9)
        holder = {"addr_type": 0, "amount_percentage": 5.0}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is True
        assert math.isclose(res.feature_vector["top1_holder_rate"], 0.05, rel_tol=1e-9)

    def test_x02_top1_fails_at_006(self):
        """x=0.2, top1 rate=6% should fail (threshold=0.051)."""
        holder = {"addr_type": 0, "amount_percentage": 6.0}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is False
        assert math.isclose(res.feature_vector["top1_holder_rate"], 0.06, rel_tol=1e-9)

    def test_x02_top1_decimal_005_passes(self):
        """Already decimal 0.05 also passes."""
        holder = {"addr_type": 0, "amount_percentage": 0.05}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is True

    def test_x02_top1_decimal_006_fails(self):
        """Already decimal 0.06 fails."""
        holder = {"addr_type": 0, "amount_percentage": 0.06}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is False

    def test_top1_rate_via_rate_key(self):
        """Test normalization with 'rate' key."""
        holder = {"addr_type": 0, "rate": 5.0}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is True
        assert math.isclose(res.feature_vector["top1_holder_rate"], 0.05, rel_tol=1e-9)


# ============================================================================
# Issue 5: SIM stop-loss does not send real transactions
# ============================================================================

class TestSimStopLossIsolation:
    @pytest.mark.asyncio
    async def test_sim_hard_tp_closes_position_without_pipeline(self, repo):
        """SIM HARD_TP_270 trigger closes position, does NOT call execute_sell."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="SIM_TP_TEST", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="POSITION_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )
        pos = await repo.get_position(pos_id)
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.028, "price": 0.028,
        })):
            await runner._process_position(pos, datetime.now(timezone.utc))
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "CLOSED", "SIM position closed on HARD_TP_270"

    @pytest.mark.asyncio
    async def test_sim_risk_recheck_paper_exit_closes(self, repo):
        """SIM risk recheck failure via paper exit closes position."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = PositionRiskRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="SIM_RISK_TEST", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="POSITION_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
        )
        pos = await repo.get_position(pos_id)
        # Paper exit with full pct -> closes
        await runner._paper_exit(
            position=pos, exit_pct=1.0,
            reason_code="RISK_RECHECK_FAILED", current_price_usd=0.01,
        )
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "CLOSED", "SIM position closed on risk fail paper exit"

    @pytest.mark.asyncio
    async def test_sim_top3_paper_exit_updates_remaining(self, repo):
        """SIM TOP3 smart degen dump partial paper exit updates remaining."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = PositionRiskRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="SIM_TOP3_TEST", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="POSITION_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
        )
        pos = await repo.get_position(pos_id)
        await runner._paper_exit(
            position=pos, exit_pct=0.5,
            reason_code="TOP3_SMART_DEGEN_DUMP", current_price_usd=0.01,
        )
        updated = await repo.get_position(pos_id)
        assert updated["status"] != "CLOSED", "50% exit keeps position open"
        assert updated["remaining_token_amount"] == 500.0
        assert math.isclose(updated["remaining_value_usd"], 5.0, rel_tol=1e-9)

    @pytest.mark.asyncio
    async def test_live_position_calls_execute_sell(self, repo):
        """LIVE position exit calls TradingPipeline.execute_sell."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)
        runner = PositionRiskRunner(repo, gmgn)
        runner.set_trading_pipeline(pipeline)
        pos_id = await repo.create_position(
            token_mint="LIVE_EXIT_TEST", is_live=True,
            locked_strategy_config_json='{"x": 0.2}',
            status="POSITION_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="LIVE",
        )
        pos = await repo.get_position(pos_id)
        pipeline.execute_sell = AsyncMock(return_value={"ok": True})
        await runner._request_exit(
            position=pos, exit_pct=1.0,
            reason_code="RISK_RECHECK_FAILED", emergency=True,
            latest={}, current_price_usd=0.01,
        )
        pipeline.execute_sell.assert_called_once()
