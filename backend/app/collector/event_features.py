from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .filters import _age_minutes, to_float
from .models import Kline


def first_recursive(value: Any, keys: Sequence[str], depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(value, Mapping):
        for key in keys:
            candidate = value.get(key)
            if candidate not in (None, ""):
                return candidate
        for nested in value.values():
            found = first_recursive(nested, keys, depth + 1)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for nested in value:
            found = first_recursive(nested, keys, depth + 1)
            if found not in (None, ""):
                return found
    return None


def optional_bool01(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if value is True or value in (1, "1"):
        return 1
    if value is False or value in (0, "0"):
        return 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on"}:
            return 1
        if normalized in {"false", "no", "off"}:
            return 0
        if normalized in {"none", "null", "unknown", "n/a"}:
            return None
    return None


def log1p_nonnegative(value: Any) -> float | None:
    number = to_float(value)
    return math.log1p(number) if number is not None and number >= 0 else None


def signed_imbalance(buy: Any, sell: Any) -> float | None:
    buy_value = to_float(buy)
    sell_value = to_float(sell)
    if buy_value is None or sell_value is None or buy_value < 0 or sell_value < 0:
        return None
    total = buy_value + sell_value
    return (buy_value - sell_value) / total if total > 0 else None


def log_volume_per_swap(volume: Any, swaps: Any) -> float | None:
    volume_value = to_float(volume)
    swaps_value = to_float(swaps)
    if volume_value is None or swaps_value is None or volume_value < 0 or swaps_value <= 0:
        return None
    return math.log1p(volume_value / swaps_value)


def ratio_nonnegative(numerator: Any, denominator: Any) -> float | None:
    left = to_float(numerator)
    right = to_float(denominator)
    if left is None or right is None or left < 0 or right <= 0:
        return None
    return left / right


def creator_status01(value: Any) -> int | None:
    """1=creator sold/closed, 0=creator still holds, unknown stays missing."""
    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    if normalized in {"creator_close", "close", "closed", "sell", "sold"}:
        return 1
    if normalized in {"creator_hold", "hold", "holding"}:
        return 0
    return None


def completed_bars(klines: Sequence[Kline], entry_time: int) -> list[Kline]:
    """Return only 1m bars that were complete at the decision timestamp."""
    return sorted(
        (line for line in klines if int(line.timestamp) + 60 <= int(entry_time)),
        key=lambda line: line.timestamp,
    )


def trailing_two_minute_volume(klines: Sequence[Kline], entry_time: int) -> float | None:
    bars = completed_bars(klines, entry_time)
    if len(bars) < 2:
        return None
    latest = bars[-2:]
    # Require adjacent one-minute buckets.  We do not silently stretch a two
    # minute feature over a gap or use a partially completed current bar.
    if int(latest[1].timestamp) - int(latest[0].timestamp) > 65:
        return None
    if any(line.volume is None or float(line.volume) < 0 for line in latest):
        return None
    return float(latest[0].volume or 0.0) + float(latest[1].volume or 0.0)


def price_change_from_history(current_price: float, klines: Sequence[Kline], target_ts: int) -> float | None:
    # Kline timestamps are bucket-open times. Use only a bar whose close was
    # already known at target_ts; selecting the bucket that starts at target_ts
    # would shorten a nominal 2m lookback by almost one minute.
    eligible = [
        line
        for line in klines
        if int(line.timestamp) + 60 <= int(target_ts) and line.close not in (None, 0)
    ]
    if not eligible:
        return None
    previous = max(eligible, key=lambda line: line.timestamp)
    return current_price / float(previous.close) - 1.0


def two_minute_volume_acceleration(klines: Sequence[Kline], entry_time: int) -> float | None:
    """Bounded minute-2 vs minute-1 activity acceleration from completed bars."""
    bars = completed_bars(klines, entry_time)
    if len(bars) < 2:
        return None
    previous, latest = bars[-2:]
    if int(latest.timestamp) - int(previous.timestamp) > 65:
        return None
    if previous.volume is None or latest.volume is None:
        return None
    older = float(previous.volume)
    newer = float(latest.volume)
    if older < 0 or newer < 0 or older + newer <= 0:
        return None
    return (newer - older) / (newer + older)


def build_gmgn_event_features(
    source: Mapping[str, Any],
    *,
    current_price: float,
    entry_time: int,
    history_klines: Sequence[Kline] = (),
    age_minutes: Any = None,
    holder_count: Any = None,
    marketcap: Any = None,
) -> dict[str, Any]:
    """Build non-duplicative event features known no later than entry_time.

    Missing provider facts stay ``None``.  No absent event is converted to zero.
    The optional two-minute volume is constructed only from two completed 1m
    OHLCV buckets, so it cannot reach past the admission/decision timestamp.
    """
    volume_1m = first_recursive(source, ("volume_1m", "volume1m", "volume_m1"))
    swaps_1m = first_recursive(source, ("swaps_1m", "swaps1m", "trade_1m", "trades_1m"))
    buys_1m = first_recursive(source, ("buys_1m", "buy_1m", "buy_count_1m"))
    sells_1m = first_recursive(source, ("sells_1m", "sell_1m", "sell_count_1m"))
    buy_volume_1m = first_recursive(source, ("buy_volume_1m", "buyVolume1m", "buy_volume_m1"))
    sell_volume_1m = first_recursive(source, ("sell_volume_1m", "sellVolume1m", "sell_volume_m1"))
    explicit_2m_volume = first_recursive(source, ("volume_2m", "volume2m", "volume_m2"))
    volume_2m = to_float(explicit_2m_volume)
    if volume_2m is None and history_klines:
        volume_2m = trailing_two_minute_volume(history_klines, entry_time)

    price_change_2m: float | None = None
    old_price_2m = to_float(first_recursive(source, ("price_2m", "price2m", "price_m2")))
    if old_price_2m not in (None, 0):
        price_change_2m = current_price / float(old_price_2m) - 1.0
    elif history_klines:
        price_change_2m = price_change_from_history(current_price, history_klines, entry_time - 120)

    return {
        "price_change_2m": price_change_2m,
        "ln(volume_1m+1)": log1p_nonnegative(volume_1m),
        "ln(swaps_1m+1)": log1p_nonnegative(swaps_1m),
        "buy_count_imbalance_1m": signed_imbalance(buys_1m, sells_1m),
        "buy_volume_imbalance_1m": signed_imbalance(buy_volume_1m, sell_volume_1m),
        "ln(volume_1m/swaps_1m+1)": log_volume_per_swap(volume_1m, swaps_1m),
        "ln(volume_2m+1)": log1p_nonnegative(volume_2m),
        "volume_acceleration_2m": two_minute_volume_acceleration(history_klines, entry_time),
        "holder_count/age": ratio_nonnegative(
            holder_count if holder_count not in (None, "") else first_recursive(source, ("holder_count", "holders")),
            age_minutes if age_minutes not in (None, "") else _age_minutes(source, entry_time),
        ),
        "ln(marketcap+1)": log1p_nonnegative(
            marketcap if marketcap not in (None, "") else first_recursive(source, ("marketcap", "market_cap", "marketCap"))
        ),
        "creator_token_status": creator_status01(
            first_recursive(source, ("creator_token_status", "creatorTokenStatus"))
        ),
        "dexscr_ad": optional_bool01(first_recursive(source, ("dexscr_ad", "dexscreener_ad", "dexscrAd"))),
        "ln(dexscr_boost_fee+1)": log1p_nonnegative(
            first_recursive(source, ("dexscr_boost_fee", "dexscreener_boost_fee", "dexscrBoostFee"))
        ),
        "dexscr_trending_bar": optional_bool01(
            first_recursive(source, ("dexscr_trending_bar", "dexscreener_trending_bar", "dexscrTrendingBar"))
        ),
        "ln(x_user_follower+1)": log1p_nonnegative(
            first_recursive(source, ("x_user_follower", "twitter_follower", "twitter_followers"))
        ),
        "ln(tg_call_count+1)": log1p_nonnegative(
            first_recursive(source, ("tg_call_count", "telegram_call_count", "tgCallCount"))
        ),
    }
