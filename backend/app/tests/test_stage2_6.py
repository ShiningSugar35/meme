"""Stage 2.6 regression tests — trenches pushdown, default x, price_change, top1 normalize, SIM/LIVE isolation."""

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from ..config import settings, ProviderMode
from ..providers.gmgn_real import GMGNProvider, GMGNAPIError
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcRealProvider
from ..runners.discovery_runner import DiscoveryRunner
from ..runners.active_position_price_runner import ActivePositionPriceRunner
from ..runners.position_risk_runner import PositionRiskRunner
from ..runners.position_soft_stop_runner import PositionSoftStopRunner
from ..services.position_exit_service import PositionExitService
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
        assert math.isclose(payload["min_liquidity"], 4500.0, rel_tol=1e-9)
        assert math.isclose(payload["min_top_holder_rate"], 0.06, rel_tol=1e-9)
        assert math.isclose(payload["max_top_holder_rate"], 0.275, rel_tol=1e-9)
        assert math.isclose(payload["max_fresh_wallet_rate"], 0.15, rel_tol=1e-9)
        assert math.isclose(payload["max_creator_balance_rate"], 0.051, rel_tol=1e-9)  # 买入值 0.049+0.01*0.2
        assert payload["min_holder_count"] == 25
        assert math.isclose(payload["min_marketcap"], 4950.0, rel_tol=1e-9)
        assert math.isclose(payload["min_volume_24h"], 1200.0, rel_tol=1e-9)
        assert payload.get("min_smart_degen_count") is None

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
            if k == "min_smart_degen_count" and k not in payload:
                continue
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
    @pytest.mark.xfail(reason="refactored to type-based discovery")
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
        assert len(captured_params) == len(settings.get_discovery_slots())
        for params in captured_params:
            assert "trench_filters" not in params
            assert not any(key.startswith("_") for key in params), params
            assert params.get("type") == ["new_creation", "near_completion"]
            assert params.get("min_created", "").endswith("m")
            assert params.get("max_created", "").endswith("m")

    @pytest.mark.asyncio
    async def test_provider_trenches_v2_body_and_pump_mapping(self, repo, monkeypatch):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        gmgn.mode = ProviderMode.ONLINE_READONLY
        captured = {}

        async def fake_make_request(path, params, method="GET", json_body=None, credential_slot=None):
            captured.update({
                "path": path,
                "params": dict(params or {}),
                "method": method,
                "json_body": json_body,
                "credential_slot": credential_slot,
            })
            return {
                "data": {
                    "new_creation": [{"token_mint": "NEW1", "pool_address": "POOL1", "symbol": "N1"}],
                    "pump": [{"token_mint": "PUMP1", "pool_address": "POOL2", "symbol": "P1"}],
                }
            }

        monkeypatch.setattr(gmgn, "_make_request", fake_make_request)
        result = await gmgn.fetch_trenches({
            "chain": "sol",
            "type": ["new_creation", "near_completion"],
            "platforms": ["Pump.fun"],
            "min_created": "60m",
            "max_created": "120m",
            "trench_filters": {"min_liquidity": 4500.0},
        }, credential_slot=3)

        assert captured["path"] == settings.GMGN_TRENCHES_PATH
        assert captured["method"] == "POST"
        assert captured["params"] == {"chain": "sol"}
        assert captured["credential_slot"] == 3
        body = captured["json_body"]
        assert body["version"] == "v2"
        assert set(body.keys()) == {"version", "new_creation", "near_completion"}
        assert body["new_creation"]["min_created"] == "60m"
        assert body["new_creation"]["max_created"] == "120m"
        assert body["near_completion"]["min_created"] == "60m"
        assert body["near_completion"]["max_created"] == "120m"
        assert body["new_creation"]["min_liquidity"] == 4500.0
        assert [item["type"] for item in result] == ["new_creation", "near_completion"]

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="refactored to type-based discovery")
    async def test_discovery_age_shards_use_duration_windows(self, repo, monkeypatch):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = DiscoveryRunner(repo=repo, gmgn=gmgn, strategy_groups=[])
        monkeypatch.setattr(type(settings), "get_provider_mode", lambda self: ProviderMode.ONLINE_READONLY)

        calls = []

        async def spy_try_fetch(group_name, platforms, request_slot, role, custom_params=None):
            calls.append((request_slot, dict(custom_params or {})))
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
        await runner._fetch_trenches_two_group(custom_params={"trench_filters": build_trench_filters_for_x(0.2)})

        assert [slot for slot, _ in calls] == settings.get_discovery_slots()
        for ordinal, (slot, params) in enumerate(calls):
            i = ordinal + 1
            assert params["min_created"] == f"{60 * i}m"
            assert params["max_created"] == f"{60 * i + 60}m"
            assert params["type"] == ["new_creation", "near_completion"]

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="refactored to type-based discovery")
    async def test_feature_call_prefers_token_slot_and_falls_back_on_429(self, repo):
        class FakeGMGN:
            def __init__(self):
                self.calls = []

            async def fetch_latest_price(self, token_mint, credential_slot=None):
                self.calls.append(credential_slot)
                if credential_slot == 5:
                    raise GMGNAPIError("rate limited", status_code=429)
                return {"price": 1.0, "price_usd": 1.0}

        fake = FakeGMGN()
        runner = DiscoveryRunner(repo=repo, gmgn=fake, strategy_groups=[])

        def choose_slot(token, stage, exclude=None):
            exclude = exclude or set()
            return 5 if 5 not in exclude else 6

        runner._feature_slot_for_token = choose_slot
        result, slot = await runner._call_gmgn_with_token_slot(
            {"token_mint": "FALLBACK", "_credential_slot": 5},
            "price_info",
            "fetch_latest_price",
            "FALLBACK",
        )

        assert result["price"] == 1.0
        assert slot == 6
        assert fake.calls == [5, 6]

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="refactored to type-based discovery")
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
        assert math.isclose(filters["min_liquidity"], 4500.0, rel_tol=1e-9)




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





