import pytest
import json
from datetime import datetime, timezone

from ..trading.audit_builder import (
    ENTRY_AUDIT_REQUIRED_FIELDS,
    EXIT_AUDIT_REQUIRED_FIELDS,
    build_entry_audit_payload,
    build_exit_audit_payload,
)
from ..trading.executor import WRAPPED_SOL_MINT


@pytest.mark.asyncio
async def test_entry_audit_required_keys_sim_buy(pipeline_factory):
    pipeline, _ = pipeline_factory()
    repo = pipeline.repo
    md = pipeline.gmgn.mock_data
    tok = "ENTRY_TEST"
    md.tokens[tok] = {
        "token_mint": tok, "symbol": "ET", "name": "Entry Test",
        "pool_address": "pool1", "pool_created_at": "2025-01-01T00:00:00Z",
        "type": "new_creation", "launchpad": "pump", "platform": "pump",
        "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0,
        "volume_usd": 10000.0, "volume_1h": 500.0,
        "market_cap": 500000.0, "price_usd": 0.001, "price_sol": 0.00001,
        "top_10_holder_rate": 0.3, "top1_holder_rate": 0.1,
        "max_rug_ratio": 0.05, "max_insider_ratio": 0.02,
        "max_entrapment_ratio": 0.03, "is_wash_trading": 0,
        "rat_trader_amount_rate": 0.01, "suspected_insider_hold_rate": 0.02,
        "max_bundler_rate": 0.01, "fresh_wallet_rate": 0.1,
        "sell_tax": 0.0, "burn_status": "burned", "sniper_count": 2,
        "has_social": 1, "holder_count": 150,
        "dev_team_hold_rate": 0.05, "creator_balance_rate": 0.05,
        "smart_degen_count": 5,
        "swaps_1h": 100, "price_change_percent1h": 5.0,
        "twitter": "elonmusk",
    }
    md.latest[tok] = {
        "price_usd": 0.001, "price_sol": 0.00001,
        "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0,
    }

    pos_id = await repo.create_position(
        token_mint=tok,
        is_live=False,
        locked_strategy_config_json='{"id": 1}',
        status="POSITION_OPEN",
        entry_price_usd=0.001,
        entry_token_amount=100.0,
        remaining_token_amount=100.0,
        remaining_value_usd=0.1,
        account_type="SIM",
    )

    te = await repo.append_trade_event(
        f"SIM_BUY_TEST:{tok}",
        token_mint=tok, side="BUY", event_type="SIM_BUY",
        status="CONFIRMED", is_live=0, account_type="SIM",
        executed_token_amount=100.0, price_usd=0.001,
        trade_value_usd_net=-100.0,
    )

    entry_audit = await build_entry_audit_payload(
        repo=repo, gmgn=pipeline.gmgn,
        token_mint=tok, position_id=pos_id,
        account_type="SIM", strategy={"id": 1},
        discovery_event_id=None, snapshot_id=None,
        buy_trade_event=te, quote={},
        token_amount=100.0, price_usd=0.001, price_sol=0.00001,
        size_usd=100.0,
    )

    for field in ENTRY_AUDIT_REQUIRED_FIELDS:
        assert field in entry_audit, f"Missing ENTRY field: {field}"
    assert isinstance(entry_audit.get("socials"), list)
    assert entry_audit.get("token_mint") == tok
    assert entry_audit.get("symbol") == "ET"
    assert entry_audit.get("buy_price_usd") == 0.001
    assert entry_audit.get("entry_missing_fields") is not None


