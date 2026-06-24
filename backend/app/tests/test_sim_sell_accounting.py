"""Tests for SIM sell accounting alignment between TradingPipeline and PositionExitService.

Verifies:
- Quote success path accounting matches old pipeline formula
- Quote failure / fallback paths
- Raw-rounds-to-zero fallback
- Full vs partial exit
- EXIT audit uses real trade_event (not mini dict)
- LIVE audit_context passthrough
"""

from __future__ import annotations

import json
import math
from unittest.mock import AsyncMock, MagicMock

import pytest
from ..config import settings, ProviderMode
from ..db.repositories import Repositories
from ..trading.executor import TradingPipeline, WRAPPED_SOL_MINT, LAMPORTS_PER_SOL
from ..trading.sim_sell_accounting import prepare_sim_sell_accounting
from ..services.position_exit_service import PositionExitService
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcRealProvider


def _set_fee_upper(fee: float):
    settings.SIM_SELL_FEE_UPPER_BOUND_USD = fee


def _reset_fee_upper():
    settings.SIM_SELL_FEE_UPPER_BOUND_USD = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sim_position(repo, **overrides):
    """Create a SIM position via repo with defaults."""
    defaults = {
        "token_mint": "MOCK_TOKEN_MINT",
        "is_live": False,
        "locked_strategy_config_json": '{"id": 1, "token_decimals": 6, "sell_slippage_cap_bps": 100}',
        "status": "POSITION_OPEN",
        "entry_price_usd": 0.01,
        "entry_token_amount": 10000,
        "remaining_token_amount": 10000,
        "remaining_value_usd": 100.0,
        "account_type": "SIM",
    }
    defaults.update(overrides)
    return repo.create_position(**defaults)


# ---------------------------------------------------------------------------
# A. prepare_sim_sell_accounting unit tests (mock objects)
# ---------------------------------------------------------------------------

