import pytest


@pytest.mark.asyncio
async def test_sim_sell_full_exit(pipeline_factory):
    pipeline, _ = pipeline_factory()
    repo = pipeline.repo

    pos_id = await repo.create_position(
        token_mint="PASS1",
        is_live=False,
        locked_strategy_config_json='{"id": 1}',
        status="POSITION_OPEN",
        entry_price_usd=1.0,
        entry_token_amount=100.0,
        remaining_token_amount=100.0,
        remaining_value_usd=100.0,
        account_type="SIM",
    )

    position = await repo.get_position(pos_id)
    result = await pipeline._execute_sim_paper_sell(position, 1.0, "HARD_TP_160")

    assert result["ok"] is True

    updated = await repo.get_position(pos_id)
    assert updated["status"] == "CLOSED"

    trade_events = await repo.list_trade_events()
    sell_events = [te for te in trade_events if te["side"] == "SELL"]
    assert len(sell_events) == 1
    assert sell_events[0]["status"] == "CONFIRMED"
    assert float(sell_events[0]["gross_value_usd"]) > 0


@pytest.mark.asyncio
async def test_sim_sell_partial_exit(pipeline_factory):
    pipeline, _ = pipeline_factory()
    repo = pipeline.repo

    pos_id = await repo.create_position(
        token_mint="PASS1",
        is_live=False,
        locked_strategy_config_json='{"id": 1}',
        status="POSITION_OPEN",
        entry_price_usd=1.0,
        entry_token_amount=100.0,
        remaining_token_amount=100.0,
        remaining_value_usd=100.0,
        account_type="SIM",
    )

    position = await repo.get_position(pos_id)
    result = await pipeline._execute_sim_paper_sell(position, 0.5, "PARTIAL_TP")

    assert result["ok"] is True

    updated = await repo.get_position(pos_id)
    assert updated["status"] == "POSITION_OPEN"
    assert float(updated["remaining_token_amount"]) == pytest.approx(50.0, rel=0.01)


@pytest.mark.asyncio
async def test_sim_sell_zero_remaining(pipeline_factory):
    pipeline, _ = pipeline_factory()
    repo = pipeline.repo

    pos_id = await repo.create_position(
        token_mint="PASS1",
        is_live=False,
        locked_strategy_config_json='{"id": 1}',
        status="POSITION_OPEN",
        entry_price_usd=1.0,
        entry_token_amount=100.0,
        remaining_token_amount=0.0,
        remaining_value_usd=0.0,
        account_type="SIM",
    )

    position = await repo.get_position(pos_id)
    result = await pipeline._execute_sim_paper_sell(position, 0.5, "EXIT")

    assert result["ok"] is False
    assert result["error"] == "ZERO_REMAINING"


@pytest.mark.asyncio
async def test_sim_sell_zero_pct(pipeline_factory):
    pipeline, _ = pipeline_factory()
    repo = pipeline.repo

    pos_id = await repo.create_position(
        token_mint="PASS1",
        is_live=False,
        locked_strategy_config_json='{"id": 1}',
        status="POSITION_OPEN",
        entry_price_usd=1.0,
        entry_token_amount=100.0,
        remaining_token_amount=100.0,
        remaining_value_usd=100.0,
        account_type="SIM",
    )

    position = await repo.get_position(pos_id)
    # Use a negative pct to trigger ZERO_EXIT_PCT — exit_pct=0 is
    # coerced to 1.0 by the `or 1.0` fallback in _execute_sim_paper_sell.
    result = await pipeline._execute_sim_paper_sell(position, -1.0, "EXIT")

    assert result["ok"] is False
    assert result["error"] == "ZERO_EXIT_PCT"
