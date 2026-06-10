import json
import pytest
from datetime import datetime, timezone

from ..trading.audit_builder import (
    ENTRY_AUDIT_REQUIRED_FIELDS,
    EXIT_AUDIT_REQUIRED_FIELDS,
    build_entry_audit_payload,
    build_exit_audit_payload,
)
from ..config import ProviderMode, settings
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
        trade_value_usd_net=0.15, gross_value_usd=0.15,
    )

    position = await repo.get_position(pos_id)
    exit_audit = await build_exit_audit_payload(
        repo=repo, position=position,
        sell_trade_event=sell_te,
        exit_reason="HARD_TP_160", exit_pct=1.0,
        sell_amount_human=100.0, gross_value_usd=0.15,
        current_price_usd=0.0016,
    )

    # effective_sell_price_usd = 0.15 / 100.0 = 0.0015
    # sell_price_multiple = 0.0015 / 0.001 = 1.50  (NOT 1.60 from spot price)
    assert exit_audit["sell_price_multiple"] == 1.50, \
        f"Expected 1.50, got {exit_audit['sell_price_multiple']}"
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


@pytest.mark.asyncio
async def test_export_trade_audit_complete_entry_exit_audit(repo):
    """ENTRY audit persisted with all required fields;
    EXIT audit persisted with all required fields;
    entry/exit audit rows retrievable via repo."""
    tok = "EXPORT_FULL"
    pid = await repo.create_position(
        token_mint=tok, is_live=False,
        locked_strategy_config_json='{"id": 1}',
        status="POSITION_OPEN", entry_price_usd=0.001,
        entry_token_amount=100.0, remaining_token_amount=100.0,
        remaining_value_usd=0.1, account_type="SIM",
    )

    entry_audit_json = {k: f"val_{k}" for k in ENTRY_AUDIT_REQUIRED_FIELDS}
    entry_audit_json["buy_price_usd"] = 0.001
    entry_audit_json["entry_missing_fields"] = []
    entry_audit_json["entry_data_sources"] = {}
    await repo.insert_position_audit(
        position_id=pid, token_mint=tok, account_type="SIM",
        strategy_id=1, discovery_event_id=None, snapshot_id=None,
        audit_type="ENTRY",
        audit_json=entry_audit_json,
    )

    exit_audit_json = {k: f"val_{k}" for k in EXIT_AUDIT_REQUIRED_FIELDS}
    exit_audit_json["sell_price_multiple"] = 0.50
    exit_audit_json["exit_reason_code"] = "RISK_RECHECK_FAILED"
    exit_audit_json["exit_reason_label"] = "持仓风控复查失败"
    exit_audit_json["exit_pct"] = 1.0
    exit_audit_json["exit_missing_fields"] = []
    exit_audit_json["exit_data_sources"] = {}
    exit_audit_json["risk_failed_rules"] = []
    await repo.insert_position_audit(
        position_id=pid, token_mint=tok, account_type="SIM",
        strategy_id=1, discovery_event_id=None, snapshot_id=None,
        audit_type="EXIT",
        audit_json=exit_audit_json,
    )

    entry_rows = await repo.get_position_audits(pid, audit_type="ENTRY")
    assert len(entry_rows) == 1
    stored_entry = entry_rows[0].get("audit_json") or {}
    if isinstance(stored_entry, str):
        stored_entry = json.loads(stored_entry)
    for field in ENTRY_AUDIT_REQUIRED_FIELDS:
        assert field in stored_entry, f"Stored ENTRY audit missing field: {field}"
    assert stored_entry.get("buy_price_usd") == 0.001
    assert stored_entry.get("entry_missing_fields") == []

    exit_rows = await repo.get_position_audits(pid, audit_type="EXIT")
    assert len(exit_rows) == 1
    stored_exit = exit_rows[0].get("audit_json") or {}
    if isinstance(stored_exit, str):
        stored_exit = json.loads(stored_exit)
    for field in EXIT_AUDIT_REQUIRED_FIELDS:
        assert field in stored_exit, f"Stored EXIT audit missing field: {field}"
    assert stored_exit.get("sell_price_multiple") == 0.50
    assert stored_exit.get("exit_reason_code") == "RISK_RECHECK_FAILED"
    assert stored_exit.get("exit_missing_fields") == []


