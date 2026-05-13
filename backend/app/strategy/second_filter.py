from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class SecondFilterResult:
    passed: bool
    details: List[Dict[str, Any]]
    feature_vector: Dict[str, Any]


def _first_present(data: Dict[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return default


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _kline_close(k: Dict[str, Any]) -> Optional[float]:
    return _to_float(_first_present(k, ["close", "c", "price", "price_usd"]))


def _kline_high(k: Dict[str, Any]) -> Optional[float]:
    high = _to_float(_first_present(k, ["high", "h"]))
    return high if high is not None else _kline_close(k)


def _kline_low(k: Dict[str, Any]) -> Optional[float]:
    low = _to_float(_first_present(k, ["low", "l"]))
    return low if low is not None else _kline_close(k)


def _current_price(latest_price: Dict[str, Any], token: Dict[str, Any]) -> Optional[float]:
    # Provider code in this project sometimes uses price, sometimes price_usd/latest_price_usd.
    return _to_float(
        _first_present(
            latest_price,
            ["price", "price_usd", "latest_price_usd", "close", "c"],
            _first_present(token, ["price", "price_usd", "latest_price_usd"]),
        )
    )


def _extract_5m_range(klines: List[Dict[str, Any]], current: Optional[float]) -> tuple[Optional[float], Optional[float], int]:
    highs: List[float] = []
    lows: List[float] = []

    # Caller should pass open-to-now or at least the latest five 1m candles.
    # We defensively keep only the last five items if more are passed.
    recent = klines[-5:] if len(klines) > 5 else klines
    for k in recent:
        h = _kline_high(k)
        l = _kline_low(k)
        if h is not None:
            highs.append(h)
        if l is not None:
            lows.append(l)

    if current is not None:
        highs.append(current)
        lows.append(current)

    if not highs or not lows:
        return None, None, 0
    return max(highs), min(lows), min(len(recent), 5)


async def run_second_filter(
    token: Dict[str, Any],
    strategy_group: Dict[str, Any],
    latest_price: Dict[str, Any],
    klines: List[Dict[str, Any]],
    buy_sell_1m: Dict[str, float],
) -> SecondFilterResult:
    """Second-stage momentum/price-position filter.

    Rules implemented:
    - past 1m buy volume > sell volume * (1.2 - y * 0.05)
    - current price > past/open-to-now 5m high / y
    - current price < past/open-to-now 5m low * y
    - current price percentile in 5m range is within the strategy y-dependent band
    """
    details: List[Dict[str, Any]] = []
    y = float(strategy_group.get("y", 2.25))

    current_price = _current_price(latest_price, token)
    if current_price is None:
        details.append({"rule": "latest_price_present", "passed": False, "reason": "missing latest price"})
        return SecondFilterResult(False, details, {})

    high_5m, low_5m, kline_count = _extract_5m_range(klines, current_price)
    if high_5m is None or low_5m is None:
        details.append({
            "rule": "kline_price_range_present",
            "passed": False,
            "actual": {"kline_count": len(klines)},
            "reason": "need at least one usable kline price or current price",
        })
        return SecondFilterResult(False, details, {"current_price": current_price})

    if high_5m <= low_5m:
        details.append({
            "rule": "high_gt_low",
            "passed": False,
            "actual": {"high_5m": high_5m, "low_5m": low_5m},
            "reason": "cannot compute price range fraction when high <= low",
        })
        return SecondFilterResult(False, details, {
            "current_price": current_price,
            "high_5m": high_5m,
            "low_5m": low_5m,
            "kline_count_used": kline_count,
        })

    buy_v = _to_float(buy_sell_1m.get("buy_volume")) or 0.0
    sell_v = _to_float(buy_sell_1m.get("sell_volume")) or 0.0
    buy_sell_threshold = sell_v * (1.2 - y * 0.05)
    cond1 = buy_v > buy_sell_threshold
    details.append({
        "rule": "buy_vs_sell_1m",
        "passed": cond1,
        "buy": buy_v,
        "sell": sell_v,
        "threshold_expression": "sell_volume_1m * (1.2 - y * 0.05)",
        "threshold_value": buy_sell_threshold,
        "y": y,
    })

    high_over_y = high_5m / y
    cond2 = current_price > high_over_y
    details.append({
        "rule": "price_gt_high_over_y",
        "passed": cond2,
        "current": current_price,
        "high_5m": high_5m,
        "threshold_value": high_over_y,
        "y": y,
    })

    low_times_y = low_5m * y
    cond3 = current_price < low_times_y
    details.append({
        "rule": "price_lt_low_times_y",
        "passed": cond3,
        "current": current_price,
        "low_5m": low_5m,
        "threshold_value": low_times_y,
        "y": y,
    })

    frac = (current_price - low_5m) / (high_5m - low_5m)
    low_frac = 0.8 - 0.2 * y
    high_frac = 0.35 + 0.2 * y
    cond4 = low_frac < frac < high_frac
    details.append({
        "rule": "fraction_range",
        "passed": cond4,
        "frac": frac,
        "range": [low_frac, high_frac],
        "current": current_price,
        "high_5m": high_5m,
        "low_5m": low_5m,
        "y": y,
    })

    passed = all(d.get("passed") for d in details)
    feature_vector = {
        "y": y,
        "current_price": current_price,
        "high_5m": high_5m,
        "low_5m": low_5m,
        "frac": frac,
        "buy_volume_1m": buy_v,
        "sell_volume_1m": sell_v,
        "kline_count_used": kline_count,
    }
    return SecondFilterResult(passed, details, feature_vector)
