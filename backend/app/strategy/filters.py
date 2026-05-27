"""Risk + price filter rules for GMGN trench candidates.

Risk screen — minimum liquidity depends on x:
    min_liquidity_usd = 40000 - 100000 * x

Price screen — y-scaling swap/price rules and smart degen check.
"""
from __future__ import annotations

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

    min_liquidity_usd = 40000 - 100000 * x
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

    burn_status = _norm_str(_first_present(snapshot, ["burn_status", "lp_burn_status", "burnt_status"])).lower()
    details.append(
        _mk_pass("burn_status", burn_status, "burn", "burn")
        if burn_status in BURN_VALUES
        else _mk_failed("burn_status", burn_status, "must be burn", "burn", missing=(burn_status == ""))
    )

    _check_float(details, snapshot, "sniper_count", ["sniper_count", "snipers", "sniper_trader_count"],
                 lambda v: v < 50 * x, f"< {50 * x:.6g}")

    top1_threshold = 0.049 + 0.01 * x
    top1_rate = _to_float(_first_present(snapshot, ["top1_holder_rate", "top1_rate"]))
    top1_ok = top1_rate is not None and top1_rate < top1_threshold
    details.append(
        _mk_pass("top1_holder", top1_rate, f"< {top1_threshold:.6g}", top1_threshold)
        if top1_ok
        else _mk_failed("top1_holder", top1_rate, f"< {top1_threshold:.6g}", top1_threshold,
                        missing=(top1_rate is None))
    )

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
        "top1_holder_threshold": top1_threshold,
    }
    return details, feature_vector


async def run_risk_filter(snapshot: Dict[str, Any], strategy_group: Dict[str, Any], now: datetime | None = None) -> FilterResult:
    details, feature_vector = _evaluate_core_risk_rules(snapshot, strategy_group, include_type=True, include_platform=True)
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
    smart_degen_holders: List[Dict[str, Any]],
) -> PriceFilterResult:
    details: List[Dict[str, Any]] = []
    x = float(strategy_group.get("x", 0.2))
    y = float(strategy_group.get("y", 2.25))

    current_price = _current_price(latest_price, token)
    if current_price is None or current_price <= 0:
        details.append({"rule": "latest_price_present", "passed": False, "reason": "missing latest price"})
        return PriceFilterResult(False, details, {})

    swaps_5m = _to_float(latest_price.get("swaps_5m"))
    swaps_1h = _to_float(latest_price.get("swaps_1h"))
    price_1h = _to_float(latest_price.get("price_1h"))
    price_5m = _to_float(latest_price.get("price_5m"))
    price_1m = _to_float(latest_price.get("price_1m"))

    creation_ts = _to_float(token.get("creation_timestamp"))
    now_ts = datetime.utcnow().timestamp()
    age_minutes = (now_ts - creation_ts / 1000.0) if (creation_ts and creation_ts > 1e9) else None
    if swaps_1h and swaps_1h > 0:
        if age_minutes is not None and age_minutes < 60:
            divisor = max(1, age_minutes / 5.0)
        else:
            divisor = 12.0
        swaps_threshold = max(0, (4.0 - y) * (swaps_1h / divisor))
        cond_swaps = swaps_5m is not None and swaps_5m > swaps_threshold
    else:
        cond_swaps = False
        swaps_threshold = None
    details.append({
        "rule": "swaps_5m_scaled", "passed": cond_swaps,
        "swaps_5m": swaps_5m, "swaps_1h": swaps_1h,
        "threshold": swaps_threshold, "y": y, "age_minutes": age_minutes,
    })

    if price_1h and price_1h > 0:
        pct_change_1h = ((current_price - price_1h) / price_1h) * 100.0
        pct_threshold = 0.7 - 0.2 * y
        cond_pct = pct_change_1h > pct_threshold
    else:
        pct_change_1h = None
        pct_threshold = None
        cond_pct = False
    details.append({
        "rule": "price_change_1h", "passed": cond_pct,
        "current": current_price, "price_1h": price_1h,
        "pct_change": pct_change_1h, "threshold": pct_threshold, "y": y,
    })

    min_degen_count = 4.0 - 10.0 * x
    degen_count = len(smart_degen_holders)
    cond_degen = degen_count >= max(1, int(min_degen_count))
    degen_hold_ok = False
    degen_hold_detail: Dict[str, Any] = {}
    if smart_degen_holders:
        holders_by_pct = sorted(smart_degen_holders, key=lambda h: _to_float(h.get("amount_percentage")) or 0.0, reverse=True)
        max_holder = holders_by_pct[0]
        max_pct = _to_float(max_holder.get("amount_percentage"))
        max_usd = _to_float(max_holder.get("usd_value"))
        max_ok = (max_pct is not None and max_pct > 1.5) or (max_usd is not None and max_usd > 200)
        min_holder = holders_by_pct[-1]
        min_pct = _to_float(min_holder.get("amount_percentage"))
        min_usd = _to_float(min_holder.get("usd_value"))
        min_ok = (min_pct is not None and min_pct > 1.0) or (min_usd is not None and min_usd > 100)
        degen_hold_ok = max_ok and min_ok
        degen_hold_detail = {
            "max_holder_pct": max_pct, "max_holder_usd": max_usd, "max_ok": max_ok,
            "min_holder_pct": min_pct, "min_holder_usd": min_usd, "min_ok": min_ok,
        }
    cond_degen_full = cond_degen and degen_hold_ok
    details.append({
        "rule": "smart_degen", "passed": cond_degen_full,
        "degen_count": degen_count, "min_degen_count": max(1, int(min_degen_count)),
        "holdings": degen_hold_detail, "x": x,
    })

    passed = all(d.get("passed") for d in details)
    feature_vector = {
        "x": x, "y": y, "current_price": current_price,
        "swaps_5m": swaps_5m, "swaps_1h": swaps_1h,
        "price_change_1h_pct": pct_change_1h,
        "degen_count": degen_count,
        "age_minutes": age_minutes,
    }
    return PriceFilterResult(passed, details, feature_vector)