class TestPrepareSimSellAccounting:

    @pytest.mark.asyncio
    async def test_quote_success_path_accounting_formula(self):
        """Quote success: expected_usd, conservative_net, sell_tax_est_usd match old pipeline formula."""
        gmgn = AsyncMock()
        gmgn.fetch_latest_price = AsyncMock(return_value={
            "price_usd": 0.01, "price_sol": 1e-9, "token_decimals": 6,
        })

        jupiter = AsyncMock()
        jupiter.quote_exact_in = AsyncMock(return_value={
            "outAmount": str(int(1.0 * LAMPORTS_PER_SOL)),
            "otherAmountThreshold": str(int(0.95 * LAMPORTS_PER_SOL)),
            "outputMint": WRAPPED_SOL_MINT,
            "priceImpactPct": "0.02",
            "routePlan": [{"x": 1}],
            "platformFee": {"amount": "123"},
        })

        repo = MagicMock()
        repo.get_position_audits = AsyncMock(return_value=[])
        repo.get_latest_token_metric_snapshot = AsyncMock(return_value={"sell_tax": 0.01})

        position = {
            "id": 1,
            "token_mint": "TOKEN",
            "remaining_token_amount": 1000,
            "locked_strategy_config_json": '{"token_decimals": 6, "sell_slippage_cap_bps": 100}',
            "last_fill_price_usd": 0.01,
            "entry_price_usd": 0.01,
            "discovery_event_id": 1,
        }

        _set_fee_upper(0.5)
        try:
            ctx = await prepare_sim_sell_accounting(
                repo=repo,
                gmgn=gmgn,
                jupiter=jupiter,
                position=position,
                exit_pct=0.5,
                reason_code="HARD_TP_160",
            )
        finally:
            _reset_fee_upper()

        assert ctx["pct"] == 0.5
        assert ctx["quote_ok"] is True
        assert ctx["sell_amount_human"] == 500.0  # 1000 * 0.5
        assert ctx["current_price_usd"] == 0.01

        acct = ctx["acct"]
        assert acct["accounting_source"] == "jupiter_quote_conservative"
        assert acct["accounting_status"] == "ESTIMATED"

        # expected_usd = 1 SOL * sol_usd
        sol_usd = ctx["current_price_usd"] / (ctx["current_price_sol"] or 1e-9) if ctx["current_price_sol"] > 0 else 200.0
        expected_usd = acct["trade_value_usd_expected"]
        assert expected_usd > 0, f"expected_usd should be positive, got {expected_usd}"

        # conservative_net <= expected_usd
        assert acct["trade_value_usd_conservative"] <= expected_usd
        assert acct["trade_value_usd_net"] == acct["trade_value_usd_conservative"]

        # fee_detail
        fd = acct["fee_detail"]
        assert fd["accounting_mode"] == "SIM_CONSERVATIVE"
        assert fd["sell_tax_ratio"] == 0.01
        assert fd["sell_tax_est_usd"] > 0

        # quote fields
        assert ctx["price_impact"] > 0
        assert ctx["price_impact_pct"] == ctx["price_impact"] * 100.0
        assert ctx["quote_json"] is not None
        assert ctx["route_plan_json"] is not None

    @pytest.mark.asyncio
    async def test_quote_success_specific_values(self):
        """Verify exact accounting formula with controlled inputs."""
        gmgn = AsyncMock()
        gmgn.fetch_latest_price = AsyncMock(return_value={
            "price_usd": 0.01, "price_sol": 0.00005,
        })

        jupiter = AsyncMock()
        # outAmount = 1_000_000_000 → 1 SOL → at sol_usd=200 → $200
        # otherAmountThreshold = 950_000_000 → 0.95 SOL → $190
        jupiter.quote_exact_in = AsyncMock(return_value={
            "outAmount": "1000000000",
            "otherAmountThreshold": "950000000",
            "outputMint": WRAPPED_SOL_MINT,
            "priceImpactPct": "0.02",
            "routePlan": [{"x": 1}],
            "platformFee": {"amount": "123"},
        })

        repo = MagicMock()
        repo.get_position_audits = AsyncMock(return_value=[])
        repo.get_latest_token_metric_snapshot = AsyncMock(return_value={"sell_tax": 0.01})

        position = {
            "id": 1,
            "token_mint": "TOKEN",
            "remaining_token_amount": 1000,
            "locked_strategy_config_json": '{"token_decimals": 6, "sell_slippage_cap_bps": 100}',
            "last_fill_price_usd": 0.01,
            "entry_price_usd": 0.01,
            "entry_price_sol": 0.00005,
            "discovery_event_id": 1,
        }

        _set_fee_upper(0.5)
        try:
            ctx = await prepare_sim_sell_accounting(
                repo=repo,
                gmgn=gmgn,
                jupiter=jupiter,
                position=position,
                exit_pct=0.5,
            )
        finally:
            _reset_fee_upper()

        acct = ctx["acct"]
        # outAmount = 1000000000 → 1 SOL, sol_usd ≈ 0.01/0.00000005 = 200
        # expected_usd = 1 * 200 = 200
        assert acct["trade_value_usd_expected"] == pytest.approx(200.0, rel=0.01)
        # otherAmountThreshold = 950000000 → 0.95 SOL → 190
        # sell_tax_est = 200 * 0.01 = 2
        # fee_upper = 0.5
        # conservative_net = max(0, 190 - 2 - 0.5) = 187.5
        assert acct["trade_value_usd_conservative"] == pytest.approx(187.5, rel=0.01)
        assert acct["trade_value_usd_net"] == pytest.approx(187.5, rel=0.01)
        assert acct["gross_value_usd"] == pytest.approx(200.0, rel=0.01)
        assert acct["fee_usd_est"] == pytest.approx(2.5, rel=0.01)  # 2 + 0.5

        # sell_price_effective = 187.5 / 500 = 0.375
        assert ctx["sell_price_effective"] == pytest.approx(0.375, rel=0.01)

        # price_impact_pct = 0.02 * 100 = 2.0
        assert ctx["price_impact_pct"] == pytest.approx(2.0, rel=0.01)

    @pytest.mark.asyncio
    async def test_quote_failure_fallback_path(self):
        """Quote returns error → GMGN price fallback formula."""
        gmgn = AsyncMock()
        gmgn.fetch_latest_price = AsyncMock(return_value={
            "price_usd": 0.01, "price_sol": 1e-9,
        })

        jupiter = AsyncMock()
        jupiter.quote_exact_in = AsyncMock(return_value={"error": "NO_QUOTE"})

        repo = MagicMock()
        position = {
            "id": 1,
            "token_mint": "TOKEN",
            "remaining_token_amount": 1000,
            "locked_strategy_config_json": '{"token_decimals": 6}',
            "last_fill_price_usd": 0.01,
            "entry_price_usd": 0.01,
            "discovery_event_id": 1,
        }

        _set_fee_upper(0.5)
        try:
            ctx = await prepare_sim_sell_accounting(
                repo=repo,
                gmgn=gmgn,
                jupiter=jupiter,
                position=position,
                exit_pct=0.5,
            )
        finally:
            _reset_fee_upper()

        assert ctx["quote_ok"] is False
        assert ctx["quote"].get("error") == "NO_QUOTE"
        assert ctx["quote_json"] is None
        assert ctx["route_plan_json"] is None
        assert ctx["price_impact_pct"] is None

        acct = ctx["acct"]
        # sell_amount = 500, price_usd = 0.01, gross = 5
        # fallback_net = abs(5 - 0.5) = 4.5
        assert acct["trade_value_usd_expected"] == pytest.approx(5.0, rel=0.01)
        assert acct["trade_value_usd_conservative"] == pytest.approx(4.5, rel=0.01)
        assert acct["trade_value_usd_net"] == pytest.approx(4.5, rel=0.01)
        assert acct["gross_value_usd"] == pytest.approx(5.0, rel=0.01)
        assert acct["fee_usd_est"] == 0.5
        assert acct["accounting_source"] == "gmgn_price_fallback"
        assert acct["accounting_status"] == "ESTIMATED"

        fd = acct["fee_detail"]
        assert fd["fallback"] is True
        assert fd["reason"] == "no_quote_or_sell_amount_raw_rounds_to_zero"

        # sell_price_effective = 4.5 / 500 = 0.009
        assert ctx["sell_price_effective"] == pytest.approx(0.009, rel=0.01)

    @pytest.mark.asyncio
    async def test_no_jupiter_provider_uses_fallback(self):
        """When jupiter is None, fallback path is used."""
        gmgn = AsyncMock()
        gmgn.fetch_latest_price = AsyncMock(return_value={
            "price_usd": 0.02, "price_sol": 1e-9,
        })

        repo = MagicMock()
        position = {
            "id": 1,
            "token_mint": "TOKEN",
            "remaining_token_amount": 1000,
            "locked_strategy_config_json": '{"token_decimals": 6}',
            "last_fill_price_usd": 0.02,
            "entry_price_usd": 0.02,
            "discovery_event_id": 1,
        }

        ctx = await prepare_sim_sell_accounting(
            repo=repo,
            gmgn=gmgn,
            jupiter=None,
            position=position,
            exit_pct=0.3,
        )

        assert ctx["quote_ok"] is False
        acct = ctx["acct"]
        assert acct["accounting_source"] == "gmgn_price_fallback"
        assert acct["accounting_status"] == "ESTIMATED"

    @pytest.mark.asyncio
    async def test_sell_amount_raw_rounds_to_zero_fallback(self):
        """Very small remaining tokens with 0 decimals → raw rounds to zero."""
        gmgn = AsyncMock()
        gmgn.fetch_latest_price = AsyncMock(return_value={
            "price_usd": 0.01, "price_sol": 1e-9,
        })

        jupiter = AsyncMock()

        repo = MagicMock()
        position = {
            "id": 1,
            "token_mint": "TOKEN",
            "remaining_token_amount": 0.000000001,  # extremely small
            "locked_strategy_config_json": '{"token_decimals": 0}',  # 0 decimals → raw = floor(0.000000001 * 1) = 0
            "last_fill_price_usd": 0.01,
            "entry_price_usd": 0.01,
            "discovery_event_id": 1,
        }

        ctx = await prepare_sim_sell_accounting(
            repo=repo,
            gmgn=gmgn,
            jupiter=jupiter,
            position=position,
            exit_pct=1.0,
        )

        assert ctx["quote_ok"] is False
        assert ctx["sell_amount_raw"] == 0
        acct = ctx["acct"]
        assert acct["fee_usd_est"] == 0.0
        assert acct["accounting_source"] == "gmgn_price_fallback"
        fd = acct["fee_detail"]
        assert fd["reason"] == "sell_amount_raw_rounds_to_zero"

    @pytest.mark.asyncio
    async def test_price_impact_cap_triggers_fallback(self):
        """Quote with high price impact above cap falls back to GMGN."""
        gmgn = AsyncMock()
        gmgn.fetch_latest_price = AsyncMock(return_value={
            "price_usd": 0.01, "price_sol": 1e-9,
        })

        jupiter = AsyncMock()
        jupiter.quote_exact_in = AsyncMock(return_value={
            "outAmount": "1000",
            "otherAmountThreshold": "950",
            "outputMint": WRAPPED_SOL_MINT,
            "priceImpactPct": "0.15",  # 15% → above 10% cap
            "routePlan": [],
        })

        repo = MagicMock()
        position = {
            "id": 1,
            "token_mint": "TOKEN",
            "remaining_token_amount": 1000,
            "locked_strategy_config_json": '{"token_decimals": 6}',
            "last_fill_price_usd": 0.01,
            "entry_price_usd": 0.01,
            "discovery_event_id": 1,
        }

        ctx = await prepare_sim_sell_accounting(
            repo=repo,
            gmgn=gmgn,
            jupiter=jupiter,
            position=position,
            exit_pct=0.5,
        )

        # Impact capped → falls back to GMGN
        assert ctx["quote_ok"] is False
        assert ctx["quote"].get("error") == "PRICE_IMPACT_HARD_CAP"
        assert ctx["acct"]["accounting_source"] == "gmgn_price_fallback"