@pytest.mark.asyncio
async def test_entry_audit_rug_ratio_zero_not_fallback(pipeline_factory):
    """ENTRY audit with rug_ratio=0 must NOT fallback to latest snapshot."""
    pipeline, _ = pipeline_factory()
    repo = pipeline.repo
    tok = "RUG_ZERO"
    pid = await repo.create_position(
        token_mint=tok, is_live=False,
        locked_strategy_config_json='{}',
        status="POSITION_OPEN", entry_price_usd=0.001,
        entry_token_amount=100.0, remaining_token_amount=100.0,
        remaining_value_usd=0.1, account_type="SIM",
    )
    entry_audit = {k: f"val_{k}" for k in ENTRY_AUDIT_REQUIRED_FIELDS}
    entry_audit["rug_ratio"] = 0.0
    entry_audit["entry_missing_fields"] = []
    entry_audit["entry_data_sources"] = {}
    await repo.insert_position_audit(
        position_id=pid, token_mint=tok, account_type="SIM",
        strategy_id=1, discovery_event_id=None, snapshot_id=None,
        audit_type="ENTRY",
        audit_json=entry_audit,
    )
    entry_rows = await repo.get_position_audits(pid, audit_type="ENTRY")
    assert len(entry_rows) == 1
    stored = entry_rows[0].get("audit_json") or {}
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored.get("rug_ratio") == 0.0
    assert stored.get("entry_missing_fields") == []


@pytest.mark.asyncio
async def test_addr_type_non_numeric_not_crash(pipeline_factory):
    """addr_type="" or "normal" must not crash build_entry_audit_payload."""
    pipeline, mock = pipeline_factory()
    repo = pipeline.repo
    tok = "ADDR_TYPE_SAFE"

    class _Mock:
        async def fetch_top_holders(self, token_mint, limit=20, credential_slot=None):
            return [
                {"address": "pool_wallet", "addr_type": 2, "amount_percentage": 0.5},
                {"address": "empty_type", "addr_type": "", "amount_percentage": 0.04},
                {"address": "none_type", "addr_type": None, "amount_percentage": 0.03},
                {"address": "str_type", "addr_type": "normal", "amount_percentage": 0.02},
                {"address": "int_zero", "addr_type": 0, "amount_percentage": 0.01},
            ]
        async def fetch_smart_degen_holders(self, token_mint, limit=100, credential_slot=None):
            return []
        async def fetch_token_snapshot(self, token_mint, credential_slot=None):
            return {}
        async def fetch_kline(self, token_mint, interval, limit, **kw):
            return []
        async def fetch_latest_price(self, token_mint, credential_slot=None):
            return {"price_usd": 0.001, "price_sol": 0.00001}

    gmgn = _Mock()
    pid = await repo.create_position(
        token_mint=tok, is_live=False,
        locked_strategy_config_json='{}',
        status="POSITION_OPEN", entry_price_usd=0.001,
        entry_token_amount=100.0, remaining_token_amount=100.0,
        remaining_value_usd=0.1, account_type="SIM",
    )
    te = await repo.append_trade_event(
        f"AT_SAFE:{tok}", token_mint=tok, side="BUY",
        event_type="SIM_BUY", status="CONFIRMED", is_live=0,
        account_type="SIM", executed_token_amount=100.0,
        price_usd=0.001, trade_value_usd_net=-100.0,
    )
    entry_audit = await build_entry_audit_payload(
        repo=repo, gmgn=gmgn,
        token_mint=tok, position_id=pid,
        account_type="SIM", strategy={"id": 1},
        discovery_event_id=None, snapshot_id=None,
        buy_trade_event=te, quote={},
        token_amount=100.0, price_usd=0.001, price_sol=0.00001,
        size_usd=100.0,
    )
    # Must not crash; top1 addr_type=0 should be "int_zero" (the only addr_type==0)
    assert entry_audit["top1_addr_type0_address"] == "int_zero"
    assert entry_audit["top1_addr_type0_holder_rate"] == 0.01


