from __future__ import annotations

from backend.app.collector.labels import LabelFinalizer
from backend.app.collector.models import CollectedSample, Kline

ENTRY = 1_700_000_000


def sample() -> CollectedSample:
    return CollectedSample("x", "new_creation", ENTRY, 100.0, "Pump.fun", 10_000, {})


def test_stop_loss_before_take_profit_is_tag_zero() -> None:
    result = LabelFinalizer().finalize(sample(), [
        Kline(ENTRY - 3_600, 80, 80, 80),
        Kline(ENTRY + 60, 110, 89, 95),
        Kline(ENTRY + 120, 181, 95, 150),
    ])
    assert result.tag == 0
    assert result.exit_reason == "stop_loss_first"


def test_take_profit_before_later_stop_is_tag_one() -> None:
    result = LabelFinalizer().finalize(sample(), [
        Kline(ENTRY + 60, 181, 99, 150),
        Kline(ENTRY + 120, 140, 89, 90),
    ])
    assert result.tag == 1
    assert result.exit_reason == "take_profit_first"


def test_same_minute_conflict_is_conservatively_tag_zero() -> None:
    result = LabelFinalizer().finalize(sample(), [Kline(ENTRY + 60, 181, 89, 120)])
    assert result.tag == 0
    assert result.first_stop_loss_at == result.first_take_profit_at


def test_no_barrier_close_above_1_2_is_still_negative() -> None:
    result = LabelFinalizer().finalize(sample(), [
        Kline(ENTRY + 60, 130, 95, 121),
        Kline(ENTRY + 5_400, 130, 95, 120.01),
    ])
    assert result.tag == 0
    assert result.exit_reason == "window_timeout_negative"
    assert result.final_close_ratio > 1.2


def test_close_exactly_1_2_is_negative() -> None:
    result = LabelFinalizer().finalize(sample(), [Kline(ENTRY + 5_400, 130, 95, 120)])
    assert result.tag == 0
    assert result.exit_reason == "window_timeout_negative"


def test_outside_window_does_not_leak_into_label_and_history_is_pre_entry() -> None:
    result = LabelFinalizer().finalize(sample(), [
        Kline(ENTRY - 3_600, 50, 50, 50),
        Kline(ENTRY, 105, 95, 100),
        Kline(ENTRY + 5_400, 130, 95, 121),
        Kline(ENTRY + 5_401, 200, 50, 200),
    ])
    assert result.tag == 0
    assert result.max_price_ratio == 1.3
    assert result.price_change_1h == 1.0