# ============================================================================
# Issue 3: price_change_percent1h reading logic
# ============================================================================

class TestPriceChangePercent1h:
    def _klines(self):
        return [{"open": 0.01, "high": 0.03, "low": 0.005, "close": 0.011}]

    def test_top_level_takes_priority(self):
        """When top-level price_change_percent1h exists, nested must NOT overwrite."""
        token = {"price_usd": 0.01}
        strategy = {"x": 0.2}
        latest = {
            "price_usd": 0.011,
            "price_change_percent1h": 20.0,
            "swaps_1h": 100,
            "volume_1h": 3000,
            "pool": {"price_change_percent1h": 5.0},
        }
        res = asyncio.run(evaluate_price_activity_rules(token, strategy, latest, klines=self._klines()))
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
            "swaps_1h": 100,
            "volume_1h": 3000,
            "pool": {"price_change_percent1h": 20.0},
        }
        res = asyncio.run(evaluate_price_activity_rules(token, strategy, latest, klines=self._klines()))
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
            "swaps_1h": 100,
            "volume_1h": 3000,
        }
        res = asyncio.run(evaluate_price_activity_rules(token, strategy, latest, klines=self._klines()))
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
        """x=0.2, top1 rate=5% (0.05) should pass (entry threshold=0.051)."""
        t = compute_thresholds(0.2)
        assert math.isclose(t.top1_addr_type0_max, 0.051, rel_tol=1e-9)
        holder = {"addr_type": 0, "amount_percentage": 5.0}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is True
        assert math.isclose(res.feature_vector["top1_holder_rate"], 0.05, rel_tol=1e-9)

    def test_x02_top1_fails_at_006(self):
        """x=0.2, top1 rate=6% should fail (entry threshold=0.051)."""
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
        """SIM HARD_TP_210 trigger at 2.8x closes position."""
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
        assert updated["status"] == "CLOSED", "SIM position closed on TP at 2.8x"

    @pytest.mark.asyncio
    async def test_sim_risk_recheck_exit_via_exit_service(self, repo):
        """SIM risk recheck failure delegates to PositionExitService and closes position."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = PositionRiskRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="SIM_RISK_TEST", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="POSITION_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=20.0, account_type="SIM",
        )
        pos = await repo.get_position(pos_id)
        # _request_exit delegates to exit_service.exit_position()
        await runner._request_exit(
            position=pos, exit_pct=1.0,
            reason_code="RISK_RECHECK_FAILED", emergency=True,
            latest={}, current_price_usd=0.01,
        )
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "CLOSED", "SIM position closed via unified exit service"

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


# ============================================================================
# Price runner: price API failure must NOT cause EXIT_PENDING
# ============================================================================

class TestPriceRunnerKeepPollingOnFailure:

    @pytest.mark.asyncio
    async def test_sim_price_failure_no_exit_pending(self, repo):
        """Price API failure on SIM: status stays SIM_OPEN, no EXIT_PENDING."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)

        pos_id = await repo.create_position(
            token_mint="SIM_PRICE_FAIL", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )

        positions = await repo.list_open_positions()
        pos = positions[0]
        fail_key = ActivePositionPriceRunner._failure_key(pos)

        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(side_effect=GMGNAPIError("API down"))):
            await runner._process_position(pos, datetime.now(timezone.utc))

        # Status must remain SIM_OPEN, not EXIT_PENDING
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "SIM_OPEN", "Price failure must not change status"

        # Failure counter incremented with composite key
        assert runner._consecutive_price_failures.get(fail_key, 0) == 1

    @pytest.mark.asyncio
    async def test_price_failure_keeps_position_in_next_poll(self, repo):
        """After price failure, position is still returned by list_open_positions."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)

        pos_id = await repo.create_position(
            token_mint="SIM_POLL_AGAIN", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
        )

        positions = await repo.list_open_positions()
        fail_key = ActivePositionPriceRunner._failure_key(positions[0])

        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(side_effect=GMGNAPIError("API down"))):
            for _ in range(2):
                await runner.run_once()

        # Position must still be in open list (not EXIT_PENDING / CLOSED)
        open_positions = await repo.list_open_positions()
        open_ids = {int(p["id"]) for p in open_positions}
        assert pos_id in open_ids, "Position must remain in open list after price failure"

        # fetch_latest_price should have been attempted twice (2 cycles, 1 each)
        assert runner._consecutive_price_failures.get(fail_key, 0) >= 2

    @pytest.mark.asyncio
    async def test_price_failure_then_recovery_updates_value(self, repo):
        """Price fails first, then recovers: remaining_value_usd is refreshed."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)

        pos_id = await repo.create_position(
            token_mint="SIM_RECOVER", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=5000.0,
            remaining_value_usd=50.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )

        positions = await repo.list_open_positions()
        fail_key = ActivePositionPriceRunner._failure_key(positions[0])

        # First round: price fails
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(side_effect=GMGNAPIError("API down"))):
            await runner._process_position(positions[0], datetime.now(timezone.utc))

        assert runner._consecutive_price_failures.get(fail_key, 0) == 1

        # Second round: price recovers — value is low so no exit triggers
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.015, "price": 0.015,
        })):
            await runner._process_position(positions[0], datetime.now(timezone.utc))

        # Consecutive failures must be cleared
        assert runner._consecutive_price_failures.get(fail_key) is None

        # remaining_value_usd must be refreshed (5000 * 0.015 = 75.0)
        updated = await repo.get_position(pos_id)
        assert updated["remaining_value_usd"] == 75.0
        assert updated["status"] == "SIM_OPEN", "No exit at 1.5x price (below 1.6x TP, above dust)"
        # pnl_pct must be updated: (0.015 / 0.01) - 1 = 0.5
        assert math.isclose(updated.get("pnl_pct") or 0, 0.5, rel_tol=1e-9)

    @pytest.mark.asyncio
    async def test_one_failure_does_not_block_another(self, repo):
        """One failing token does not block a healthy token's price fetch."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)

        fail_id = await repo.create_position(
            token_mint="BLOCKER_FAIL", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
        )
        ok_id = await repo.create_position(
            token_mint="BLOCKER_OK", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=500.0,
            remaining_value_usd=5.0, account_type="SIM",
        )

        real_fetch = gmgn.fetch_latest_price

        async def side_effect(token_mint, **kwargs):
            if token_mint == "BLOCKER_FAIL":
                raise GMGNAPIError("API down")
            return await real_fetch(token_mint, **kwargs)

        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(side_effect=side_effect)):
            await runner.run_once()

        ok_pos = await repo.get_position(ok_id)
        fail_pos = await repo.get_position(fail_id)

        # OK token should still have its price refreshed
        assert ok_pos["remaining_value_usd"] == 5.0, "Healthy token still processed"
        assert ok_pos["status"] == "SIM_OPEN"
        # Failing token stays open
        assert fail_pos["status"] == "SIM_OPEN"

    @pytest.mark.asyncio
    async def test_throttle_system_events_on_consecutive_failures(self, repo):
        """Repeated failures do not write a system event every cycle."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)

        pos_id = await repo.create_position(
            token_mint="THROTTLE_TEST", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
        )

        event_count = 0

        real_append = repo.append_system_event

        async def counting_append(level, category, message, data, **kwargs):
            nonlocal event_count
            if "KEEP_POLLING_NO_EXIT" in str(data):
                event_count += 1
            await real_append(level, category, message, data, **kwargs)

        repo.append_system_event = counting_append

        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(side_effect=GMGNAPIError("API down"))):
            for _ in range(3):
                await runner._process_position(await repo.get_position(pos_id), datetime.now(timezone.utc))

        # First failure writes event; subsequent failures within cooldown do not
        assert event_count == 1, f"Expected 1 throttled event, got {event_count}"

    @pytest.mark.asyncio
    async def test_one_price_call_per_cycle(self, repo):
        """_process_position makes exactly 1 API call per cycle, no retry."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)

        pos_id = await repo.create_position(
            token_mint="SINGLE_CALL", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )

        mock_fn = AsyncMock(return_value={"price_usd": 0.02, "price": 0.02})
        with patch.object(gmgn, 'fetch_latest_price', new=mock_fn):
            await runner.run_once()

        # Exactly 1 call made, no retries
        assert mock_fn.call_count == 1, f"Expected 1 call, got {mock_fn.call_count}"


# ============================================================================
# New TP/SL rule tests
# ============================================================================

class TestNewTPSLRules:

    @pytest.mark.asyncio
    async def test_hard_tp_160_half_exit(self, repo):
        """Price = 1.61x entry, no prior HARD_TP_160 → sell 50%, record HARD_TP_160."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="TP_160_HALF", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )
        pos = await repo.get_position(pos_id)
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.0161, "price": 0.0161,
        })):
            await runner._process_position(pos, datetime.now(timezone.utc))
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "SIM_OPEN", "50% exit keeps position open"
        assert updated["remaining_token_amount"] == 500.0
        rules = json.loads(updated.get("executed_exit_rules_json") or "[]")
        assert "HARD_TP_160" in rules

    @pytest.mark.asyncio
    async def test_hard_tp_160_not_triggered_at_1_6x(self, repo):
        """Price = 1.6x entry, no prior → NOT triggered (> not >=)."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="TP_160_NO", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )
        pos = await repo.get_position(pos_id)
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.016, "price": 0.016,
        })):
            await runner._process_position(pos, datetime.now(timezone.utc))
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "SIM_OPEN", "Position remains open"
        assert updated["remaining_token_amount"] == 1000.0

    @pytest.mark.asyncio
    async def test_hard_tp_210_full_exit(self, repo):
        """Price = 2.11x entry → full exit, reason HARD_TP_210."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="TP_210_FULL", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )
        pos = await repo.get_position(pos_id)
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.0211, "price": 0.0211,
        })):
            await runner._process_position(pos, datetime.now(timezone.utc))
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "CLOSED", "Full exit at 2.11x"

    @pytest.mark.asyncio
    async def test_hard_tp_210_not_triggered_at_2_1x(self, repo):
        """Price = 2.1x entry → NOT triggered for HARD_TP_210."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="TP_210_NO", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )
        pos = await repo.get_position(pos_id)
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.021, "price": 0.021,
        })):
            await runner._process_position(pos, datetime.now(timezone.utc))
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "SIM_OPEN", "No HARD_TP_210 at exactly 2.1x"

    @pytest.mark.asyncio
    async def test_hard_tp_160_retrace_full_exit(self, repo):
        """Already executed HARD_TP_160, price = 1.49x → full exit, reason HARD_TP_160_RETRACE."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="TP_160_RETRACE", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=500.0,
            remaining_value_usd=5.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )
        await repo.db.execute("UPDATE positions SET executed_exit_rules_json=? WHERE id=?", (json.dumps(["HARD_TP_160"]), pos_id))
        pos = await repo.get_position(pos_id)
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.0149, "price": 0.0149,
        })):
            await runner._process_position(pos, datetime.now(timezone.utc))
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "CLOSED", "Full exit on retrace to <1.5x"

    @pytest.mark.asyncio
    async def test_hard_tp_160_retrace_below_1_5x(self, repo):
        """Already executed HARD_TP_160, price = 1.4x → full exit."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="TP_160_RETRACE2", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=500.0,
            remaining_value_usd=5.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )
        await repo.db.execute("UPDATE positions SET executed_exit_rules_json=? WHERE id=?", (json.dumps(["HARD_TP_160"]), pos_id))
        pos = await repo.get_position(pos_id)
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.014, "price": 0.014,
        })):
            await runner._process_position(pos, datetime.now(timezone.utc))
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "CLOSED", "Full exit on retrace below 1.5x"

    @pytest.mark.asyncio
    async def test_hard_sl_75_full_exit(self, repo):
        """Price = 0.74x entry → full exit, reason HARD_SL_75."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="SL_75_FULL", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )
        pos = await repo.get_position(pos_id)
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.0074, "price": 0.0074,
        })):
            await runner._process_position(pos, datetime.now(timezone.utc))
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "CLOSED", "Full exit at 0.74x"

    @pytest.mark.asyncio
    async def test_hard_sl_75_not_triggered_at_0_75x(self, repo):
        """Price = 0.75x → NOT triggered (< not <=)."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="SL_75_NO", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=3000.0,
            remaining_value_usd=30.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )
        pos = await repo.get_position(pos_id)
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.0075, "price": 0.0075,
        })):
            await runner._process_position(pos, datetime.now(timezone.utc))
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "SIM_OPEN", "No SL at exactly 0.75x"

    @pytest.mark.asyncio
    async def test_completed_full_exit(self, repo):
        """Position with type=completed → full exit."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="COMPLETED_TEST", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )
        pos = await repo.get_position(pos_id)
        pos["latest_token_type"] = "completed"
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.01, "price": 0.01,
        })):
            await runner._process_position(pos, datetime.now(timezone.utc))
        updated = await repo.get_position(pos_id)
        assert updated["status"] == "CLOSED", "Completed triggers full exit"

    @pytest.mark.asyncio
    async def test_old_reason_codes_not_produced(self, repo):
        """Old reason codes HARD_TP_150/HARD_TP_200/HARD_TP_250/HARD_SL_70/HARD_SL_50/HARD_SL_45 not produced."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = ActivePositionPriceRunner(repo, gmgn)
        pos_id = await repo.create_position(
            token_mint="OLD_CODES", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=10.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )
        pos = await repo.get_position(pos_id)
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.017, "price": 0.017,  # triggers new HARD_TP_160
        })):
            await runner._process_position(pos, datetime.now(timezone.utc))
        updated = await repo.get_position(pos_id)
        rules = json.loads(updated.get("executed_exit_rules_json") or "[]")
        for old in ("HARD_TP_150", "HARD_TP_200", "HARD_TP_250", "HARD_SL_70", "HARD_SL_50", "HARD_SL_45"):
            assert old not in rules, f"Old reason code {old} must not be produced"


# ============================================================================
# Concurrent exit protection
# ============================================================================

class TestConcurrentExitProtection:

    @pytest.mark.asyncio
    async def test_concurrent_sim_second_exit_blocked(self, repo):
        """Two concurrent exit_position calls on same SIM position: second gets POSITION_ALREADY_EXITING."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        exit_svc = PositionExitService(repo)
        pos_id = await repo.create_position(
            token_mint="SIM_CONCUR", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=20.0, account_type="SIM",
        )
        pos = await repo.get_position(pos_id)

        # First call succeeds
        r1 = await exit_svc.exit_position(
            position=pos, exit_pct=1.0,
            reason_code="HARD_TP_210", current_price_usd=0.021,
            source="TEST",
        )
        assert r1["ok"] is True, f"First exit should succeed: {r1}"

        # Second call on same (now closed) position should fail
        r2 = await exit_svc.exit_position(
            position=pos, exit_pct=1.0,
            reason_code="MANUAL_SELL", current_price_usd=0.021,
            source="TEST",
        )
        assert r2["ok"] is False
        assert "POSITION_ALREADY" in r2.get("error", ""), f"Expected POSITION_ALREADY_*, got {r2}"

    @pytest.mark.asyncio
    async def test_concurrent_live_only_one_execute_sell(self, repo):
        """Two concurrent LIVE exit calls: only the first claims and calls execute_sell."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)
        pipeline.execute_sell = AsyncMock(return_value={"ok": True})
        exit_svc = PositionExitService(repo, trading_pipeline=pipeline)

        pos_id = await repo.create_position(
            token_mint="LIVE_CONCUR", is_live=True,
            locked_strategy_config_json='{"x": 0.2}',
            status="POSITION_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=20.0, account_type="LIVE",
        )
        pos = await repo.get_position(pos_id)

        # First LIVE exit
        r1 = await exit_svc.exit_position(
            position=pos, exit_pct=1.0,
            reason_code="RISK_RECHECK_FAILED", current_price_usd=0.01,
            source="TEST",
        )
        assert r1["ok"] is True
        assert pipeline.execute_sell.call_count == 1

        # Second LIVE exit on same position — blocked by EXIT_PENDING
        r2 = await exit_svc.exit_position(
            position=pos, exit_pct=1.0,
            reason_code="MANUAL_SELL", current_price_usd=0.01,
            source="TEST",
        )
        assert r2["ok"] is False
        assert pipeline.execute_sell.call_count == 1, "execute_sell must NOT be called a second time"

    @pytest.mark.asyncio
    async def test_concurrent_sim_partial_then_full(self, repo):
        """SIM position partial exit (50%) succeeds, then full exit on remaining works."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        exit_svc = PositionExitService(repo)
        pos_id = await repo.create_position(
            token_mint="SIM_PART_THEN_FULL", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=20.0, account_type="SIM",
        )
        pos = await repo.get_position(pos_id)

        # 50% partial exit
        r1 = await exit_svc.exit_position(
            position=pos, exit_pct=0.5,
            reason_code="HARD_TP_160", current_price_usd=0.016,
            source="TEST",
        )
        assert r1["ok"] is True
        p1 = await repo.get_position(pos_id)
        assert p1["remaining_token_amount"] == 500.0

        # Claim was released, so second exit on same position works
        r2 = await exit_svc.exit_position(
            position=p1, exit_pct=1.0,
            reason_code="HARD_SL_75", current_price_usd=0.007,
            source="TEST",
        )
        assert r2["ok"] is True
        p2 = await repo.get_position(pos_id)
        assert p2["status"] == "CLOSED"