# ---------------------------------------------------------------------------
# B. PositionExitService + TradingPipeline integration tests (real repo)
# ---------------------------------------------------------------------------

class TestSimSellAccountingIntegration:

    @pytest.mark.asyncio
    async def test_service_and_pipeline_produce_same_accounting(self, repo):
        """PositionExitService and TradingPipeline produce identical trade_event accounting fields."""
        pos_id = await _make_sim_position(repo)

        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)
        exit_svc = PositionExitService(repo, trading_pipeline=pipeline, gmgn=gmgn)

        # Pipeline sell
        pos1 = await repo.get_position(pos_id)
        r1 = await pipeline._execute_sim_paper_sell(pos1, 0.5, "HARD_TP_160")
        assert r1["ok"] is True

        # Read the trade event
        te1 = (await repo.list_trade_events())[-1]

        # Service sell on fresh position (same setup)
        pos2_id = await _make_sim_position(repo, token_mint="MOCK_TOKEN_2")
        pos2 = await repo.get_position(pos2_id)
        r2 = await exit_svc.exit_position(
            position=pos2, exit_pct=0.5,
            reason_code="HARD_TP_160",
            current_price_usd=0.01,
            source="TEST",
        )
        assert r2["ok"] is True

        te2 = (await repo.list_trade_events())[-1]

        # Compare accounting columns
        for field in [
            "gross_value_usd", "trade_value_usd_net",
            "trade_value_usd_expected", "trade_value_usd_conservative",
            "fee_usd_est", "accounting_source", "accounting_status",
            "exit_reason_label", "provider",
        ]:
            val1 = te1.get(field)
            val2 = te2.get(field)
            assert val1 == val2, (
                f"Field {field} mismatch: pipeline={val1}, service={val2}"
            )

        # provider must be PIPELINE_SIM (not runner source)
        assert te2["provider"] == "PIPELINE_SIM", f"Expected PIPELINE_SIM, got {te2.get('provider')}"

        # Both should have quote_json for mock success scenario
        assert te1.get("quote_json") is not None
        assert te2.get("quote_json") is not None

    @pytest.mark.asyncio
    async def test_service_sim_full_exit_closes_position(self, repo):
        """Full SIM exit via service closes the position with pipeline accounting."""
        pos_id = await _make_sim_position(repo)

        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)
        exit_svc = PositionExitService(repo, trading_pipeline=pipeline, gmgn=gmgn)

        pos = await repo.get_position(pos_id)
        r = await exit_svc.exit_position(
            position=pos, exit_pct=1.0,
            reason_code="HARD_TP_210",
            current_price_usd=0.021,
            source="TEST",
        )
        assert r["ok"] is True

        updated = await repo.get_position(pos_id)
        assert updated["status"] == "CLOSED"

        # mark_exit_rule_executed called
        te = await repo.list_trade_events()
        sell_events = [t for t in te if t["event_type"] == "SIM_SELL"]
        assert len(sell_events) == 1

    @pytest.mark.asyncio
    async def test_service_sim_audit_uses_real_trade_event_and_quote(self, repo):
        """PositionExitService EXIT audit passes real trade_event and real quote data."""
        pos_id = await _make_sim_position(repo)

        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)
        exit_svc = PositionExitService(repo, trading_pipeline=pipeline, gmgn=gmgn)

        pos = await repo.get_position(pos_id)
        r = await exit_svc.exit_position(
            position=pos, exit_pct=0.5,
            reason_code="RISK_RECHECK_FAILED",
            current_price_usd=0.01,
            source="TEST",
            audit_context={"risk_failed_rules": ["liquidity", "holders"]},
        )
        assert r["ok"] is True

        audits = await repo.get_position_audits(pos_id, audit_type="EXIT")
        assert len(audits) == 1
        audit_json = audits[0].get("audit_json")
        if isinstance(audit_json, str):
            audit_json = json.loads(audit_json)

        # audit must contain quote/route/price impact info
        assert audit_json.get("quote_json") is not None, "Audit missing quote_json"
        assert audit_json.get("route_plan_json") is not None, "Audit missing route_plan_json"
        assert audit_json.get("exit_reason_code") == "RISK_RECHECK_FAILED"
        assert audit_json.get("exit_pct") == 0.5

        # risk context passed through
        assert audit_json.get("risk_failed_rules") == ["liquidity", "holders"]

        # sell_trade_event should have real data (id present)
        assert audit_json.get("sell_time_utc") is not None

    @pytest.mark.asyncio
    async def test_service_fallback_audit_no_jupiter_available(self, repo):
        """When no trading_pipeline/jupiter, PositionExitService uses fallback accounting for audit."""
        pos_id = await _make_sim_position(repo)

        exit_svc = PositionExitService(repo)  # no pipeline, no jupiter

        pos = await repo.get_position(pos_id)
        r = await exit_svc.exit_position(
            position=pos, exit_pct=0.5,
            reason_code="MANUAL_SELL",
            current_price_usd=0.01,
            source="MANUAL_API",
        )
        assert r["ok"] is True

        trade_events = await repo.list_trade_events()
        sells = [t for t in trade_events if t["event_type"] == "SIM_SELL"]
        assert len(sells) >= 1
        te = sells[-1]
        assert te["accounting_source"] == "gmgn_price_fallback"
        assert te["accounting_status"] == "ESTIMATED"
        assert te["fee_usd_est"] == 0.0  # raw rounds to zero or no jupiter → fee=0

    @pytest.mark.asyncio
    async def test_service_full_exit_writes_quote_fallback_info(self, repo):
        """Full exit via service writes accounting info even with no jupiter."""
        pos_id = await _make_sim_position(repo)

        exit_svc = PositionExitService(repo)

        pos = await repo.get_position(pos_id)
        r = await exit_svc.exit_position(
            position=pos, exit_pct=1.0,
            reason_code="COMPLETED",
            current_price_usd=0.03,
            source="TEST",
        )
        assert r["ok"] is True

        updated = await repo.get_position(pos_id)
        assert updated["status"] == "CLOSED"

        # Verify system_event written
        sys_events = await repo.list_recent_system_events(limit=10)
        paper_sell_events = [e for e in sys_events if "paper" in str(e.get("message", "")).lower()]
        assert len(paper_sell_events) >= 1


