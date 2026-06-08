"""Risk + price filter rules for GMGN trench candidates.

All thresholds from thresholds.py — single source of truth.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..config import settings
from .thresholds import compute_thresholds, StrategyThresholds, normalize_rate_fraction

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
# Kline helpers
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

def _current_price(latest_price: Dict[str, Any], token: Dict[str, Any]) -> Optional[float]:
    return _to_float(
        _first_present(
            latest_price,
            ["price", "price_usd", "latest_price_usd", "close", "c"],
            _first_present(token, ["price", "price_usd", "latest_price_usd"]),
        )
    )

# ---------------------------------------------------------------------------
# Creation-time parsing
# ---------------------------------------------------------------------------

def _parse_creation_ts(token: Dict[str, Any]) -> Tuple[Optional[float], str, bool]:
    pool_created_at = token.get("pool_created_at")
    if isinstance(pool_created_at, (int, float)) and float(pool_created_at) > 0:
        val = float(pool_created_at)
        if val > 1e12:
            return val / 1000.0, "pool_created_at_ms", False
        if val > 1e9:
            return val, "pool_created_at_s", False
    if isinstance(pool_created_at, str) and pool_created_at.strip():
        try:
            dt = datetime.fromisoformat(pool_created_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp(), "pool_created_at_iso", False
        except Exception:
            pass
    raw_creation = _first_present(token, ["creation_timestamp", "created_timestamp", "created_at", "open_time", "launch_time"])
    if raw_creation is not None:
        try:
            val = float(raw_creation)
            if val > 1e12:
                return val / 1000.0, "creation_timestamp_ms", False
            if val > 1e9:
                return val, "creation_timestamp_s", False
        except Exception:
            pass
    return None, "missing", True

def _compute_age_minutes(creation_ts: Optional[float]) -> Optional[float]:
    if creation_ts is None:
        return None
    return (datetime.now(timezone.utc).timestamp() - creation_ts) / 60.0

# ---------------------------------------------------------------------------
# Detail helpers
# ---------------------------------------------------------------------------

def _mk_pass(name: str, value: Any, reason: str, threshold: Any) -> FilterDetail:
    return FilterDetail(name=name, passed=True, value=value, threshold=threshold, reason=reason)

def _mk_failed(name: str, value: Any, reason: str, threshold: Any, missing: bool = False) -> FilterDetail:
    return FilterDetail(name=name, passed=False, value=value, threshold=threshold, reason=reason, missing=missing)

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
# Stage 1: Trenches filter construction (returns body params)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Helpers for renounced_* / bool-one checks
# ---------------------------------------------------------------------------

def _check_bool_one(details, snapshot, name, keys, required=True):
    raw = _first_present(snapshot, keys)
    value = _to_int_bool(raw)
    if value is None:
        if required:
            details.append(_mk_failed(name, raw, "missing required boolean=1 field", 1, missing=True))
        else:
            details.append(_mk_pass(name, raw, "optional field missing; treated as pass", 1))
        return value
    details.append(_mk_pass(name, value, "equals 1", 1) if value == 1 else _mk_failed(name, value, "must equal 1", 1))
    return value


# ---------------------------------------------------------------------------
# Stage 2: Entry local risk filter (post-fetch, pre-buy)
# ---------------------------------------------------------------------------

async def run_entry_local_risk_filter(
    snapshot: Dict[str, Any],
    strategy_group: Dict[str, Any],
    now: Any = None,
) -> FilterResult:
    """Local risk rules not pre-filtered by trenches.

    Does NOT check type, platform, liquidity, marketcap, volume24h, etc.
    (those are pre-filtered by trenches or trenches-local from snapshot).
    """
    x = float(strategy_group.get("x") if strategy_group.get("x") is not None else settings.STRATEGY_DEFAULT_X)
    t = compute_thresholds(x)
    details: List[FilterDetail] = []
    fv: Dict[str, Any] = {"x": x}

    _check_bool_one(details, snapshot, "renounced_mint", ["renounced_mint", "mint_renounced", "is_mint_renounced"])
    _check_bool_one(details, snapshot, "renounced_freeze_account", ["renounced_freeze_account", "freeze_renounced", "is_freeze_renounced", "freeze_authority_renounced"])
    _check_bool_zero(details, snapshot, "is_wash_trading", ["is_wash_trading", "wash_trading", "wash_trading_detected"])

    _check_float(details, snapshot, "rat_trader_amount_rate", ["rat_trader_amount_rate", "rat_trader_rate"],
                 lambda v: v < t.common_risk, f"< {t.common_risk:.6g}")
    _check_float(details, snapshot, "suspected_insider_hold_rate",
                 ["suspected_insider_hold_rate", "insider_hold_rate", "max_insider_ratio"],
                 lambda v: v < t.common_risk, f"< {t.common_risk:.6g}")

    raw_tax = _to_float(_first_present(snapshot, ["sell_tax", "sell_tax_rate"]))
    if raw_tax is not None and raw_tax > 1:
        raw_tax = raw_tax / 100.0
    sell_tax_ok = raw_tax is not None and raw_tax < t.sell_tax_max
    details.append(
        _mk_pass("sell_tax", raw_tax, f"< {t.sell_tax_max:.6g}", f"< {t.sell_tax_max:.6g}")
        if sell_tax_ok
        else _mk_failed("sell_tax", raw_tax, f">= {t.sell_tax_max:.6g} or missing", t.sell_tax_max, missing=(raw_tax is None))
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

    burn_status = _norm_str(_first_present(snapshot, ["burn_status", "lp_burn_status", "burnt_status"])).lower()
    details.append(
        _mk_pass("burn_status", burn_status, "burn", "burn")
        if burn_status in BURN_VALUES
        else _mk_failed("burn_status", burn_status, "must be burn", "burn", missing=(burn_status == ""))
    )

    _check_float(details, snapshot, "sniper_count", ["sniper_count", "snipers", "sniper_trader_count"],
                 lambda v: v < t.sniper_count_max, f"< {t.sniper_count_max:.6g}")

    fv = {"x": x}
    return FilterResult(all(d.passed for d in details), details, fv)

# ---------------------------------------------------------------------------
# Stage 2b: Top1 holder via API (separate high-weight endpoint)
# ---------------------------------------------------------------------------

def evaluate_top1_holder(top1_holder: Optional[Dict[str, Any]], x: float) -> FilterResult:
    """Evaluate top1 holder with addr_type==0.

    Only regular addresses (addr_type==0) are considered for the top1 check.
    If the provided holder dict has addr_type != 0, it is treated as missing.
    """
    t = compute_thresholds(x)
    rate = None
    missing_reason = "missing"
    if top1_holder:
        addr_type = top1_holder.get("addr_type", None)
        try:
            addr_type = int(addr_type) if addr_type is not None else None
        except (TypeError, ValueError):
            addr_type = None
        if addr_type is None or addr_type != 0:
            missing_reason = f"addr_type={addr_type} != 0, skipped"
            top1_holder = None
        else:
            rate = normalize_rate_fraction(_to_float(_first_present(top1_holder, ["top1_holder_rate", "rate", "amount_percentage", "percentage", "hold_rate"])))
    passed = rate is not None and t.top1_addr_type0_min < rate < t.top1_addr_type0_max
    details = [FilterDetail(name="top1_holder_addr_type0", passed=passed, value=rate, threshold=t.top1_addr_type0_max,
                            reason=f"top1 rate={rate}, range=[{t.top1_addr_type0_min}, {t.top1_addr_type0_max})" if rate is not None else missing_reason,
                            missing=rate is None)]
    return FilterResult(passed, details, {"top1_holder_rate": rate, "top1_threshold": t.top1_addr_type0_max})

# ---------------------------------------------------------------------------
# Stage 3: Price activity rules (swaps + price_change, no smart_degen)
# ---------------------------------------------------------------------------

async def evaluate_price_activity_rules(
    token: Dict[str, Any],
    strategy_group: Dict[str, Any],
    latest_price: Dict[str, Any],
    klines: Optional[List[Dict[str, Any]]] = None,
) -> PriceFilterResult:
    details: List[Dict[str, Any]] = []
    x = float(strategy_group.get("x") if strategy_group.get("x") is not None else settings.STRATEGY_DEFAULT_X)
    t = compute_thresholds(x)

    current_price = _current_price(latest_price, token)
    if not latest_price:
        current_price = _to_float(_first_present(token, ["price_usd", "price", "latest_price_usd"]))
    if current_price is None or current_price <= 0:
        details.append({"rule": "latest_price_present", "passed": False, "reason": "missing latest price",
                        "source": "missing", "current_price": current_price})
        return PriceFilterResult(False, details, {})

    def _first_price_field(keys: Sequence[str]) -> Tuple[Any, str]:
        raw = _first_present(latest_price, keys)
        if raw is not None:
            return raw, "latest_price"
        for nest_key in ("price", "pool", "token", "info"):
            nested = latest_price.get(nest_key)
            if isinstance(nested, dict):
                raw = _first_present(nested, keys)
                if raw is not None:
                    return raw, nest_key
        raw = _first_present(token, keys)
        if raw is not None:
            return raw, "token_snapshot"
        return None, "missing"

    # swaps_1h rule — support multiple GMGN field name conventions
    swaps_1h_raw, swaps_source = _first_price_field(["swaps_1h", "swaps1h", "swap_1h", "trades_1h", "trade_1h"])
    swaps_1h = _to_float(swaps_1h_raw)

    creation_ts, _, age_missing = _parse_creation_ts(token)
    age_minutes = _compute_age_minutes(creation_ts)
    if age_minutes is None and not age_missing:
        age_missing = True

    cond_swaps_overall = swaps_1h is not None and swaps_1h > t.swaps_1h_min
    details.append({
        "rule": "swaps_1h_min", "passed": cond_swaps_overall,
        "swaps_1h": swaps_1h,
        "threshold": t.swaps_1h_min, "age_minutes": age_minutes,
        "source": swaps_source, "age_missing": age_missing,
    })

    # volume_1h / swaps_1h rule
    volume_1h_raw, volume_1h_source = _first_price_field(["volume_1h", "volume1h", "volume_1h_usd", "volume_h1"])
    volume_1h = _to_float(volume_1h_raw)
    if volume_1h is None:
        buy_volume_1h_raw, buy_volume_1h_source = _first_price_field(["buy_volume_1h", "buyVolume1h", "buy_volume1h"])
        sell_volume_1h_raw, sell_volume_1h_source = _first_price_field(["sell_volume_1h", "sellVolume1h", "sell_volume1h"])
        buy_volume_1h = _to_float(buy_volume_1h_raw)
        sell_volume_1h = _to_float(sell_volume_1h_raw)
        if buy_volume_1h is not None or sell_volume_1h is not None:
            volume_1h = (buy_volume_1h or 0.0) + (sell_volume_1h or 0.0)
            volume_1h_source = f"{buy_volume_1h_source}+{sell_volume_1h_source}"
    volume_per_swap_cond = False
    vps = None
    if swaps_1h == 0:
        vps = 0.0
    elif volume_1h is not None and swaps_1h is not None and swaps_1h > 0:
        vps = volume_1h / swaps_1h
    if vps is not None:
        volume_per_swap_cond = vps > t.volume_per_swap_1h_min
    details.append({
        "rule": "volume_per_swap_1h", "passed": volume_per_swap_cond,
        "volume_1h": volume_1h, "swaps_1h": swaps_1h, "vps": vps, "value": vps,
        "threshold": t.volume_per_swap_1h_min, "source": volume_1h_source,
        "data_unavailable": volume_1h is None or swaps_1h is None,
    })

    # price_change_1h rule — prefer direct API field, then compute from price_1h or klines
    lower_pct = t.price_change_1h_min_pct
    upper_pct = t.price_change_1h_max_pct
    pct_change_1h: Optional[float] = None
    price_change_source: str = "missing"
    price_change_age_mode: str = "unknown"
    cond_pct: bool = False

    # First try direct price_change_percent1h from GMGN
    pct_change_1h = _to_float(_first_present(latest_price, [
        "price_change_percent1h", "price_change_1h", "change_1h", "price_change_percent_1h",
        "price_change_1h_pct",
    ]))
    # Only fall back to nested containers if top-level is missing
    if pct_change_1h is None:
        for nest_key in ("price", "pool", "token", "info"):
            nested = latest_price.get(nest_key)
            if isinstance(nested, dict):
                pct_change_1h = _to_float(_first_present(nested, [
                    "price_change_percent1h", "price_change_1h", "change_1h", "price_change_percent_1h",
                ]))
                if pct_change_1h is not None:
                    break
    if pct_change_1h is not None:
        price_change_source = "direct_price_change_percent1h"
        price_change_age_mode = "direct_api"
        cond_pct = lower_pct < pct_change_1h < upper_pct

    # Fallback: kline computation for young tokens
    if pct_change_1h is None and age_minutes is not None and age_minutes < 60 and klines:
        sorted_klines = sort_klines(klines)
        if sorted_klines:
            open_price = _kline_open(sorted_klines[0])
            if open_price is not None and open_price > 0:
                pct_change_1h = ((current_price - open_price) / open_price) * 100.0
                price_change_source = "kline_since_open"
                price_change_age_mode = "young_kline_priority"
                cond_pct = lower_pct < pct_change_1h < upper_pct
        if pct_change_1h is None:
            price_change_age_mode = "young_no_kline_fallback"

    # Fallback: compute from price_1h
    if pct_change_1h is None:
        if price_change_age_mode == "unknown":
            price_change_age_mode = "mature_computed"
        price_1h = _to_float(_first_present(latest_price, ["price_1h", "price1h"]))
        if price_1h is None:
            for nest_key in ("price", "pool", "token", "info"):
                nested = latest_price.get(nest_key)
                if isinstance(nested, dict):
                    price_1h = _to_float(_first_present(nested, ["price_1h", "price1h"]))
                    if price_1h is not None:
                        break
        if price_1h and price_1h > 0:
            pct_change_1h = ((current_price - price_1h) / price_1h) * 100.0
            price_change_source = "computed_from_price_1h"
            cond_pct = lower_pct < pct_change_1h < upper_pct
        else:
            price_change_source = "missing"

    details.append({
        "rule": "price_change_1h", "passed": cond_pct,
        "current": current_price, "pct_change": pct_change_1h,
        "lower_threshold": lower_pct, "upper_threshold": upper_pct, "source": price_change_source,
        "age_mode": price_change_age_mode, "age_minutes": age_minutes, "age_missing": age_missing,
        "price_change_unit": "percent_points",
    })

    # 24h price-range percentile is derived from official OHLCV kline data.
    price_range_percentile = None
    percentile_source = "missing"
    high_24h = None
    low_24h = None
    if klines:
        highs = [_kline_high(k) for k in klines]
        lows = [_kline_low(k) for k in klines]
        highs = [v for v in highs if v is not None and v > 0]
        lows = [v for v in lows if v is not None and v > 0]
        if highs and lows:
            high_24h = max(highs)
            low_24h = min(lows)
            if high_24h > low_24h:
                price_range_percentile = (current_price - low_24h) / (high_24h - low_24h)
                percentile_source = "kline_24h"
    elif klines is not None:
        percentile_source = "kline_empty"
    cond_range = (
        price_range_percentile is not None
        and t.price_range_24h_percentile_min < price_range_percentile < t.price_range_24h_percentile_max
    )
    details.append({
        "rule": "price_range_24h_percentile", "passed": cond_range,
        "current_price": current_price, "high_24h": high_24h, "low_24h": low_24h,
        "percentile": price_range_percentile, "value": price_range_percentile,
        "lower_threshold": t.price_range_24h_percentile_min,
        "upper_threshold": t.price_range_24h_percentile_max,
        "source": percentile_source,
        "data_unavailable": price_range_percentile is None,
    })

    passed = all(d.get("passed") for d in details)

    fv = {
        "x": x, "current_price": current_price,
        "swaps_1h": swaps_1h, "volume_1h": volume_1h, "volume_per_swap_1h": vps,
        "price_change_1h_pct": pct_change_1h, "price_change_source": price_change_source,
        "price_change_age_mode": price_change_age_mode, "price_change_unit": "percent_points",
        "age_minutes": age_minutes, "creation_ts": creation_ts,
        "swaps_source": swaps_source, "age_missing": age_missing,
        "price_range_24h_percentile": price_range_percentile,
        "price_range_24h_percentile_source": percentile_source,
        "price_range_24h_high": high_24h,
        "price_range_24h_low": low_24h,
    }
    return PriceFilterResult(passed, details, fv)

# ---------------------------------------------------------------------------
# Stage 4: Smart degen holders evaluation
# ---------------------------------------------------------------------------

def _normalize_pct(pct: Optional[float]) -> Tuple[Optional[float], str]:
    """Normalize amount_percentage to decimal (0.015 = 1.5%).

    Returns (normalized, source_desc) where source_desc is 'raw_decimal' or 'pct_divided_by_100'.
    """
    if pct is None:
        return None, "missing"
    if pct > 1.0:
        return pct / 100.0, "pct_divided_by_100"
    return pct, "raw_decimal"


async def evaluate_smart_degen(
    strategy_group: Dict[str, Any],
    smart_degen_holders: List[Dict[str, Any]],
) -> PriceFilterResult:
    details: List[Dict[str, Any]] = []
    x = float(strategy_group.get("x") if strategy_group.get("x") is not None else settings.STRATEGY_DEFAULT_X)
    t = compute_thresholds(x)

    # min_smart_degen_count = max(0, 2 - 10*x). Strictly greater: required > that value.
    raw_required = t.min_smart_degen_count_raw
    required_count_for_count_rule = max(0, int(math.floor(raw_required)) + 1)
    required_count_for_holding_eval = max(1, required_count_for_count_rule)
    degen_count = len(smart_degen_holders)
    cond_degen = degen_count >= required_count_for_count_rule

    degen_hold_ok = False
    degen_hold_detail: Dict[str, Any] = {}
    if smart_degen_holders and cond_degen and degen_count >= 1:
        holders_sorted = sorted(smart_degen_holders, key=lambda h: _to_float(h.get("amount_percentage")) or 0.0, reverse=True)
        top_n = holders_sorted[:required_count_for_holding_eval]
        if top_n:
            max_holder = top_n[0]
            max_pct = _to_float(max_holder.get("amount_percentage"))
            max_usd = _to_float(max_holder.get("usd_value"))
            max_pct_norm, max_norm_src = _normalize_pct(max_pct)
            max_ok = (max_pct_norm is not None and max_pct_norm > t.smart_degen_max_pct) or (max_usd is not None and max_usd > t.smart_degen_max_usd)

            min_holder = top_n[-1] if len(top_n) > 1 else top_n[0]
            min_pct = _to_float(min_holder.get("amount_percentage"))
            min_usd = _to_float(min_holder.get("usd_value"))
            min_pct_norm, min_norm_src = _normalize_pct(min_pct)
            min_ok = (min_pct_norm is not None and min_pct_norm > t.smart_degen_min_pct) or (min_usd is not None and min_usd > t.smart_degen_min_usd)

            degen_hold_ok = max_ok and min_ok
            degen_hold_detail = {
                "required_count": required_count_for_holding_eval, "raw_required": raw_required,
                "required_count_for_count": required_count_for_count_rule,
                "actual_count": len(top_n),
                "max_holder_pct_raw": max_pct, "max_holder_pct_norm": max_pct_norm, "max_holder_pct_norm_src": max_norm_src,
                "max_holder_usd": max_usd, "max_ok": max_ok,
                "min_holder_pct_raw": min_pct, "min_holder_pct_norm": min_pct_norm, "min_holder_pct_norm_src": min_norm_src,
                "min_holder_usd": min_usd, "min_ok": min_ok,
                "threshold_max_pct": t.smart_degen_max_pct, "threshold_min_pct": t.smart_degen_min_pct,
                "threshold_max_usd": t.smart_degen_max_usd, "threshold_min_usd": t.smart_degen_min_usd,
            }
    elif not smart_degen_holders:
        degen_hold_detail = {"reason": "no smart degen holders available"}

    cond_degen_full = cond_degen and degen_hold_ok
    details.append({
        "rule": "smart_degen", "passed": cond_degen_full,
        "degen_count": degen_count, "required_count": required_count_for_count_rule,
        "held_eval_required_count": required_count_for_holding_eval,
        "holdings": degen_hold_detail, "x": x,
    })

    passed = all(d.get("passed") for d in details)
    fv = {"x": x, "degen_count": degen_count, "required_count": required_count_for_count_rule,
          "degen_hold_ok": degen_hold_ok}
    return PriceFilterResult(passed, details, fv)

# --- Legacy aliases ---
run_initial_filter = run_entry_local_risk_filter
run_price_filter = evaluate_price_activity_rules


# ---------------------------------------------------------------------------
# Position / holding risk filter
# ---------------------------------------------------------------------------

async def run_holding_risk_filter(
    snapshot: Dict[str, Any],
    strategy_group: Dict[str, Any],
    now: Any = None,
) -> FilterResult:
    """Risk rules checked during position monitoring (holding period).

    Does NOT check type == new_creation (type == completed = exit, not risk fail).
    All thresholds from StrategyThresholds.
    """
    x = float(strategy_group.get("x") if strategy_group.get("x") is not None else settings.STRATEGY_DEFAULT_X)
    t = compute_thresholds(x)
    details: List[FilterDetail] = []

    _check_float(details, snapshot, "rug_ratio", ["rug_ratio", "max_rug_ratio", "max_rugged_ratio", "rug"],
                 lambda v: v < t.common_risk, f"< {t.common_risk:.6g}", required=False)
    _check_float(details, snapshot, "entrapment_ratio", ["entrapment_ratio", "max_entrapment_ratio", "entrapment"],
                 lambda v: v < t.common_risk, f"< {t.common_risk:.6g}", required=False)
    _check_float(details, snapshot, "insider_ratio", ["max_insider_ratio", "insider_ratio", "insider_rate"],
                 lambda v: v < t.common_risk, f"< {t.common_risk:.6g}", required=False)
    _check_float(details, snapshot, "suspected_insider_hold_rate",
                 ["suspected_insider_hold_rate", "insider_hold_rate", "insider_rate"],
                 lambda v: v < t.common_risk, f"< {t.common_risk:.6g}", required=False)
    _check_float(details, snapshot, "bundler_trader_amount_rate",
                 ["bundler_trader_amount_rate", "bundler_rate", "max_bundler_rate", "bundler"],
                 lambda v: v < t.common_risk, f"< {t.common_risk:.6g}", required=False)
    _check_float(details, snapshot, "top_10_holder_rate_range",
                 ["top_10_holder_rate", "top10_holder_rate", "top10_holder_percent", "top_10_rate",
                  "top_holder_rate", "top10_holder_pct", "top10HolderRate", "top_10_holder_percent",
                  "top10HolderPercent"],
                 lambda v: t.min_top_holder_rate < v < t.max_top_holder_rate,
                 f"({t.min_top_holder_rate:.6g}, {t.max_top_holder_rate:.6g})", required=False)
    _check_float(details, snapshot, "fresh_wallet_rate", ["fresh_wallet_rate", "fresh_wallets_rate", "fresh_wallet"],
                 lambda v: v < t.max_fresh_wallet_rate, f"< {t.max_fresh_wallet_rate:.6g}", required=False)
    _check_float(details, snapshot, "creator_balance_rate",
                 ["creator_balance_rate", "dev_team_hold_rate", "creator_hold_rate", "dev_hold_rate"],
                 lambda v: v < t.max_creator_balance_rate, f"< {t.max_creator_balance_rate:.6g}", required=False)
    _check_float(details, snapshot, "holder_count", ["holder_count", "holders", "total_holders", "holder"],
                 lambda v: v > t.min_holder_count_raw, f"> {t.min_holder_count_raw:.6g}", required=False)
    _check_bool_zero(details, snapshot, "is_wash_trading", ["is_wash_trading", "wash_trading", "wash_trading_detected", "is_wash"], required=False)
    _check_float(details, snapshot, "rat_trader_amount_rate", ["rat_trader_amount_rate", "rat_trader_rate", "rat_trader"],
                 lambda v: v < t.common_risk, f"< {t.common_risk:.6g}", required=False)
    _check_float(details, snapshot, "sniper_count", ["sniper_count", "snipers", "sniper_trader_count", "sniper_cnt"],
                 lambda v: v < t.sniper_count_max, f"< {t.sniper_count_max:.6g}", required=False)
    return FilterResult(all(d.passed for d in details), details, {"x": x})


# --- Legacy aliases (kept after all function definitions) ---
run_risk_filter = run_holding_risk_filter