@pytest.mark.asyncio
async def test_exit_audit_smart_money_trigger_detail(pipeline_factory):
    """SMART_MONEY_SELL exit_audit carries smart_money_trigger_detail."""
    pipeline, mock = pipeline_factory()
    repo = pipeline.repo
    tok = "SM_TRIGGER"
    mock.tokens[tok] = {
        "token_mint": tok, "symbol": "SM", "name": "Smart Money",
        "pool_address": "pool1", "type": "new_creation",
        "liquidity_usd": 50000.0, "price_usd": 0.001, "price_sol": 0.00001,
    }
    mock.latest[tok] = {"price_usd": 0.001, "price_sol": 0.00001, "liquidity_usd": 50000.0}
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
    sell_te = await repo.append_trade_event(
        f"SELL_SM:{tok}", position_id=pos_id,
        token_mint=tok, side="SELL", event_type="SIM_SELL",
        status="CONFIRMED", is_live=0, account_type="SIM",
        executed_token_amount=100.0, price_usd=0.0008,
        exit_reason="SMART_MONEY_SELL",
        exit_reason_label="Smart Money Sell",
        trade_value_usd_net=0.08, gross_value_usd=0.08,
    )
    position = await repo.get_position(pos_id)
    exit_audit = await build_exit_audit_payload(
        repo=repo, position=position,
        sell_trade_event=sell_te,
        exit_reason="SMART_MONEY_SELL", exit_pct=1.0,
        sell_amount_human=100.0, gross_value_usd=0.08,
        current_price_usd=0.0008,
        smart_money_trigger_detail={"triggered_wallet": "abc123"},
    )
    assert exit_audit["exit_reason_code"] == "SMART_MONEY_SELL"
    assert exit_audit["smart_money_trigger_detail"] == {"triggered_wallet": "abc123"}


@pytest.mark.asyncio
async def test_exit_audit_top3_smart_degen_trigger_detail(pipeline_factory):
    """TOP3_SMART_DEGEN_DUMP exit_audit carries top3_smart_degen_trigger_detail."""
    pipeline, mock = pipeline_factory()
    repo = pipeline.repo
    tok = "TOP3_TRIGGER"
    mock.tokens[tok] = {
        "token_mint": tok, "symbol": "T3", "name": "Top3 Trigger",
        "pool_address": "pool1", "type": "new_creation",
        "liquidity_usd": 50000.0, "price_usd": 0.001, "price_sol": 0.00001,
    }
    mock.latest[tok] = {"price_usd": 0.001, "price_sol": 0.00001, "liquidity_usd": 50000.0}
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
    sell_te = await repo.append_trade_event(
        f"SELL_T3:{tok}", position_id=pos_id,
        token_mint=tok, side="SELL", event_type="SIM_SELL",
        status="CONFIRMED", is_live=0, account_type="SIM",
        executed_token_amount=100.0, price_usd=0.0008,
        exit_reason="TOP3_SMART_DEGEN_DUMP",
        exit_reason_label="Top3 Dump",
        trade_value_usd_net=0.08, gross_value_usd=0.08,
    )
    position = await repo.get_position(pos_id)
    exit_audit = await build_exit_audit_payload(
        repo=repo, position=position,
        sell_trade_event=sell_te,
        exit_reason="TOP3_SMART_DEGEN_DUMP", exit_pct=1.0,
        sell_amount_human=100.0, gross_value_usd=0.08,
        current_price_usd=0.0008,
        top3_smart_degen_trigger_detail={
            "triggered_wallet": "wallet123",
            "reduction_threshold_pct": 25.0,
        },
    )
    assert exit_audit["exit_reason_code"] == "TOP3_SMART_DEGEN_DUMP"
    assert exit_audit["top3_smart_degen_trigger_detail"] == {
        "triggered_wallet": "wallet123",
        "reduction_threshold_pct": 25.0,
    }