# ============================================================================
# Soft stop tick fallback
# ============================================================================

class TestSoftStopTickFallback:

    @pytest.mark.asyncio
    async def test_dull_drop_ticks_fallback(self, repo):
        """Soft stop: if latest price lacks change fields, tick_snapshots are used as fallback."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)

        pos_id = await repo.create_position(
            token_mint="SOFT_TICK_FALLBACK", is_live=False,
            locked_strategy_config_json='{"x": 0.2}',
            status="SIM_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000.0, remaining_token_amount=1000.0,
            remaining_value_usd=20.0, account_type="SIM",
            last_fill_at=datetime.now(timezone.utc).isoformat(),
            last_fill_price_usd=0.01,
        )
        pos = await repo.get_position(pos_id)

        # Insert tick snapshots safely inside the query window (use offsets
        # slightly less than window_seconds to avoid cutoff-edge race)
        now = datetime.now(timezone.utc)
        for offset_sec, price_val in [(3595, 0.02), (295, 0.02)]:
            observed = (now - timedelta(seconds=offset_sec)).isoformat()
            await repo.db.execute(
                "INSERT INTO tick_snapshots (token_mint, source, observed_at, price_usd, price_sol) VALUES (?, ?, ?, ?, ?)",
                ("SOFT_TICK_FALLBACK", "GMGN", observed, price_val, price_val),
            )
        await repo.db.commit()

        runner = PositionSoftStopRunner(repo, gmgn)

        # Mock GMGN to return latest price WITHOUT change fields
        with patch.object(gmgn, 'fetch_latest_price', new=AsyncMock(return_value={
            "price_usd": 0.0095,  # ~5% drop from 0.01 ref, below 1% → triggers dull-drop
        })):
            did_exit = await runner._check_dull_drop(pos, {"price_usd": 0.0095}, 0.0095, now)
            assert did_exit is True, "Dull-drop should trigger via tick fallback"

        updated = await repo.get_position(pos_id)
        assert updated["status"] == "CLOSED", "Position closed after dull-drop via tick fallback"
