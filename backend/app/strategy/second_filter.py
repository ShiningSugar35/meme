"""Second-stage K-line and holder filter.

Top-holder fetching is deliberately kept outside this module by the runner so it
can be called only after cheap K-line prechecks pass.  This function is the final
canonical decision writer used for strategy_match rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any, Dict, List, Optional, Sequence
import math

from .filters import _top1_threshold, normalise_features


@dataclass
class SecondFilterResult:
    passed: bool
    details: Dict[str, Any]
    feature_vector: Dict[str, Any]


@dataclass(frozen=True)
class SecondFilterParams:
    x: float = 0.20
    y: float = 2.25

    @classmethod
    def from_strategy(cls, strategy: Optional[Dict[str, Any]] = None) -> "SecondFilterParams":
        strategy = strategy or {}
        x = _first_number(strategy, ("x", "risk_x", "initial_x", "filter_x"), 0.20)
        y = _first_number(strategy, ("y", "momentum_y", "second_filter_y", "kline_y"), 2.25)
        return cls(x=max(0.0, float(x)), y=max(0.000001, float(y)))


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _first_present(data: Dict[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return default


def _first_number(data: Dict[str, Any], keys: Sequence[str], default: float = 0.0) -> float:
    for key in keys:
        v = _to_float(data.get(key))
        if v is not None:
            return v
    return default


def _candle_value(candle: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
    return _to_float(_first_present(candle or {}, keys))


def _normalise_candles(candles: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not candles:
        return []
    return [c for c in candles if isinstance(c, dict)]


def _completed_5m_sample(snapshot: Dict[str, Any], completed_1m: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candles_5m = _normalise_candles(snapshot.get("completed_5m_candles"))
    if candles_5m:
        return candles_5m[-1:]
    return completed_1m[-5:] if completed_1m else []


def _price_from_latest_or_close(latest: Dict[str, Any], latest_1m: Dict[str, Any]) -> Optional[float]:
    return (
        _to_float(_first_present(latest or {}, ("price_usd", "latest_price_usd", "price")))
        or _candle_value(latest_1m, ("close", "c"))
    )


async def run_second_filter(
    snapshot: Dict[str, Any],
    strategy: Optional[Dict[str, Any]] = None,
    latest: Optional[Dict[str, Any]] = None,
    completed_1m_candles: Optional[List[Dict[str, Any]]] = None,
    buy_sell_1m: Optional[Dict[str, Any]] = None,
) -> SecondFilterResult:
    """Evaluate the final buy gate.

    Strategy predicates implemented here:
    - latest completed 1m candle volume/shape checks;
    - top1 normal-holder concentration check;
    - current price inside the recent 5m range band.
    """
    snapshot = snapshot or {}
    latest = latest or {}
    p = SecondFilterParams.from_strategy(strategy)
    y = p.y
    completed_1m = _normalise_candles(completed_1m_candles or snapshot.get("completed_1m_candles"))

    reasons: List[str] = []
    checks: Dict[str, Any] = {}
    features: Dict[str, Any] = {"x": p.x, "y": p.y}

    if not completed_1m:
        return SecondFilterResult(False, {"reasons": ["no_completed_1m_candle"], "checks": checks}, features)

    latest_1m = completed_1m[-1]
    open_1m = _candle_value(latest_1m, ("open", "o"))
    high_1m = _candle_value(latest_1m, ("high", "h"))
    low_1m = _candle_value(latest_1m, ("low", "l"))
    close_1m = _candle_value(latest_1m, ("close", "c"))
    volume_1m = _candle_value(latest_1m, ("volume_usd", "volume", "v", "usd_volume"))

    features.update({
        "open_1m": open_1m,
        "high_1m": high_1m,
        "low_1m": low_1m,
        "close_1m": close_1m,
        "volume_1m": volume_1m,
    })

    if open_1m is None or high_1m is None or low_1m is None or close_1m is None or volume_1m is None:
        return SecondFilterResult(False, {"reasons": ["incomplete_1m_candle"], "checks": checks}, features)

    range_1m = high_1m - low_1m
    if range_1m <= 0:
        return SecondFilterResult(False, {"reasons": ["zero_1m_range"], "checks": checks}, features)

    prev_volumes = [
        _candle_value(k, ("volume_usd", "volume", "v", "usd_volume"))
        for k in completed_1m[-6:-1]
    ]
    prev_volumes = [v for v in prev_volumes if v is not None and v >= 0]
    median_volume_prev_5m = median(prev_volumes) if prev_volumes else 0.0

    risk_features = normalise_features(snapshot)
    liquidity_usd = (
        _to_float(snapshot.get("liquidity_usd"))
        or _to_float(latest.get("liquidity_usd"))
        or risk_features.get("liquidity_usd")
        or 0.0
    )
    volume_threshold = max(
        liquidity_usd * max(0.0, 0.07 - 0.02 * y),
        median_volume_prev_5m * max(0.0, 1.3 - 0.1 * y),
    )
    checks["volume_1m"] = volume_1m > volume_threshold
    if not checks["volume_1m"]:
        reasons.append("volume_1m")

    close_threshold = open_1m * (1 - 0.002 * y)
    checks["close_1m"] = close_1m > close_threshold
    if not checks["close_1m"]:
        reasons.append("close_1m")

    close_pos_1m = (close_1m - low_1m) / range_1m
    close_pos_threshold = 0.80 - 0.01 * y
    checks["close_position_1m"] = close_pos_1m > close_pos_threshold
    if not checks["close_position_1m"]:
        reasons.append("close_position_1m")

    top1_rate = _to_float(
        snapshot.get("top1_holder_rate")
        if snapshot.get("top1_holder_rate") is not None
        else snapshot.get("top_1_holder_rate")
    )
    top1_threshold = _top1_threshold(strategy or p.x)
    checks["top1_addr_type0_holder_rate"] = top1_rate is not None and top1_rate < top1_threshold
    if not checks["top1_addr_type0_holder_rate"]:
        reasons.append("top1_addr_type0_holder_rate")

    range_sample = _completed_5m_sample(snapshot, completed_1m)
    highs = [_candle_value(k, ("high", "h")) for k in range_sample]
    lows = [_candle_value(k, ("low", "l")) for k in range_sample]
    highs = [v for v in highs if v is not None]
    lows = [v for v in lows if v is not None]
    high_5m = _to_float(snapshot.get("high_5m")) if snapshot.get("high_5m") is not None else (max(highs) if highs else None)
    low_5m = _to_float(snapshot.get("low_5m")) if snapshot.get("low_5m") is not None else (min(lows) if lows else None)
    current_price = _price_from_latest_or_close(latest, latest_1m)

    features.update({
        "liquidity_usd": liquidity_usd,
        "median_volume_prev_5m": median_volume_prev_5m,
        "volume_threshold": volume_threshold,
        "close_threshold": close_threshold,
        "close_position_1m": close_pos_1m,
        "close_position_1m_threshold": close_pos_threshold,
        "top1_holder_rate": top1_rate,
        "top1_holder_threshold": top1_threshold,
        "high_5m": high_5m,
        "low_5m": low_5m,
        "current_price": current_price,
    })

    if high_5m is None or low_5m is None or current_price is None:
        return SecondFilterResult(False, {"reasons": reasons + ["incomplete_5m_range"], "checks": checks}, features)

    range_5m = high_5m - low_5m
    if range_5m <= 0:
        return SecondFilterResult(False, {"reasons": reasons + ["zero_5m_range"], "checks": checks}, features)

    price_pos_5m = (current_price - low_5m) / range_5m
    pos_low = 0.8 - 0.2 * y
    pos_high = 0.35 + 0.2 * y

    checks["current_gt_high_5m_over_y"] = current_price > high_5m / y
    checks["current_lt_low_5m_times_y"] = current_price < low_5m * y
    checks["price_position_5m_range"] = pos_low < price_pos_5m < pos_high

    for key in ("current_gt_high_5m_over_y", "current_lt_low_5m_times_y", "price_position_5m_range"):
        if not checks[key]:
            reasons.append(key)

    features.update({
        "price_position_5m": price_pos_5m,
        "price_position_5m_low": pos_low,
        "price_position_5m_high": pos_high,
        "buy_sell_1m": buy_sell_1m or {},
    })

    return SecondFilterResult(
        passed=not reasons,
        details={"reasons": reasons, "checks": checks},
        feature_vector=features,
    )
