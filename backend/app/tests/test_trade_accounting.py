import pytest


@pytest.mark.asyncio
async def test_trade_event_buy_sign_negative(repo):
    te = await repo.append_trade_event(
        "BUY_SIGN_TEST", token_mint="MINT1", side="BUY",
        event_type="SIM_BUY", status="CONFIRMED",
        is_live=0, account_type="SIM",
        trade_value_usd_net=-100.0,
    )
    assert te["trade_value_usd_net"] == -100.0
    assert float(te["trade_value_usd_net"]) < 0


@pytest.mark.asyncio
async def test_trade_event_sell_sign_positive(repo):
    te = await repo.append_trade_event(
        "SELL_SIGN_TEST", token_mint="MINT1", side="SELL",
        event_type="SIM_SELL", status="CONFIRMED",
        is_live=0, account_type="SIM",
        trade_value_usd_net=150.0,
    )
    assert te["trade_value_usd_net"] == 150.0
    assert float(te["trade_value_usd_net"]) > 0


@pytest.mark.asyncio
async def test_trade_event_exit_reason_stored(repo):
    te = await repo.append_trade_event(
        "EXIT_REASON_TEST", token_mint="MINT1", side="SELL",
        event_type="SIM_SELL", status="CONFIRMED",
        is_live=0, account_type="SIM",
        exit_reason="HARD_SL_45", exit_reason_label="硬止损45%全平",
        trade_value_usd_net=50.0,
    )
    assert te["exit_reason"] == "HARD_SL_45"
    assert te["exit_reason_label"] == "硬止损45%全平"


@pytest.mark.asyncio
async def test_trade_events_ledger_returns_individual_events(repo):
    await repo.append_trade_event(
        "LEDGER_BUY", token_mint="MINT1", side="BUY",
        event_type="SIM_BUY", status="CONFIRMED",
        is_live=0, account_type="SIM",
        trade_value_usd_net=-100.0,
    )
    await repo.append_trade_event(
        "LEDGER_SELL1", token_mint="MINT1", side="SELL",
        event_type="SIM_SELL", status="CONFIRMED",
        is_live=0, account_type="SIM",
        trade_value_usd_net=60.0,
        exit_reason="TP_160", exit_reason_label="止盈160%",
    )
    await repo.append_trade_event(
        "LEDGER_SELL2", token_mint="MINT1", side="SELL",
        event_type="SIM_SELL", status="CONFIRMED",
        is_live=0, account_type="SIM",
        trade_value_usd_net=40.0,
    )
    async with repo.db.execute(
        "SELECT side, trade_value_usd_net, exit_reason, exit_reason_label FROM trade_events WHERE account_type='SIM' ORDER BY id ASC"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 3
    buys = [r for r in rows if r["side"] == "BUY"]
    sells = [r for r in rows if r["side"] == "SELL"]
    assert len(buys) == 1
    assert len(sells) == 2
    assert sells[0]["exit_reason"] is not None


@pytest.mark.asyncio
async def test_pnl_summary_from_trade_events(repo):
    """PnL summary computed from trade_events should match the expected totals."""
    await repo.append_trade_event(
        "PNL_BUY1", token_mint="MINT1", side="BUY",
        event_type="SIM_BUY", status="CONFIRMED",
        is_live=0, account_type="SIM",
        trade_value_usd_net=-200.0,
    )
    await repo.append_trade_event(
        "PNL_SELL1", token_mint="MINT1", side="SELL",
        event_type="SIM_SELL", status="CONFIRMED",
        is_live=0, account_type="SIM",
        trade_value_usd_net=250.0,
    )
    await repo.append_trade_event(
        "PNL_BUY2", token_mint="MINT2", side="BUY",
        event_type="LIVE_BUY_CONFIRMED", status="CONFIRMED",
        is_live=1, account_type="LIVE",
        trade_value_usd_net=-300.0,
    )
    await repo.append_trade_event(
        "PNL_SELL2", token_mint="MINT2", side="SELL",
        event_type="LIVE_SELL_CONFIRMED", status="CONFIRMED",
        is_live=1, account_type="LIVE",
        trade_value_usd_net=270.0,
    )

    async with repo.db.execute(
        "SELECT account_type, side, trade_value_usd_net FROM trade_events WHERE status='CONFIRMED' AND side IN ('BUY','SELL')"
    ) as cur:
        rows = await cur.fetchall()
    sim_realized = 0.0
    live_realized = 0.0
    for row in rows:
        val = float(row["trade_value_usd_net"] or 0)
        side = str(row["side"])
        signed = -abs(val) if side == "BUY" else abs(val)
        if str(row["account_type"]) == "LIVE":
            live_realized += signed
        else:
            sim_realized += signed

    assert sim_realized == pytest.approx(50.0)
    assert live_realized == pytest.approx(-30.0)


@pytest.mark.asyncio
async def test_trade_events_charge_and_settle(repo):
    """Full lifecycle: BUY then multiple partial SELLs produce correct net PnL."""
    await repo.append_trade_event(
        "CYCLE_BUY", token_mint="MINT1", side="BUY",
        event_type="SIM_BUY", status="CONFIRMED",
        is_live=0, account_type="SIM",
        trade_value_usd_net=-100.0,
    )
    await repo.append_trade_event(
        "CYCLE_SELL1", token_mint="MINT1", side="SELL",
        event_type="SIM_SELL", status="CONFIRMED",
        is_live=0, account_type="SIM",
        trade_value_usd_net=30.0,
        exit_reason="PARTIAL_TP", exit_reason_label="部分止盈",
    )
    await repo.append_trade_event(
        "CYCLE_SELL2", token_mint="MINT1", side="SELL",
        event_type="SIM_SELL", status="CONFIRMED",
        is_live=0, account_type="SIM",
        trade_value_usd_net=80.0,
        exit_reason="FULL_EXIT", exit_reason_label="全部平仓",
    )

    async with repo.db.execute(
        "SELECT trade_value_usd_net, side, exit_reason, exit_reason_label FROM trade_events WHERE token_mint='MINT1' ORDER BY id ASC"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 3
    net = sum(float(r["trade_value_usd_net"]) for r in rows)
    assert net == pytest.approx(10.0)

