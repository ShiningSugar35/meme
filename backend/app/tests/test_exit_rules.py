import asyncio
from datetime import datetime, timedelta, timezone
from app.strategy.exit_rules import decide_exit, normalize_pct_change


def test_exit_small_remaining_forces_full():
    position = {"entry_price_sol": 1.0, "remaining_value_usd": 5}
    tick = {"price_sol": 1.0}
    rolling = {"low": 0.5, "high": 1.5}
    res = asyncio.run(decide_exit(position, tick, rolling, {}))
    assert res.should_exit is True
    assert res.exit_pct == 1.0


class TestHardStopLoss75:
    """New: <0.75x → full exit (HARD_SL_75)."""

    def test_sl_075_full_exit(self):
        position = {"entry_price_sol": 1.0, "remaining_value_usd": 100}
        tick = {"price_sol": 0.74}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert any(r.name == "HARD_SL_75" and r.desired_exit_pct == 1.0 for r in res.reasons)

    def test_sl_075_not_at_075(self):
        position = {"entry_price_sol": 1.0, "remaining_value_usd": 100}
        tick = {"price_sol": 0.75}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert not any(r.name == "HARD_SL_75" for r in res.reasons)

    def test_sl_075_not_triggered_at_08(self):
        position = {"entry_price_sol": 1.0, "remaining_value_usd": 100}
        tick = {"price_sol": 0.8}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert not any(r.name == "HARD_SL_75" for r in res.reasons)


class TestHardTP160:
    """>1.6x first hit → 50% exit (HARD_TP_160)."""

    def test_tp_160_half_exit(self):
        position = {"entry_price_sol": 1.0, "remaining_value_usd": 100}
        tick = {"price_sol": 1.61}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert any(r.name == "HARD_TP_160" and r.desired_exit_pct == 0.5 for r in res.reasons)

    def test_tp_160_not_at_16(self):
        position = {"entry_price_sol": 1.0, "remaining_value_usd": 100}
        tick = {"price_sol": 1.6}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert not any(r.name == "HARD_TP_160" for r in res.reasons)


class TestHardTP210:
    """>2.1x → full exit (HARD_TP_210)."""

    def test_tp_210_full_exit(self):
        position = {"entry_price_sol": 1.0, "remaining_value_usd": 100}
        tick = {"price_sol": 2.11}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert any(r.name == "HARD_TP_210" and r.desired_exit_pct == 1.0 for r in res.reasons)

    def test_tp_210_not_at_21(self):
        position = {"entry_price_sol": 1.0, "remaining_value_usd": 100}
        tick = {"price_sol": 2.1}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert not any(r.name == "HARD_TP_210" for r in res.reasons)

    def test_tp_210_trumps_160(self):
        """At 2.2x, should get both HARD_TP_160 (50%) and HARD_TP_210 (full); full dominates."""
        position = {"entry_price_sol": 1.0, "remaining_value_usd": 100}
        tick = {"price_sol": 2.2}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert res.exit_pct == 1.0
        assert any(r.name == "HARD_TP_210" for r in res.reasons)

    def test_no_old_code_250(self):
        """At 2.6x, should trigger HARD_TP_210, NOT HARD_TP_250."""
        position = {"entry_price_sol": 1.0, "remaining_value_usd": 100}
        tick = {"price_sol": 2.6}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert not any(r.name == "HARD_TP_250" for r in res.reasons)
        assert any(r.name == "HARD_TP_210" for r in res.reasons)


class TestHardTP160Retrace:
    """After HARD_TP_160 executed, price retraces to <1.5x → full exit."""

    def test_retrace_full_exit(self):
        position = {
            "entry_price_sol": 1.0,
            "remaining_value_usd": 100,
            "executed_exit_rules_json": '["HARD_TP_160"]',
        }
        tick = {"price_sol": 1.49}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert any(r.name == "HARD_TP_160_RETRACE" and r.desired_exit_pct == 1.0 for r in res.reasons)

    def test_retrace_not_without_first_tp(self):
        position = {"entry_price_sol": 1.0, "remaining_value_usd": 100}
        tick = {"price_sol": 1.49}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert not any(r.name == "HARD_TP_160_RETRACE" for r in res.reasons)

    def test_retrace_not_below_16(self):
        """At 1.55x, not yet retraced."""
        position = {
            "entry_price_sol": 1.0,
            "remaining_value_usd": 100,
            "executed_exit_rules_json": '["HARD_TP_160"]',
        }
        tick = {"price_sol": 1.55}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert not any(r.name == "HARD_TP_160_RETRACE" for r in res.reasons)


class TestCompleted:
    def test_completed_full_exit(self):
        position = {"entry_price_sol": 1.0, "remaining_value_usd": 100, "type": "completed"}
        tick = {"price_sol": 1.0}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert any(r.name == "COMPLETED" for r in res.reasons)


class TestDustForceExit:
    def test_dust_exit(self):
        position = {"entry_price_sol": 1.0, "remaining_value_usd": 5}
        tick = {"price_sol": 1.0}
        res = asyncio.run(decide_exit(position, tick, {}, {}))
        assert any(r.name == "DUST_FORCE_EXIT" for r in res.reasons)


class TestNormalizePctChange:
    def test_percentage_divided(self):
        assert normalize_pct_change(1.2) == 0.012

    def test_decimal_kept(self):
        assert normalize_pct_change(0.05) == 0.05

    def test_large_percentage(self):
        assert normalize_pct_change(120) == 1.2

    def test_none(self):
        assert normalize_pct_change(None) is None

    def test_negative(self):
        assert normalize_pct_change(-3.5) == -0.035

    def test_negative_decimal(self):
        assert normalize_pct_change(-0.01) == -0.01
