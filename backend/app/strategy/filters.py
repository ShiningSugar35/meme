"""Risk + price filter rules for GMGN trench candidates.

The discovery timing parameters ``min_created`` and ``max_created`` are used
by the provider query to ask GMGN for pools whose age is in [min_created,
max_created] seconds.  After the risk screen, kline data is fetched so the
price screen can run in the same cycle.

Risk screen — minimum liquidity is a function of min_created:

    min_liquidity_usd = 5000 + 4 * min_created

Price screen — y-scaling rules on 1m/5m candles and current price.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _first_present(d: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _to_int_bool(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if int(v) != 0 else 0
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "renounced", "locked", "burn", "burned"}:
        return 1
    if s in {"0", "false", "no", "n", "none", "null", "open", "not_renounced"}:
        return 0
    return None


def _norm_str(v: Any) -> str:
    return str(v or "").strip()


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class FilterDetail:
    name: str
    passed: bool
    value: Any
    threshold: Any
    reason: str = ""
    missing: bool = False


@dataclass
class FilterResult:
    passed: bool
    details: List[FilterDetail]
    feature_vector: Dict[str, Any]


@dataclass
class PriceFilterResult:
    passed: bool
    details: List[Dict[str, Any]]
    feature_vector: Dict[str, Any]


# ---------------------------------------------------------------------------
# Kline helpers (for price filter)
# ---------------------------------------------------------------------------

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


def _kline_time(kline: Dict[str, Any]) -> str:
    return str(_first_present(kline, ["open_time", "time", "timestamp", "t"], default=""))


def sort_klines(klines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(klines, key=_kline_time)


def extract_buy_sell_1m(klines: List[Dict[str, Any]], snapshot: Dict[str, Any]) -> Dict[str, float]:
    latest_kline = sort_klines(klines)[-1] if klines else {}
    buy = _to_float(_first_present(latest_kline, ["buy_volume", "buyVolume", "buy_vol", "buyVol"]))
    sell = _to_float(_first_present(latest_kline, ["sell_volume", "sellVolume", "sell_vol", "sellVol"]))
    if buy is None:
        buy = _to_float(_first_present(snapshot, ["buy_volume_1m", "buy_1m", "buy_volume"]))
    if sell is None:
        sell = _to_float(_first_present(snapshot, ["sell_volume_1m", "sell_1m", "sell_volume"]))
    return {"buy_volume": float(buy or 0.0), "sell_volume": float(sell or 0.0)}


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


# ---------------------------------------------------------------------------
# Risk-filter helpers
# ---------------------------------------------------------------------------

def _mk_pass(name: str, value: Any, reason: str, threshold: Any) -> FilterDetail:
    return FilterDetail(name=name, passed=True, value=value, threshold=threshold, reason=reason)


def _mk_failed(name: str, value: Any, reason: str, threshold: Any, missing: bool = False) -> FilterDetail:
    return FilterDetail(name=name, passed=False, value=value, threshold=threshold, reason=reason, missing=missing)


def _strategy_x(strategy_group: Dict[str, Any]) -> float:
    return float(strategy_group.get("x", 0.2))


def _strategy_min_created(strategy_group: Dict[str, Any]) -> int:
    raw = _first_present(strategy_group, ["min_created", "t_seconds", "t", "age_seconds"], 180)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 180


PLATFORMS = {
    "Pump.fun", "PumpFun", "pump", "pump_fun", "pumpfun",
    "Moonshot", "moonshot", "moonshot_app",
    "letsbonk", "LetsBonk",
    "memoo", "Memeoo",
    "token_mill", "Token Mill",
    "jup_studio", "Jup Studio",
    "bags", "BAGS",
    "believe", "Believe",
    "heaven", "Heaven",
}

BURN_VALUES = {"burn", "burned", "burnt", "true", "1", "yes"}
CREATOR_CLOSE_VALUES = {"creator_close", "close", "closed", "creator_closed"}


def _check_float(details, snapshot, name, keys, predicate, threshold_desc, required=True):
    raw = _first_present(snapshot, keys)
    value = _to_float(raw)
    if value is None:
        if required:
            details.append(_mk_failed(name, raw, "missing numeric field", threshold_desc, missing=True))
        else:
            details.append(_mk_pass(name, raw, "optional field missing; treated as pass", threshold_desc))
        return None
    try:
        ok = bool(predicate(value))
    except Exception as e:
        details.append(_mk_failed(name, value, f"predicate error: {e}", threshold_desc))
        return value
    details.append(
        _mk_pass(name, value, f"satisfies {threshold_desc}", threshold_desc)
        if ok
        else _mk_failed(name, value, f"violates {threshold_desc}", threshold_desc)
    )
    return value


def _check_bool_one(details, snapshot, name, keys, required=True):
    raw = _first_present(snapshot, keys)
    value = _to_int_bool(raw)
    if value is None:
        if required:
            details.append(_mk_failed(name, raw, "missing boolean field", 1, missing=True))
        else:
            details.append(_mk_pass(name, raw, "optional field missing; treated as pass", 1))
        return value
    details.append(_mk_pass(name, value, "equals 1", 1) if value == 1 else _mk_failed(name, value, "must equal 1", 1))
    return value


def _check_bool_zero(details, snapshot, name, keys, required=True):
    raw = _first_present(snapshot, keys)
    value = _to_int_bool(raw)
    if value is None:
        if required:
            details.append(_mk_failed(name, raw, "missing boolean field", 0, missing=True))
        else:
            details.append(_mk_pass(name, raw, "optional field missing; treated as pass", 0))
        return value
    details.append(_mk_pass(name, value, "equals 0", 0) if value == 0 else _mk_failed(name, value, "must equal 0", 0))
    return value


# ---------------------------------------------------------------------------
# Risk filter (core rules)
# ---------------------------------------------------------------------------

def _evaluate_core_risk_rules(
    snapshot: Dict[str, Any],
    strategy_group: Dict[str, Any],
    *,
    include_type: bool = True,
    include_platform: bool = True,
) -> tuple[List[FilterDetail], Dict[str, Any]]:
    x = _strategy_x(strategy_group)
    min_created = _strategy_min_created(strategy_group)
    details: List[FilterDetail] = []

    if include_type:
        typ = _norm_str(_first_present(snapshot, ["type", "trench_type", "category"]))
        details.append(
            _mk_pass("type_new_creation", typ, "type == new_creation", "new_creation")
            if typ == "new_creation"
            else _mk_failed("type_new_creation", typ, "type must be new_creation", "new_creation", missing=(typ == ""))
        )
    else:
        typ = _norm_str(_first_present(snapshot, ["type", "trench_type", "category"]))

    min_liquidity_usd = 5000 + 4 * min_created
    liquidity = _check_float(
        details, snapshot, "min_liquidity_usd",
        ["liquidity_usd", "liquidity", "pool_liquidity_usd"],
        lambda v: v >= min_liquidity_usd, f">= {min_liquidity_usd:.6g}",
    )

    low = 0.165 - 0.1 * x
    high = 0.26 + 0.2 * x
    top10 = _check_float(
        details, snapshot, "top_10_holder_rate_range",
        ["top_10_holder_rate", "top10_holder_rate", "top10_holder_percent", "top_10_rate"],
        lambda v: low < v < high, f"({low:.6g}, {high:.6g})",
    )

    top1 = _to_float(_first_present(snapshot, ["top1_holder_rate", "top_1_holder_rate", "top_holder_rate"]))
    if top1 is not None:
        details.append(_mk_pass("top1_holder_rate_observed", top1, "observed only in risk filter", "observed"))

    _check_bool_one(details, snapshot, "renounced_mint", ["renounced_mint", "mint_renounced", "is_mint_renounced"])
    _check_bool_one(details, snapshot, "renounced_freeze_account", ["renounced_freeze_account", "freeze_renounced", "is_freeze_renounced"])

    _check_float(details, snapshot, "rug_ratio", ["rug_ratio", "max_rug_ratio", "max_rugged_ratio"],
                 lambda v: v < -0.05 + x, f"< {-0.05 + x:.6g}")
    _check_float(details, snapshot, "entrapment_ratio", ["entrapment_ratio", "max_entrapment_ratio"],
                 lambda v: v < -0.05 + x, f"< {-0.05 + x:.6g}")
    _check_bool_zero(details, snapshot, "is_wash_trading", ["is_wash_trading", "wash_trading", "wash_trading_detected"])
    _check_float(details, snapshot, "rat_trader_amount_rate", ["rat_trader_amount_rate", "rat_trader_rate"],
                 lambda v: v < -0.05 + x, f"< {-0.05 + x:.6g}")
    _check_float(details, snapshot, "suspected_insider_hold_rate",
                 ["suspected_insider_hold_rate", "insider_hold_rate", "max_insider_ratio"],
                 lambda v: v < x, f"< {x:.6g}")
    _check_float(details, snapshot, "bundler_trader_amount_rate",
                 ["bundler_trader_amount_rate", "bundler_rate", "max_bundler_rate"],
                 lambda v: v < -0.05 + x, f"< {-0.05 + x:.6g}")
    _check_float(details, snapshot, "fresh_wallet_rate", ["fresh_wallet_rate", "fresh_wallets_rate"],
                 lambda v: v < 0.13 + 0.1 * x, f"< {0.13 + 0.1 * x:.6g}")

    raw_tax = _to_float(_first_present(snapshot, ["sell_tax", "sell_tax_rate"]))
    if raw_tax is not None and raw_tax > 1:
        raw_tax = raw_tax / 100.0
    sell_tax_ok = True if raw_tax is None else raw_tax < 0.1 * x
    details.append(
        _mk_pass("sell_tax", raw_tax, f"< {0.1 * x:.6g} (raw={snapshot.get('sell_tax')})", f"< {0.1 * x:.6g}")
        if sell_tax_ok
        else _mk_failed("sell_tax", raw_tax, f">= {0.1 * x:.6g} (raw={snapshot.get('sell_tax')})", 0.1 * x, missing=(raw_tax is None))
    )

    if x < 0.15:
        raw_social = _first_present(snapshot, ["has_at_least_one_social", "has_social", "has_twitter_or_telegram", "social_count"])
        if isinstance(raw_social, (int, float)) and not isinstance(raw_social, bool):
            ok = float(raw_social) > 0
            val = raw_social
        else:
            b = _to_int_bool(raw_social)
            ok = (b == 1)
            val = b
        details.append(
            _mk_pass("has_at_least_one_social", val, "required when x < 0.15", 1)
            if ok
            else _mk_failed("has_at_least_one_social", val, "required when x < 0.15", 1, missing=(val is None))
        )

    creator_status = _norm_str(_first_present(snapshot, ["creator_token_status", "creator_status"])).lower()
    dev_hold = _to_float(_first_present(snapshot, ["dev_team_hold_rate", "dev_hold_rate", "creator_hold_rate"]))
    dev_threshold = 0.03 + 0.1 * x
    creator_ok = creator_status in CREATOR_CLOSE_VALUES or (dev_hold is not None and dev_hold < dev_threshold)
    creator_value = creator_status or dev_hold
    details.append(
        _mk_pass("creator_token_status_or_dev_team_hold_rate", creator_value,
                 f"creator_close OR dev_hold < {dev_threshold:.6g}", ("creator_close", dev_threshold))
        if creator_ok
        else _mk_failed("creator_token_status_or_dev_team_hold_rate", creator_value,
                        f"creator_close OR dev_hold < {dev_threshold:.6g}", ("creator_close", dev_threshold),
                        missing=(creator_value in (None, "")))
    )

    burn_status = _norm_str(_first_present(snapshot, ["burn_status", "lp_burn_status", "burnt_status"])).lower()
    details.append(
        _mk_pass("burn_status", burn_status, "burn", "burn")
        if burn_status in BURN_VALUES
        else _mk_failed("burn_status", burn_status, "must be burn", "burn", missing=(burn_status == ""))
    )

    _check_float(details, snapshot, "sniper_count", ["sniper_count", "snipers", "sniper_trader_count"],
                 lambda v: v < 50 * x, f"< {50 * x:.6g}")

    if include_platform:
        platform = _norm_str(_first_present(snapshot, ["launchpad", "platform", "source_platform", "pool_platform"]))
        details.append(
            _mk_pass("platform", platform, f"in {sorted(PLATFORMS)}", sorted(PLATFORMS))
            if platform in PLATFORMS
            else _mk_failed("platform", platform, f"in {sorted(PLATFORMS)}", sorted(PLATFORMS), missing=(platform == ""))
        )
    else:
        platform = _norm_str(_first_present(snapshot, ["launchpad", "platform", "source_platform", "pool_platform"]))

    feature_vector = {
        "x": x,
        "min_created": min_created,
        "min_liquidity_usd_threshold": min_liquidity_usd,
        "top_10_holder_rate_low": low,
        "top_10_holder_rate_high": high,
        "type": typ,
        "liquidity_usd": liquidity,
        "top_10_holder_rate": top10,
        "top1_holder_rate": top1,
        "renounced_mint": _to_int_bool(snapshot.get("renounced_mint")),
        "renounced_freeze_account": _to_int_bool(snapshot.get("renounced_freeze_account")),
        "platform": platform,
    }
    return details, feature_vector


async def run_risk_filter(snapshot: Dict[str, Any], strategy_group: Dict[str, Any], now: datetime | None = None) -> FilterResult:
    details, feature_vector = _evaluate_core_risk_rules(snapshot, strategy_group, include_type=True, include_platform=True)
    feature_vector.update({"min_created": _strategy_min_created(strategy_group)})
    return FilterResult(all(d.passed for d in details), details, feature_vector)


async def run_initial_filter(snapshot: Dict[str, Any], strategy_group: Dict[str, Any], now: datetime | None = None) -> FilterResult:
    return await run_risk_filter(snapshot, strategy_group, now)


# ---------------------------------------------------------------------------
# Price filter (was second_filter.py)
# ---------------------------------------------------------------------------

async def run_price_filter(
    token: Dict[str, Any],
    strategy_group: Dict[str, Any],
    latest_price: Dict[str, Any],
    klines: List[Dict[str, Any]],
    buy_sell_1m: Dict[str, float],
) -> PriceFilterResult:
    details: List[Dict[str, Any]] = []
    y = float(strategy_group.get("y", 2.25))

    current_price = _current_price(latest_price, token)
    if current_price is None:
        details.append({"rule": "latest_price_present", "passed": False, "reason": "missing latest price"})
        return PriceFilterResult(False, details, {})

    high_5m, low_5m, kline_count = _extract_5m_range(klines, current_price)
    if high_5m is None or low_5m is None:
        details.append({
            "rule": "kline_price_range_present", "passed": False,
            "actual": {"kline_count": len(klines)},
            "reason": "need at least one usable kline price or current price",
        })
        return PriceFilterResult(False, details, {"current_price": current_price})

    if high_5m <= low_5m:
        details.append({
            "rule": "high_gt_low", "passed": False,
            "actual": {"high_5m": high_5m, "low_5m": low_5m},
            "reason": "cannot compute price range fraction when high <= low",
        })
        return PriceFilterResult(False, details, {
            "current_price": current_price, "high_5m": high_5m,
            "low_5m": low_5m, "kline_count_used": kline_count,
        })

    latest_1m: Dict[str, Any] = klines[-1] if klines else {}
    open_1m = _kline_open(latest_1m)
    close_1m = _kline_close(latest_1m)
    high_1m = _kline_high(latest_1m)
    low_1m = _kline_low(latest_1m)
    volume_1m = _kline_volume_usd(latest_1m) or 0.0

    # rule 1: volume_1m > max(liquidity_usd*(0.07-0.02*y), median_volume_prev_5m*(1.3-0.1*y))
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
        "rule": "volume_1m", "passed": cond1,
        "volume_1m": volume_1m, "liquidity_usd": liquidity_usd,
        "threshold_liquidity": threshold_a, "median_volume_prev": median_prev,
        "threshold_median": threshold_b, "threshold": threshold_1, "y": y,
    })

    # rule 2: close_1m > open_1m * (1 - 0.002 * y)
    if open_1m and open_1m > 0 and close_1m:
        threshold_2 = open_1m * (1.0 - 0.002 * y)
        cond2 = close_1m > threshold_2
    else:
        cond2 = False
        threshold_2 = None
    details.append({
        "rule": "close_gt_open_scaled", "passed": cond2,
        "open_1m": open_1m, "close_1m": close_1m,
        "threshold": threshold_2, "y": y,
    })

    # rule 3: (close_1m - low_1m) / (high_1m - low_1m) > (0.80 - 0.01 * y)
    if high_1m and low_1m and close_1m and high_1m > low_1m:
        candle_ratio = (close_1m - low_1m) / (high_1m - low_1m)
        threshold_3 = 0.80 - 0.01 * y
        cond3 = candle_ratio > threshold_3
    else:
        candle_ratio = None
        threshold_3 = None
        cond3 = False
    details.append({
        "rule": "candle_position", "passed": cond3,
        "candle_ratio": candle_ratio, "threshold": threshold_3,
        "open_1m": open_1m, "close_1m": close_1m,
        "high_1m": high_1m, "low_1m": low_1m, "y": y,
    })

    # rule 4: current_price > high_5m / y
    high_over_y = high_5m / y
    cond4 = current_price > high_over_y
    details.append({
        "rule": "price_gt_high_over_y", "passed": cond4,
        "current": current_price, "high_5m": high_5m,
        "threshold_value": high_over_y, "y": y,
    })

    # rule 5: current_price < low_5m * y
    low_times_y = low_5m * y
    cond5 = current_price < low_times_y
    details.append({
        "rule": "price_lt_low_times_y", "passed": cond5,
        "current": current_price, "low_5m": low_5m,
        "threshold_value": low_times_y, "y": y,
    })

    # rule 6: 0.8-0.2*y < frac < 0.35+0.2*y
    frac = (current_price - low_5m) / (high_5m - low_5m)
    low_frac = 0.8 - 0.2 * y
    high_frac = 0.35 + 0.2 * y
    cond6 = low_frac < frac < high_frac
    details.append({
        "rule": "fraction_range", "passed": cond6,
        "frac": frac, "range": [low_frac, high_frac],
        "current": current_price, "high_5m": high_5m,
        "low_5m": low_5m, "y": y,
    })

    passed = all(d.get("passed") for d in details)
    feature_vector = {
        "y": y, "current_price": current_price,
        "high_5m": high_5m, "low_5m": low_5m, "frac": frac,
        "volume_1m": volume_1m, "liquidity_usd": liquidity_usd,
        "median_volume_prev": median_prev,
        "open_1m": open_1m, "close_1m": close_1m,
        "high_1m": high_1m, "low_1m": low_1m,
        "kline_count_used": kline_count,
    }
    return PriceFilterResult(passed, details, feature_vector)
