"""Two-hour first-touch label finalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .constants import LabelPolicy
from .errors import CollectorValidationError
from .models import CollectedSample, Kline


@dataclass(frozen=True, slots=True)
class PriceWindowResult:
    address: str
    entry_time: int
    label_version: str
    tag: int
    exit_reason: str
    max_price_ratio: float
    min_price_ratio: float
    final_close_ratio: float
    first_take_profit_at: int | None
    first_stop_loss_at: int | None
    price_change_1h: float | None
    price_change_5m: float | None


def _historical_change(
    entry_price: float,
    klines: Sequence[Kline],
    target_ts: int,
) -> float | None:
    eligible = [line for line in klines if line.timestamp <= target_ts and line.close not in (None, 0)]
    if not eligible:
        return None
    previous = max(eligible, key=lambda line: line.timestamp)
    return entry_price / float(previous.close) - 1.0


class LabelFinalizer:
    def __init__(self, policy: LabelPolicy | None = None) -> None:
        self.policy = policy or LabelPolicy()

    def is_due(self, sample: CollectedSample, now_ts: int) -> bool:
        return now_ts >= sample.entry_time + self.policy.window_seconds

    def finalize(self, sample: CollectedSample, klines: Sequence[Kline]) -> PriceWindowResult:
        if sample.entry_price <= 0:
            raise CollectorValidationError("Entry price must be positive")
        end_ts = sample.entry_time + self.policy.window_seconds
        ordered = sorted(
            (
                line
                for line in klines
                if sample.entry_time <= line.timestamp <= end_ts
            ),
            key=lambda line: line.timestamp,
        )
        if not ordered:
            raise CollectorValidationError("No usable Kline exists inside the 2h label window")

        max_ratio = 0.0
        min_ratio = float("inf")
        final_close: float | None = None
        first_tp: int | None = None
        first_sl: int | None = None
        for line in ordered:
            high = line.high if line.high is not None else line.close
            low = line.low if line.low is not None else line.close
            if high is not None:
                max_ratio = max(max_ratio, high / sample.entry_price)
                if first_tp is None and high >= sample.entry_price * self.policy.take_profit_ratio:
                    first_tp = line.timestamp
            if low is not None:
                min_ratio = min(min_ratio, low / sample.entry_price)
                if first_sl is None and low <= sample.entry_price * self.policy.stop_loss_ratio:
                    first_sl = line.timestamp
            if line.close is not None:
                final_close = line.close

        if max_ratio <= 0 or min_ratio == float("inf") or final_close is None:
            raise CollectorValidationError("Kline window lacks usable high, low or final close")
        close_ratio = final_close / sample.entry_price

        # `<=` intentionally gives the stop loss precedence when both barriers
        # occur in the same one-minute candle.
        if first_sl is not None and (first_tp is None or first_sl <= first_tp):
            tag, reason = 0, "stop_loss_first"
        elif first_tp is not None:
            tag, reason = 1, "take_profit_first"
        else:
            tag, reason = 0, "window_timeout_negative"

        return PriceWindowResult(
            address=sample.address,
            entry_time=sample.entry_time,
            label_version=self.policy.label_version,
            tag=tag,
            exit_reason=reason,
            max_price_ratio=max_ratio,
            min_price_ratio=min_ratio,
            final_close_ratio=close_ratio,
            first_take_profit_at=first_tp,
            first_stop_loss_at=first_sl,
            price_change_1h=_historical_change(
                sample.entry_price,
                klines,
                sample.entry_time - 60 * 60,
            ),
            price_change_5m=_historical_change(
                sample.entry_price,
                klines,
                sample.entry_time - 5 * 60,
            ),
        )

