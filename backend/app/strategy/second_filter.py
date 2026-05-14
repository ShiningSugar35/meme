from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
import statistics


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


def _kline_open(k: Dict[str, Any]) -> Optional[float]:
    return _to_float(_first_present(k, ["open", "o", "price_open"]))


def _kline_close(k: Dict[str, Any]) -> Optional[float]:
    return _to_float(_first_present(k, ["close", "c", "price", "price_usd"]))


def _kline_high(k: Dict[str, Any]) -> Optional[float]:
    high = _to_float(_first_present(k, ["high", "h"]))
    return high if high is not None else _kline_close(k)


def _kline_low(k: Dict[str, Any]) -> Optional[float]:
    low = _to_float(_first_present(k, ["low", "l"]))
    return low if low is not None else _kline_close(k)


def _kline_volume_usd(k: Dict[str, Any]) -> Optional[float]:
    return _to_float(_first_present(k, ["volume_usd", "volume", "v"]))


def _current_price(latest_price: Dict[str, Any], token: Dict[str, Any]) -> Optional[float]:
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

    # --- data from latest completed 1m candle ---
    latest_1m: Dict[str, Any] = klines[-1] if klines else {}
    open_1m = _kline_open(latest_1m)
    close_1m = _kline_close(latest_1m)
    high_1m = _kline_high(latest_1m)
    low_1m = _kline_low(latest_1m)
    volume_1m = _kline_volume_usd(latest_1m) or 0.0

    # --- rule 1: volume_1m > max(liquidity_usd*(0.07-0.02*y), median_volume_prev_5m*(1.3-0.1*y)) ---
    liquidity_usd = _to_float(_first_present(token, ["liquidity_usd", "liquidity"]))
    prev_volumes: List[float] = []
    for k in klines[:-1]:
        v = _kline_volume_usd(k)
        if v is not None and v > 0:
            prev_volumes.append(v)
    median_prev = statistics.median(prev_volumes) if prev_volumes else 0.0
    threshold_a = (liquidity_usd or 0.0) * (0.07 - 0.02 * y)
    threshold_b = median_prev * (1.3 - 0.1 * y)
    threshold_1 = max(threshold_a, threshold_b)
    cond1 = volume_1m > threshold_1
    details.append({
        "rule": "volume_1m",
        "passed": cond1,
        "volume_1m": volume_1m,
        "liquidity_usd": liquidity_usd,
        "threshold_liquidity": threshold_a,
        "median_volume_prev": median_prev,
        "threshold_median": threshold_b,
        "threshold": threshold_1,
        "y": y,
    })

    # --- rule 2: close_1m > open_1m * (1 - 0.002 * y) ---
    if open_1m and open_1m > 0 and close_1m:
        threshold_2 = open_1m * (1.0 - 0.002 * y)
        cond2 = close_1m > threshold_2
    else:
        cond2 = False
        threshold_2 = None
    details.append({
        "rule": "close_gt_open_scaled",
        "passed": cond2,
        "open_1m": open_1m,
        "close_1m": close_1m,
        "threshold": threshold_2,
        "y": y,
    })

    # --- rule 3: (close_1m - low_1m) / (high_1m - low_1m) > (0.80 - 0.01 * y) ---
    if high_1m and low_1m and close_1m and high_1m > low_1m:
        candle_ratio = (close_1m - low_1m) / (high_1m - low_1m)
        threshold_3 = 0.80 - 0.01 * y
        cond3 = candle_ratio > threshold_3
    else:
        candle_ratio = None
        threshold_3 = None
        cond3 = False
    details.append({
        "rule": "candle_position",
        "passed": cond3,
        "candle_ratio": candle_ratio,
        "threshold": threshold_3,
        "open_1m": open_1m,
        "close_1m": close_1m,
        "high_1m": high_1m,
        "low_1m": low_1m,
        "y": y,
    })

    # --- rule 4: current_price > high_5m / y ---
    high_over_y = high_5m / y
    cond4 = current_price > high_over_y
    details.append({
        "rule": "price_gt_high_over_y",
        "passed": cond4,
        "current": current_price,
        "high_5m": high_5m,
        "threshold_value": high_over_y,
        "y": y,
    })

    # --- rule 5: current_price < low_5m * y ---
    low_times_y = low_5m * y
    cond5 = current_price < low_times_y
    details.append({
        "rule": "price_lt_low_times_y",
        "passed": cond5,
        "current": current_price,
        "low_5m": low_5m,
        "threshold_value": low_times_y,
        "y": y,
    })

    # --- rule 6: 0.8-0.2*y < frac < 0.35+0.2*y ---
    frac = (current_price - low_5m) / (high_5m - low_5m)
    low_frac = 0.8 - 0.2 * y
    high_frac = 0.35 + 0.2 * y
    cond6 = low_frac < frac < high_frac
    details.append({
        "rule": "fraction_range",
        "passed": cond6,
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
        "volume_1m": volume_1m,
        "liquidity_usd": liquidity_usd,
        "median_volume_prev": median_prev,
        "open_1m": open_1m,
        "close_1m": close_1m,
        "high_1m": high_1m,
        "low_1m": low_1m,
        "kline_count_used": kline_count,
    }
    return SecondFilterResult(passed, details, feature_vector)