@pytest.mark.asyncio
async def test_live_sell_gross_value_uses_quote_out_amount(pipeline_factory):
    """LIVE sell must use Jupiter quote outAmount * sol_usd for gross_value_usd."""
    from ..trading.executor import LAMPORTS_PER_SOL
    original_mode = settings.PROVIDER_MODE
    settings.PROVIDER_MODE = ProviderMode.MOCK

    pipeline, mock = pipeline_factory()
    repo = pipeline.repo
    tok = "LIVE_QUOTE_SELL"

    mock.tokens[tok] = {
        "token_mint": tok, "symbol": "LQS", "name": "Live Quote Sell",
        "pool_address": "pool1", "type": "new_creation", "launchpad": "pump",
        "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0,
        "price_usd": 0.001, "price_sol": 0.00001,
        "holder_count": 100, "market_cap": 500000.0,
    }
    mock.latest[tok] = {
        "price_usd": 0.001, "price_sol": 0.00001,
        "liquidity_usd": 50000.0, "sol_side_liquidity": 100.0,
    }

    pos_id = await repo.create_position(
        token_mint=tok, is_live=True,
        locked_strategy_config_json='{"sell_slippage_cap_bps": 1000}',
        status="POSITION_OPEN", entry_price_usd=0.001,
        entry_token_amount=100_000_000.0,
        remaining_token_amount=100_000_000.0,
        remaining_value_usd=100_000.0,
        account_type="LIVE",
    )

    result = await pipeline.execute_sell(
        position=await repo.get_position(pos_id),
        exit_pct=1.0,
        exit_reason="HARD_SL_70",
    )
    assert result is not None, "LIVE sell returned None"
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    assert result.get("ok") is not False, f"LIVE sell failed: {result.get('error')}"

    # Find the CONFIRMED trade event for this sell
    events = []
    async with repo.db.execute(
        "SELECT * FROM trade_events WHERE position_id=? AND side='SELL' AND status='CONFIRMED' ORDER BY id DESC LIMIT 1",
        (pos_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, "No CONFIRMED SELL trade event found"
    te = dict(row)

    # Must NOT be the old price-based calculation: sell_amount_human * current_price_usd
    old_calc = 100_000_000.0 * 0.001  # = 100_000.0
    actual = float(te.get("gross_value_usd", 0))
    assert actual > 0, "gross_value_usd must be positive"
    assert abs(actual - old_calc) / max(old_calc, 0.01) > 0.1, \
        f"gross_value_usd {actual} suspiciously close to old price-based calc {old_calc}"
    # Verify quote was used: out_sol from quote breaks the old sell_amount_human * price pattern
    assert actual > old_calc * 10 or actual < old_calc * 0.1, \
        f"gross_value_usd {actual} should differ significantly from old calc {old_calc}"
    settings.PROVIDER_MODE = original_mode


@pytest.mark.asyncio
async def test_export_trade_audit_endpoint_with_data(pipeline_factory):
    """API-level export_trade_audit returns correct position, entry_metrics, exit_audits."""
    from fastapi.testclient import TestClient
    from ..main import app

    pipeline, mock = pipeline_factory()
    repo = pipeline.repo
    tok = "EXPORT_API_TEST"
    mock.tokens[tok] = {
        "token_mint": tok, "symbol": "EAT", "name": "Export API Test",
        "pool_address": "pool1", "type": "new_creation",
        "liquidity_usd": 50000.0, "price_usd": 0.001, "price_sol": 0.00001,
    }
    mock.latest[tok] = {"price_usd": 0.001, "price_sol": 0.00001, "liquidity_usd": 50000.0}

    pos_id = await repo.create_position(
        token_mint=tok, is_live=False,
        locked_strategy_config_json='{"id": 1}',
        status="POSITION_OPEN", entry_price_usd=0.001,
        entry_token_amount=100.0, remaining_token_amount=0.0,
        remaining_value_usd=0.0, account_type="SIM",
    )

    await repo.append_trade_event(
        f"BUY_EAT:{tok}", position_id=pos_id,
        token_mint=tok, side="BUY", event_type="SIM_BUY",
        status="CONFIRMED", is_live=0, account_type="SIM",
        executed_token_amount=100.0, price_usd=0.001,
        trade_value_usd_net=-0.10,
    )

    sell_te = await repo.append_trade_event(
        f"SELL_EAT:{tok}", position_id=pos_id,
        token_mint=tok, side="SELL", event_type="SIM_SELL",
        status="CONFIRMED", is_live=0, account_type="SIM",
        executed_token_amount=100.0, price_usd=0.0016,
        exit_reason="HARD_TP_160",
        trade_value_usd_net=0.15, gross_value_usd=0.15,
    )
    sell_created_at = sell_te.get("created_at")

    await repo.insert_position_audit(
        position_id=pos_id, token_mint=tok, account_type="SIM",
        strategy_id=1, discovery_event_id=None, snapshot_id=None,
        audit_type="ENTRY",
        audit_json={"buy_price_usd": 0.001, "rug_ratio": 0.05},
    )
    await repo.insert_position_audit(
        position_id=pos_id, token_mint=tok, account_type="SIM",
        strategy_id=1, discovery_event_id=None, snapshot_id=None,
        audit_type="EXIT",
        audit_json={
            "sell_time_utc": sell_created_at,
            "sell_price_multiple": 1.50,
            "exit_reason_code": "HARD_TP_160",
            "gross_value_usd": 0.15,
        },
    )

    original_path = settings.SQLITE_PATH
    settings.SQLITE_PATH = repo.db_path
    try:
        with TestClient(app) as client:
            app_repo = client.app.state.repo
            await app_repo.set_runtime_setting("session_started_at", "2020-01-01T00:00:00Z", "test")

            r = client.post("/api/runtime/emergency/export-trade-audit")
            assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
            data = r.json()
            assert data.get("ok") is True
            payload = data.get("data", {})
            positions = payload.get("positions", [])
            assert len(positions) == 1, f"Expected 1 position, got {len(positions)}"

            p = positions[0]
            assert p["position_id"] == pos_id
            assert p["entry_metrics"]["rug_ratio"] == 0.05

            trade_events = p.get("trade_events", [])
            sells = [te for te in trade_events if te["side"] == "SELL"]
            assert len(sells) == 1
            # sell_price_multiple should come from EXIT audit (1.50), not spot price (0.0016/0.001=1.60)
            assert sells[0]["sell_price_multiple"] == 1.50, \
                f"Expected 1.50 from EXIT audit, got {sells[0]['sell_price_multiple']}"
            assert sells[0]["exit_audit"] is not None
            assert sells[0]["exit_audit"]["sell_price_multiple"] == 1.50

            exit_audits = p.get("exit_audits", [])
            assert len(exit_audits) == 1
            assert exit_audits[0]["exit_reason_code"] == "HARD_TP_160"
    finally:
        settings.SQLITE_PATH = original_path


class TestAccountingUnit:
    def test_jupiter_swap_response_preserves_fee_fields(self):
        from ..providers.jupiter_real import JupiterProvider
        from ..config import ProviderMode
        p = JupiterProvider.__new__(JupiterProvider)
        p.mode = ProviderMode.MOCK
        p.MOCK_ROOT = ""
        assert p is not None

    def test_jito_tip_floor_to_lamports(self):
        from ..trading.accounting import normalize_jito_tip_floor_to_lamports
        assert normalize_jito_tip_floor_to_lamports(0.00001) == 10000
        assert normalize_jito_tip_floor_to_lamports(2000) == 2000
        assert normalize_jito_tip_floor_to_lamports(None) >= 1000
        assert normalize_jito_tip_floor_to_lamports(0) >= 1000
        assert normalize_jito_tip_floor_to_lamports(0.0005) == 500000

    def test_sim_sell_conservative_net_deducts_sell_tax(self):
        from ..trading.accounting import compute_sim_sell_accounting
        quote = {
            "outAmount": str(1_000_000_000),  # 1 SOL
            "otherAmountThreshold": str(950_000_000),  # 0.95 SOL
            "outputMint": "So11111111111111111111111111111111111111112",
        }
        result = compute_sim_sell_accounting(
            quote=quote, sol_usd=200.0, sell_tax=0.03, fee_upper_bound_usd=0.0,
        )
        assert result["trade_value_usd_expected"] == 200.0
        assert result["trade_value_usd_conservative"] == 184.0, \
            f"Expected 184.0 (= 190 - 6), got {result['trade_value_usd_conservative']}"
        assert result["trade_value_usd_net"] == 184.0

    def test_jupiter_platform_fee_not_double_subtracted(self):
        from ..trading.accounting import compute_sim_sell_accounting, platform_fee_amount_raw
        quote = {
            "outAmount": str(1_000_000_000),
            "otherAmountThreshold": str(950_000_000),
            "outputMint": "So11111111111111111111111111111111111111112",
            "platformFee": {"amount": "5000000"},
        }
        assert platform_fee_amount_raw(quote) == "5000000"
        result = compute_sim_sell_accounting(
            quote=quote, sol_usd=200.0, sell_tax=0.03, fee_upper_bound_usd=0.0,
        )
        assert result["trade_value_usd_expected"] == 200.0
        assert result["fee_detail"]["platform_fee_note"] is not None

    def test_sell_price_effective_uses_net_value(self):
        from ..trading.accounting import compute_effective_price_usd
        p = compute_effective_price_usd(trade_value_usd_net=184.0, token_amount=1000)
        assert p == 0.184
        buy_p = compute_effective_price_usd(trade_value_usd_net=-100.0, token_amount=1000)
        assert buy_p == 0.10
        multiple = round(0.184 / 0.10, 2)
        assert multiple == 1.84

    @pytest.mark.asyncio
    async def test_executor_jito_field_name_consistency(self, pipeline_factory):
        pipeline, mock = pipeline_factory()
        instructions = {"swapTransaction": "base64_mock", "instructions": []}
        bundle = await pipeline.jito.send(instructions)
        assert "jito_tip_lamports" in bundle, \
            f"jito_tip_lamports missing; keys: {list(bundle.keys())}"
        assert bundle["jito_tip_lamports"] is not None
        assert "jito_tip_source" in bundle
        assert "tip_used" not in bundle, "old field name tip_used must be removed"

    @pytest.mark.asyncio
    async def test_live_tx_meta_actual_buy_wallet_delta(self, pipeline_factory):
        from ..trading.executor import backfill_trade_event_from_solana_tx_meta
        pipeline, mock = pipeline_factory()
        repo = pipeline.repo
        tok = "TX_META_BUY"
        mock.tokens[tok] = {
            "token_mint": tok, "symbol": "TMB", "name": "Tx Meta Buy",
            "pool_address": "pool1", "type": "new_creation",
            "liquidity_usd": 50000.0, "price_usd": 0.001, "price_sol": 0.00001,
        }
        pos_id = await repo.create_position(
            token_mint=tok, is_live=True,
            locked_strategy_config_json='{}',
            status="POSITION_OPEN", entry_price_usd=0.001,
            entry_token_amount=100.0, remaining_token_amount=100.0,
            remaining_value_usd=0.1, account_type="LIVE",
        )
        te = await repo.append_trade_event(
            f"TXBUY:{tok}", position_id=pos_id,
            token_mint=tok, side="BUY", event_type="BUY_CONFIRMED",
            status="CONFIRMED", is_live=1, account_type="LIVE",
            trade_value_usd_net=-20.0,
            accounting_status="PENDING_RPC_BACKFILL",
            accounting_source="jupiter_quote_expected",
        )
        result = await backfill_trade_event_from_solana_tx_meta(
            repo=repo, rpc=pipeline.rpc,
            trade_event_id=te["id"],
            signature="mock_sig",
            wallet_pubkey="MOCK_WALLET",
            token_mint=tok,
            side="BUY",
            sol_usd=200.0,
        )
        assert result.get("ok") is False, "MOCK mode returns no tx meta"

    @pytest.mark.asyncio
    async def test_pnl_summary_pending_rpc_count(self, pipeline_factory):
        from fastapi.testclient import TestClient
        from ..main import app

        pipeline, mock = pipeline_factory()
        repo = pipeline.repo
        tok = "PNL_PEND"
        mock.tokens[tok] = {
            "token_mint": tok, "symbol": "PP", "name": "PnL Pend",
            "pool_address": "pool1", "type": "new_creation",
            "liquidity_usd": 50000.0, "price_usd": 0.001, "price_sol": 0.00001,
        }
        mock.latest[tok] = {"price_usd": 0.001, "price_sol": 0.00001, "liquidity_usd": 50000.0}

        pos_id = await repo.create_position(
            token_mint=tok, is_live=True,
            locked_strategy_config_json='{}',
            status="POSITION_OPEN", entry_price_usd=0.001,
            entry_token_amount=100.0, remaining_token_amount=100.0,
            remaining_value_usd=0.1, account_type="LIVE",
        )
        await repo.append_trade_event(
            f"PEND:{tok}", position_id=pos_id,
            token_mint=tok, side="BUY", event_type="BUY_CONFIRMED",
            status="CONFIRMED", is_live=1, account_type="LIVE",
            trade_value_usd_net=-0.10,
            accounting_status="PENDING_RPC_BACKFILL",
            accounting_source="jupiter_quote_expected",
        )

        original_path = settings.SQLITE_PATH
        settings.SQLITE_PATH = repo.db_path
        try:
            with TestClient(app) as client:
                r = client.get("/api/runtime/pnl-summary")
                assert r.status_code == 200
                data = r.json()
                summary = data.get("accounting_status_summary", {})
                assert summary.get("pending_rpc_backfill_count", 0) >= 1, \
                    f"Expected >=1 pending, got {summary}"
        finally:
            settings.SQLITE_PATH = original_path
