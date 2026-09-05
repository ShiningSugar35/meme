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


def log_positive_ratio(numerator: Any, denominator: Any) -> float | None:
    ratio = ratio_nonnegative(numerator, denominator)
    return math.log(ratio) if ratio is not None and ratio > 0 else None


def price_change_from_history(
    current_price: float,
    klines: Sequence[Kline],
    target_ts: int,
    known_at: int,
) -> float | None:
    """Return price change versus the PIT price at ``target_ts``.

    Kline timestamps are bucket-open times. Prefer the open of the bucket that
    starts at the target timestamp because it is the price at T-60 and the whole
    bucket is already known by entry time T. If that open is unavailable, fall
    back to the latest close ending at or before the target. Gaps wider than one
    minute stay missing rather than silently stretching the lookback.
    """
    exact = [
        line
        for line in klines
        if int(line.timestamp) <= int(target_ts)
        and int(line.timestamp) + 60 <= int(known_at)
        and line.open not in (None, 0)
        and 0 <= int(target_ts) - int(line.timestamp) <= 5
    ]
    if exact:
        anchor = max(exact, key=lambda line: line.timestamp)
        return current_price / float(anchor.open) - 1.0

    completed = [
        line
        for line in klines
        if int(line.timestamp) + 60 <= int(target_ts)
        and line.close not in (None, 0)
        and int(target_ts) - (int(line.timestamp) + 60) <= 65
    ]
    if not completed:
        return None
    anchor = max(completed, key=lambda line: line.timestamp)
    return current_price / float(anchor.close) - 1.0


def build_gmgn_event_features(
    source: Mapping[str, Any],
    *,
    current_price: float,
    entry_time: int,
    history_klines: Sequence[Kline] = (),
    age_minutes: Any = None,
    holder_count: Any = None,
    marketcap: Any = None,
    liquidity: Any = None,
) -> dict[str, Any]:
    """Build non-duplicative Shadow features known no later than entry_time.

    Missing provider facts stay ``None``. No absent event is converted to zero.
    The 1m price-change fallback uses only a completed historical 1m bar.
    """
    volume_1m = first_recursive(source, ("volume_1m", "volume1m", "volume_m1"))
    swaps_1m = first_recursive(source, ("swaps_1m", "swaps1m", "trade_1m", "trades_1m"))
    buys_1m = first_recursive(source, ("buys_1m", "buy_1m", "buy_count_1m"))
    sells_1m = first_recursive(source, ("sells_1m", "sell_1m", "sell_count_1m"))
    buy_volume_1m = first_recursive(source, ("buy_volume_1m", "buyVolume1m", "buy_volume_m1"))
    sell_volume_1m = first_recursive(source, ("sell_volume_1m", "sellVolume1m", "sell_volume_m1"))

    price_change_1m: float | None = None
    old_price_1m = to_float(first_recursive(source, ("price_1m", "price1m", "price_m1")))
    if old_price_1m not in (None, 0):
        price_change_1m = current_price / float(old_price_1m) - 1.0
    elif history_klines:
        price_change_1m = price_change_from_history(
            current_price, history_klines, entry_time - 60, entry_time
        )

    return {
        "price_change_1m": price_change_1m,
        "ln(volume_1m+1)": log1p_nonnegative(volume_1m),
        "buy_count_imbalance_1m": signed_imbalance(buys_1m, sells_1m),
        "buy_volume_imbalance_1m": signed_imbalance(buy_volume_1m, sell_volume_1m),
        "ln(volume_1m/swaps_1m+1)": log_volume_per_swap(volume_1m, swaps_1m),
        "holder_count/age": ratio_nonnegative(
            holder_count if holder_count not in (None, "") else first_recursive(source, ("holder_count", "holders")),
            age_minutes if age_minutes not in (None, "") else _age_minutes(source, entry_time),
        ),
        "ln(marketcap/liquidity)": log_positive_ratio(
            marketcap if marketcap not in (None, "") else first_recursive(source, ("marketcap", "market_cap", "marketCap")),
            liquidity if liquidity not in (None, "") else first_recursive(source, ("liquidity", "liquidity_usd", "pool_liquidity_usd")),
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