@pytest.mark.asyncio
async def test_top1_addr_type0_holder_selection(pipeline_factory):
    pipeline, mock = pipeline_factory()
    repo = pipeline.repo
    tok = "TOP1_TEST"
    mock.tokens[tok] = {
        "token_mint": tok, "symbol": "T1", "name": "Top1 Test",
        "pool_address": "pool1", "type": "new_creation",
        "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0,
        "price_usd": 0.001, "price_sol": 0.00001,
        "holder_count": 100,
    }
    mock.latest[tok] = {"price_usd": 0.001, "price_sol": 0.00001, "liquidity_usd": 50000.0}

    # Make _normalize_token_data not strip type/launchpad
    # Mock fetch_top_holders via GMGNProvider mock — the mock returns fixed data.
    # The mock's fetch_top_holders returns: [{"addr_type": 0, "top1_holder_rate": 0.04, "rate": 0.04}]
    # But we need addr_type=2 with higher percentage and addr_type=0 with lower.
    # The real mock doesn't support custom data, so we test the builder's selection logic
    # by overriding pipeline.gmgn.fetch_top_holders.

    class _Mock:
        async def fetch_top_holders(self, token_mint, limit=20, credential_slot=None):
            return [
                {"address": "pool_wallet", "addr_type": 2, "amount_percentage": 0.5, "usd_value": 5000.0},
                {"address": "regular_wallet", "addr_type": 0, "amount_percentage": 0.04, "usd_value": 400.0},
                {"address": "regular_wallet2", "addr_type": 0, "amount_percentage": 0.03, "usd_value": 300.0},
            ]
        async def fetch_smart_degen_holders(self, token_mint, limit=100, credential_slot=None):
            return [
                {"address": "degen_max", "addr_type": 0, "amount_percentage": 0.03, "usd_value": 300.0},
                {"address": "degen_min", "addr_type": 0, "amount_percentage": 0.01, "usd_value": 100.0},
            ]
        async def fetch_token_snapshot(self, token_mint, credential_slot=None):
            return {}
        async def fetch_kline(self, token_mint, interval, limit, **kw):
            return []
        async def fetch_latest_price(self, token_mint, credential_slot=None):
            return {"price_usd": 0.001, "price_sol": 0.00001, "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0}

    gmgn = _Mock()

    pos_id = await repo.create_position(
        token_mint=tok, is_live=False,
        locked_strategy_config_json='{}',
        status="POSITION_OPEN", entry_price_usd=0.001,
        entry_token_amount=100.0, remaining_token_amount=100.0,
        remaining_value_usd=0.1, account_type="SIM",
    )

    te = await repo.append_trade_event(
        f"SIM_BUY_T1:{tok}", token_mint=tok, side="BUY",
        event_type="SIM_BUY", status="CONFIRMED", is_live=0,
        account_type="SIM", executed_token_amount=100.0,
        price_usd=0.001, trade_value_usd_net=-100.0,
    )

    entry_audit = await build_entry_audit_payload(
        repo=repo, gmgn=gmgn,
        token_mint=tok, position_id=pos_id,
        account_type="SIM", strategy={"id": 1},
        discovery_event_id=None, snapshot_id=None,
        buy_trade_event=te, quote={},
        token_amount=100.0, price_usd=0.001, price_sol=0.00001,
        size_usd=100.0,
    )

    # The top1 addr_type=0 should be "regular_wallet" (0.04), not "pool_wallet" (addr_type=2)
    assert entry_audit["top1_addr_type0_address"] == "regular_wallet", \
        f"Got {entry_audit['top1_addr_type0_address']}, expected regular_wallet"
    assert entry_audit["top1_addr_type0_holder_rate"] == 0.04


@pytest.mark.asyncio
async def test_smart_degen_max_min_holder_selection(pipeline_factory):
    pipeline, mock = pipeline_factory()
    repo = pipeline.repo
    tok = "DEGEN_TEST"

    class _Mock:
        async def fetch_top_holders(self, token_mint, limit=20, credential_slot=None):
            return []
        async def fetch_smart_degen_holders(self, token_mint, limit=100, credential_slot=None):
            return [
                {"address": "degen_max", "addr_type": 0, "amount_percentage": 0.05, "usd_value": 500.0},
                {"address": "degen_mid", "addr_type": 0, "amount_percentage": 0.03, "usd_value": 300.0},
                {"address": "degen_min", "addr_type": 0, "amount_percentage": 0.01, "usd_value": 100.0},
            ]
        async def fetch_token_snapshot(self, token_mint, credential_slot=None):
            return {}
        async def fetch_kline(self, token_mint, interval, limit, **kw):
            return []
        async def fetch_latest_price(self, token_mint, credential_slot=None):
            return {"price_usd": 0.001, "price_sol": 0.00001, "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0}

    gmgn = _Mock()

    pos_id = await repo.create_position(
        token_mint=tok, is_live=False,
        locked_strategy_config_json='{}',
        status="POSITION_OPEN", entry_price_usd=0.001,
        entry_token_amount=100.0, remaining_token_amount=100.0,
        remaining_value_usd=0.1, account_type="SIM",
    )

    te = await repo.append_trade_event(
        f"SIM_BUY_DG:{tok}", token_mint=tok, side="BUY",
        event_type="SIM_BUY", status="CONFIRMED", is_live=0,
        account_type="SIM", executed_token_amount=100.0,
        price_usd=0.001, trade_value_usd_net=-100.0,
    )

    entry_audit = await build_entry_audit_payload(
        repo=repo, gmgn=gmgn,
        token_mint=tok, position_id=pos_id,
        account_type="SIM", strategy={"id": 1},
        discovery_event_id=None, snapshot_id=None,
        buy_trade_event=te, quote={},
        token_amount=100.0, price_usd=0.001, price_sol=0.00001,
        size_usd=100.0,
    )

    assert entry_audit["smart_degen_max_holder_address"] == "degen_max"
    assert entry_audit["smart_degen_max_holder_pct"] == 0.05
    assert entry_audit["smart_degen_max_holder_usd"] == 500.0
    assert entry_audit["smart_degen_min_holder_address"] == "degen_min"
    assert entry_audit["smart_degen_min_holder_pct"] == 0.01
    assert entry_audit["smart_degen_min_holder_usd"] == 100.0


@pytest.mark.asyncio
async def test_exit_audit_sell_multiple(pipeline_factory):
    pipeline, mock = pipeline_factory()
    repo = pipeline.repo
    tok = "MULTI_TEST"
    mock.tokens[tok] = {
        "token_mint": tok, "symbol": "MT", "name": "Multi Test",
        "pool_address": "pool1", "type": "new_creation",
        "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0,
        "price_usd": 0.001, "price_sol": 0.00001,
    }
    mock.latest[tok] = {"price_usd": 0.001, "price_sol": 0.00001, "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0}

    pos_id = await repo.create_position(
        token_mint=tok, is_live=False,
        locked_strategy_config_json='{"id": 1}',
        status="POSITION_OPEN", entry_price_usd=0.001,
        entry_token_amount=100.0, remaining_token_amount=100.0,
        remaining_value_usd=0.1, account_type="SIM",
    )

    # Insert ENTRY audit with buy_price_usd=0.001
    await repo.insert_position_audit(
        position_id=pos_id, token_mint=tok, account_type="SIM",
        strategy_id=1, discovery_event_id=None, snapshot_id=None,
        audit_type="ENTRY",
        audit_json={"buy_price_usd": 0.001},
    )

    sell_te = await repo.append_trade_event(
        f"SELL_MULTI:{tok}", position_id=pos_id,
        token_mint=tok, side="SELL", event_type="SIM_SELL",
        status="CONFIRMED", is_live=0, account_type="SIM",
        executed_token_amount=100.0, price_usd=0.0016,
        exit_reason="HARD_TP_160", exit_reason_label="TP",
        trade_value_usd_net=0.16, gross_value_usd=0.16,
    )

    position = await repo.get_position(pos_id)
    exit_audit = await build_exit_audit_payload(
        repo=repo, position=position,
        sell_trade_event=sell_te,
        exit_reason="HARD_TP_160", exit_pct=1.0,
        sell_amount_human=100.0, gross_value_usd=0.16,
        current_price_usd=0.0016,
    )

    assert exit_audit["sell_price_multiple"] == 1.60, \
        f"Expected 1.60, got {exit_audit['sell_price_multiple']}"
    assert exit_audit["exit_reason_code"] == "HARD_TP_160"
    assert exit_audit["exit_reason_label"] == "硬止盈：价格超过 1.6x，撤仓50%"


@pytest.mark.asyncio
async def test_risk_recheck_failed_audit_details(pipeline_factory):
    pipeline, mock = pipeline_factory()
    repo = pipeline.repo
    tok = "RISK_FAIL"
    mock.tokens[tok] = {
        "token_mint": tok, "symbol": "RF", "name": "Risk Fail",
        "pool_address": "pool1", "type": "new_creation",
        "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0,
        "price_usd": 0.001, "price_sol": 0.00001,
    }
    mock.latest[tok] = {"price_usd": 0.001, "price_sol": 0.00001, "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0}

    pos_id = await repo.create_position(
        token_mint=tok, is_live=False,
        locked_strategy_config_json='{}',
        status="POSITION_OPEN", entry_price_usd=0.001,
        entry_token_amount=100.0, remaining_token_amount=100.0,
        remaining_value_usd=0.1, account_type="SIM",
    )

    await repo.insert_position_audit(
        position_id=pos_id, token_mint=tok, account_type="SIM",
        strategy_id=1, discovery_event_id=None, snapshot_id=None,
        audit_type="ENTRY",
        audit_json={"buy_price_usd": 0.001},
    )

    risk_failed_rules = [
        {"rule": "rug_ratio", "label": "超Rug比例", "value": 0.32,
         "threshold": "< x = 0.20", "passed": False,
         "reason": "rug_ratio 超过持仓风控阈值"},
        {"rule": "holder_count", "label": "持有者数量范围", "value": 18,
         "threshold": "37 - 40*x < holder_count < 400 + 2000*x", "passed": False,
         "reason": "holder_count 低于下限"},
    ]

    sell_te = await repo.append_trade_event(
        f"SELL_RISK:{tok}", position_id=pos_id,
        token_mint=tok, side="SELL", event_type="SIM_SELL",
        status="CONFIRMED", is_live=0, account_type="SIM",
        executed_token_amount=100.0, price_usd=0.0008,
        exit_reason="RISK_RECHECK_FAILED",
        exit_reason_label="持仓风控复查失败",
        trade_value_usd_net=0.08, gross_value_usd=0.08,
    )

    position = await repo.get_position(pos_id)
    exit_audit = await build_exit_audit_payload(
        repo=repo, position=position,
        sell_trade_event=sell_te,
        exit_reason="RISK_RECHECK_FAILED", exit_pct=1.0,
        sell_amount_human=100.0, gross_value_usd=0.08,
        current_price_usd=0.0008,
        risk_failed_rules=risk_failed_rules,
    )

    assert exit_audit["risk_failed_rules"] == risk_failed_rules
    assert len(exit_audit["risk_failed_rules"]) == 2
    assert exit_audit["risk_failed_rules"][0]["rule"] == "rug_ratio"
    assert exit_audit["risk_failed_rules"][0]["passed"] is False


@pytest.mark.asyncio
async def test_dust_exit_audit_detail(pipeline_factory):
    pipeline, mock = pipeline_factory()
    repo = pipeline.repo
    tok = "DUST_TEST"
    mock.tokens[tok] = {
        "token_mint": tok, "symbol": "DT", "name": "Dust Test",
        "pool_address": "pool1", "type": "new_creation",
        "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0,
        "price_usd": 0.001, "price_sol": 0.00001,
    }
    mock.latest[tok] = {"price_usd": 0.001, "price_sol": 0.00001, "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0}

    pos_id = await repo.create_position(
        token_mint=tok, is_live=False,
        locked_strategy_config_json='{}',
        status="POSITION_OPEN", entry_price_usd=0.001,
        entry_token_amount=10.0, remaining_token_amount=10.0,
        remaining_value_usd=0.01, account_type="SIM",
    )

    await repo.insert_position_audit(
        position_id=pos_id, token_mint=tok, account_type="SIM",
        strategy_id=1, discovery_event_id=None, snapshot_id=None,
        audit_type="ENTRY",
        audit_json={"buy_price_usd": 0.001},
    )

    dust_detail = {
        "remaining_value_usd_before": 0.01,
        "dust_threshold": 12.5,
        "current_price_usd": 0.001,
        "remaining_token_amount_before": 10.0,
    }

    sell_te = await repo.append_trade_event(
        f"SELL_DUST:{tok}", position_id=pos_id,
        token_mint=tok, side="SELL", event_type="DUST_FORCE_EXIT",
        status="CONFIRMED", is_live=0, account_type="SIM",
        executed_token_amount=10.0, price_usd=0.001,
        exit_reason="DUST_FORCE_EXIT",
        exit_reason_label="尘埃仓强制清仓",
        trade_value_usd_net=0.01, gross_value_usd=0.01,
    )

    position = await repo.get_position(pos_id)
    exit_audit = await build_exit_audit_payload(
        repo=repo, position=position,
        sell_trade_event=sell_te,
        exit_reason="DUST_FORCE_EXIT", exit_pct=1.0,
        sell_amount_human=10.0, gross_value_usd=0.01,
        current_price_usd=0.001,
        dust_detail=dust_detail,
    )

    assert exit_audit["dust_detail"] == dust_detail
    assert exit_audit["dust_detail"]["remaining_value_usd_before"] == 0.01
    assert exit_audit["dust_detail"]["dust_threshold"] == 12.5
    assert exit_audit["exit_reason_code"] == "DUST_FORCE_EXIT"
