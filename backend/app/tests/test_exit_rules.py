import asyncio
from datetime import datetime, timedelta, timezone
from ..strategy.exit_rules import decide_exit


def test_exit_small_remaining_forces_full():
    position = {"entry_price_sol": 1.0, "remaining_value_usd": 5}
    tick = {"price_sol": 1.0}
    rolling = {"low": 0.5, "high": 1.5}
    res = asyncio.run(decide_exit(position, tick, rolling, {}))
    assert res.should_exit is True
    assert res.exit_pct == 1.0


def test_exit_hard_tp():
    position = {"entry_price_sol": 1.0, "remaining_value_usd": 100}
    tick = {"price_sol": 3.2}
    rolling = {"low": 0.5, "high": 1.5}
    res = asyncio.run(decide_exit(position, tick, rolling, {}))
    assert res.should_exit is True
    assert res.exit_pct == 1.0


def test_exit_hard_levels_and_completed():
    position = {"entry_price_sol": 1.0, "remaining_value_usd": 100, "last_fill_at": (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(), "last_fill_price_usd": 1.0}
    # hard tp 1.7
    tick = {"price_sol": 1.85}
    rolling = {"low": 0.5, "high": 2.0}
    res = asyncio.run(decide_exit(position, tick, rolling, {}))
    assert any(r.name.startswith("HARD_TP") for r in res.reasons)

    # hard sl 0.6
    tick2 = {"price_sol": 0.55}
    res2 = asyncio.run(decide_exit(position, tick2, rolling, {}))
    assert any(r.name.startswith("HARD_SL") for r in res2.reasons)

    # remaining value dust
    pos4 = {"entry_price_sol": 1.0, "remaining_value_usd": 5}
    tick4 = {"price_sol": 1.0}
    res4 = asyncio.run(decide_exit(pos4, tick4, rolling, {}))
    assert any(r.name == "DUST_FORCE_EXIT" for r in res4.reasons)

    # completed
    pos5 = {"entry_price_sol": 1.0, "remaining_value_usd": 100, "type": "completed"}
    res5 = asyncio.run(decide_exit(pos5, tick4, rolling, {}))
    assert any(r.name == "COMPLETED" for r in res5.reasons)
