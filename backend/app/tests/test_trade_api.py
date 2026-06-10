import pytest
from fastapi.testclient import TestClient
from ..config import settings
from ..main import app


@pytest.mark.asyncio
async def test_trade_events_ledger_not_500():
    with TestClient(app) as client:
        r = client.get("/api/runtime/trade-events-ledger")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data


@pytest.mark.asyncio
async def test_export_trade_audit_not_500():
    with TestClient(app) as client:
        r = client.post("/api/runtime/emergency/export-trade-audit")
        assert r.status_code == 200
        data = r.json()
        assert "export_path" in data


@pytest.mark.asyncio
async def test_pnl_summary_buy100_remaining105(repo):
    """BUY $100 position with $105 remaining → total_pnl_usd = +5 (not -95)."""
    pid = await repo.create_position(
        "MINT_PNL", False, "{}", status="POSITION_OPEN",
        entry_price_usd=1.0, entry_token_amount=100.0,
        remaining_token_amount=100.0, remaining_value_usd=105.0,
        live_strategy_id=None,
    )
    await repo.append_trade_event(
        "PNL_BUY_ONLY", token_mint="MINT_PNL", side="BUY",
        event_type="SIM_BUY", status="CONFIRMED",
        is_live=0, account_type="SIM", position_id=pid,
        trade_value_usd_net=-100.0,
    )

    original_path = settings.SQLITE_PATH
    settings.SQLITE_PATH = repo.db_path
    try:
        with TestClient(app) as client:
            r = client.get("/api/runtime/pnl-summary")
            assert r.status_code == 200
            data = r.json()
            sim = data.get("sim", {})
            total = sim.get("total_pnl_usd", 0)
            msg = f"Expected ~+5, got {total}"
            assert abs(total - 5.0) < 0.001, msg
            assert sim.get("open_positions", 0) == 1
    finally:
        settings.SQLITE_PATH = original_path


@pytest.mark.asyncio
async def test_pnl_summary_live_separate(repo):
    """SIM and LIVE PnL are computed independently."""
    sim_pid = await repo.create_position(
        "MINT_SIM", False, "{}", status="CLOSED",
        entry_price_usd=1.0, entry_token_amount=200.0,
        remaining_token_amount=0.0, remaining_value_usd=0.0,
        live_strategy_id=None,
    )
    await repo.append_trade_event(
        "SIM_B", token_mint="MINT_SIM", side="BUY",
        event_type="SIM_BUY", status="CONFIRMED",
        is_live=0, account_type="SIM", position_id=sim_pid,
        trade_value_usd_net=-200.0,
    )
    await repo.append_trade_event(
        "SIM_S", token_mint="MINT_SIM", side="SELL",
        event_type="SIM_SELL", status="CONFIRMED",
        is_live=0, account_type="SIM", position_id=sim_pid,
        trade_value_usd_net=180.0,
        exit_reason="HARD_SL_45", exit_reason_label="硬止损45%全平",
    )
    live_pid = await repo.create_position(
        "MINT_LIVE", True, "{}", status="CLOSED",
        entry_price_usd=1.0, entry_token_amount=300.0,
        remaining_token_amount=0.0, remaining_value_usd=0.0,
        live_strategy_id=None,
    )
    await repo.append_trade_event(
        "LIVE_B", token_mint="MINT_LIVE", side="BUY",
        event_type="LIVE_BUY_CONFIRMED", status="CONFIRMED",
        is_live=1, account_type="LIVE", position_id=live_pid,
        trade_value_usd_net=-300.0,
    )
    await repo.append_trade_event(
        "LIVE_S", token_mint="MINT_LIVE", side="SELL",
        event_type="LIVE_SELL_CONFIRMED", status="CONFIRMED",
        is_live=1, account_type="LIVE", position_id=live_pid,
        trade_value_usd_net=330.0,
        exit_reason="HARD_TP_160", exit_reason_label="硬止盈1.6x撤50%",
    )

    original_path = settings.SQLITE_PATH
    settings.SQLITE_PATH = repo.db_path
    try:
        with TestClient(app) as client:
            r = client.get("/api/runtime/pnl-summary")
            assert r.status_code == 200
            data = r.json()
            sim = data.get("sim", {})
            live = data.get("live", {})
            assert abs(sim["realized_pnl_usd"] - (-20.0)) < 0.001
            assert abs(live["realized_pnl_usd"] - 30.0) < 0.001
            assert sim["closed_positions"] == 1
            assert live["closed_positions"] == 1
            assert sim["losing_positions"] == 1
            assert live["winning_positions"] == 1
    finally:
        settings.SQLITE_PATH = original_path


@pytest.mark.asyncio
async def test_trade_events_ledger_items(repo):
    """trade-events-ledger returns one row per BUY/SELL with exit_reason."""
    await repo.append_trade_event(
        "TEL_B", token_mint="MINT_TEL", side="BUY",
        event_type="SIM_BUY", status="CONFIRMED",
        is_live=0, account_type="SIM",
        trade_value_usd_net=-50.0,
    )
    await repo.append_trade_event(
        "TEL_S", token_mint="MINT_TEL", side="SELL",
        event_type="SIM_SELL", status="CONFIRMED",
        is_live=0, account_type="SIM",
        trade_value_usd_net=60.0,
        exit_reason="HARD_TP_160", exit_reason_label="硬止盈1.6x撤50%",
    )

    original_path = settings.SQLITE_PATH
    settings.SQLITE_PATH = repo.db_path
    try:
        with TestClient(app) as client:
            r = client.get("/api/runtime/trade-events-ledger?account_type=SIM")
            assert r.status_code == 200
            data = r.json()
            items = data.get("items", [])
            assert len(items) == 2
            sides = {i["side"] for i in items}
            assert sides == {"BUY", "SELL"}
            sell = next(i for i in items if i["side"] == "SELL")
            assert sell["exit_reason_code"] == "HARD_TP_160"
    finally:
        settings.SQLITE_PATH = original_path