# ---------------------------------------------------------------------------
# C. LIVE audit_context passthrough
# ---------------------------------------------------------------------------

class TestLiveAuditContextPassthrough:

    @pytest.mark.asyncio
    async def test_live_exit_passes_audit_context_to_execute_sell(self, repo):
        """LIVE exit forwards audit_context through PositionExitService → TradingPipeline.execute_sell."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)

        capture = {}
        original_execute_sell = pipeline.execute_sell

        async def capturing_execute_sell(position, exit_pct=1.0, exit_reason="EXIT", audit_context=None):
            capture["position"] = position
            capture["exit_pct"] = exit_pct
            capture["exit_reason"] = exit_reason
            capture["audit_context"] = audit_context
            return {"ok": True}

        pipeline.execute_sell = capturing_execute_sell

        exit_svc = PositionExitService(repo, trading_pipeline=pipeline, gmgn=gmgn)

        pos_id = await repo.create_position(
            token_mint="LIVE_TOKEN", is_live=True,
            locked_strategy_config_json='{"x": 0.2}',
            status="POSITION_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000, remaining_token_amount=1000,
            remaining_value_usd=20.0, account_type="LIVE",
        )
        pos = await repo.get_position(pos_id)

        audit_ctx = {"risk_failed_rules": ["liquidity", "holders"], "risk_score": 123}
        r = await exit_svc.exit_position(
            position=pos, exit_pct=1.0,
            reason_code="RISK_RECHECK_FAILED",
            current_price_usd=0.01,
            source="RISK_RUNNER",
            audit_context=audit_ctx,
        )
        assert r["ok"] is True
        assert capture.get("audit_context") == audit_ctx

    @pytest.mark.asyncio
    async def test_live_sell_failure_releases_claim(self, repo):
        """LIVE execute_sell failure releases EXIT_PENDING claim (position stays open)."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)
        pipeline.execute_sell = AsyncMock(return_value={"ok": False, "error": "SLIPPAGE_TOO_HIGH"})

        exit_svc = PositionExitService(repo, trading_pipeline=pipeline, gmgn=gmgn)

        pos_id = await repo.create_position(
            token_mint="LIVE_FAIL", is_live=True,
            locked_strategy_config_json='{"x": 0.2}',
            status="POSITION_OPEN", entry_price_usd=0.01,
            entry_token_amount=1000, remaining_token_amount=1000,
            remaining_value_usd=20.0, account_type="LIVE",
        )
        pos = await repo.get_position(pos_id)

        r = await exit_svc.exit_position(
            position=pos, exit_pct=1.0,
            reason_code="RISK_RECHECK_FAILED",
            current_price_usd=0.01,
            source="TEST",
        )
        assert r["ok"] is False

        # Position must still be OPEN (not EXIT_PENDING, not CLOSED)
        updated = await repo.get_position(pos_id)
        assert updated["status"] in ("POSITION_OPEN", "OPEN"), f"Expected OPEN, got {updated['status']}"

    @pytest.mark.asyncio
    async def test_sim_partial_exit_then_second_exit(self, repo):
        """SIM partial exit (50%) succeeds, claim released, then second exit works."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)
        exit_svc = PositionExitService(repo, trading_pipeline=pipeline, gmgn=gmgn)

        pos_id = await _make_sim_position(repo, remaining_token_amount=1000, remaining_value_usd=10.0)

        pos = await repo.get_position(pos_id)
        r1 = await exit_svc.exit_position(
            position=pos, exit_pct=0.5,
            reason_code="HARD_TP_160", current_price_usd=0.016, source="TEST",
        )
        assert r1["ok"] is True
        p1 = await repo.get_position(pos_id)
        assert float(p1["remaining_token_amount"]) == pytest.approx(500.0, rel=0.01)

        r2 = await exit_svc.exit_position(
            position=p1, exit_pct=1.0,
            reason_code="HARD_SL_75", current_price_usd=0.007, source="TEST",
        )
        assert r2["ok"] is True
        p2 = await repo.get_position(pos_id)
        assert p2["status"] == "CLOSED"

        # Verify two separate trade events (different idempotency keys)
        sells = [t for t in await repo.list_trade_events() if t["event_type"] == "SIM_SELL"]
        assert len(sells) == 2
