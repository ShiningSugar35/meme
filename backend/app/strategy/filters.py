"""Initial and risk-filter rules for GMGN trench candidates.

The age/timing parameter ``t_seconds`` is intentionally *not* evaluated here.
It belongs to the provider discovery query: each strategy group asks GMGN for
pools whose age is in [t, t+60] seconds, then this module evaluates only the
risk/platform/holder conditions on the returned pools.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


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


def _mk_pass(name: str, value: Any, reason: str, threshold: Any) -> FilterDetail:
    return FilterDetail(name=name, passed=True, value=value, threshold=threshold, reason=reason)


def _mk_failed(name: str, value: Any, reason: str, threshold: Any, missing: bool = False) -> FilterDetail:
    return FilterDetail(name=name, passed=False, value=value, threshold=threshold, reason=reason, missing=missing)


def _check_float(
    details: List[FilterDetail],
    snapshot: Dict[str, Any],
    name: str,
    keys: Iterable[str],
    predicate,
    threshold_desc: Any,
    required: bool = True,
):
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

    details.append(_mk_pass(name, value, f"satisfies {threshold_desc}", threshold_desc) if ok else _mk_failed(name, value, f"violates {threshold_desc}", threshold_desc))
    return value


def _check_bool_one(details: List[FilterDetail], snapshot: Dict[str, Any], name: str, keys: Iterable[str], required: bool = True):
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


def _check_bool_zero(details: List[FilterDetail], snapshot: Dict[str, Any], name: str, keys: Iterable[str], required: bool = True):
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


def _norm_str(v: Any) -> str:
    return str(v or "").strip()


def _evaluate_core_risk_rules(
    snapshot: Dict[str, Any],
    strategy_group: Dict[str, Any],
    *,
    include_type: bool = True,
    include_platform: bool = True,
) -> tuple[List[FilterDetail], Dict[str, Any]]:
    x = float(strategy_group.get("x", 0.2))
    details: List[FilterDetail] = []

    if include_type:
        typ = _norm_str(_first_present(snapshot, ["type", "trench_type", "category"]))
        details.append(_mk_pass("type_new_creation", typ, "type == new_creation", "new_creation") if typ == "new_creation" else _mk_failed("type_new_creation", typ, "type must be new_creation", "new_creation", missing=(typ == "")))
    else:
        typ = _norm_str(_first_present(snapshot, ["type", "trench_type", "category"]))

    liquidity = _check_float(
        details,
        snapshot,
        "min_liquidity_usd",
        ["liquidity_usd", "liquidity", "pool_liquidity_usd"],
        lambda v: v >= 10000 - 20000 * x,
        f">= {10000 - 20000 * x:.6g}",
    )

    low = 0.175 - 0.15 * x
    high = 0.25 + 0.25 * x
    top10 = _check_float(
        details,
        snapshot,
        "top_10_holder_rate_range",
        ["top_10_holder_rate", "top10_holder_rate", "top10_holder_percent", "top_10_rate"],
        lambda v: low < v < high,
        f"({low:.6g}, {high:.6g})",
    )

    top1 = _to_float(_first_present(snapshot, ["top1_holder_rate", "top_1_holder_rate", "top_holder_rate"]))
    if top1 is not None:
        details.append(_mk_pass("top1_holder_rate_observed", top1, "observed only in initial/core filter", "observed"))

    _check_bool_one(details, snapshot, "renounced_mint", ["renounced_mint", "mint_renounced", "is_mint_renounced"])
    _check_bool_one(details, snapshot, "renounced_freeze_account", ["renounced_freeze_account", "freeze_renounced", "is_freeze_renounced"])

    _check_float(
        details,
        snapshot,
        "rug_ratio",
        ["rug_ratio", "max_rug_ratio", "max_rugged_ratio"],
        lambda v: v < -0.05 + x,
        f"< {-0.05 + x:.6g}",
    )
    _check_float(
        details,
        snapshot,
        "entrapment_ratio",
        ["entrapment_ratio", "max_entrapment_ratio"],
        lambda v: v < -0.05 + x,
        f"< {-0.05 + x:.6g}",
    )
    _check_bool_zero(details, snapshot, "is_wash_trading", ["is_wash_trading", "wash_trading", "wash_trading_detected"])
    _check_float(
        details,
        snapshot,
        "rat_trader_amount_rate",
        ["rat_trader_amount_rate", "rat_trader_rate"],
        lambda v: v < -0.05 + x,
        f"< {-0.05 + x:.6g}",
    )
    _check_float(
        details,
        snapshot,
        "suspected_insider_hold_rate",
        ["suspected_insider_hold_rate", "insider_hold_rate", "max_insider_ratio"],
        lambda v: v < x,
        f"< {x:.6g}",
    )
    _check_float(
        details,
        snapshot,
        "bundler_trader_amount_rate",
        ["bundler_trader_amount_rate", "bundler_rate", "max_bundler_rate"],
        lambda v: v < -0.05 + x,
        f"< {-0.05 + x:.6g}",
    )
    _check_float(
        details,
        snapshot,
        "fresh_wallet_rate",
        ["fresh_wallet_rate", "fresh_wallets_rate"],
        lambda v: v < 0.13 + 0.1 * x,
        f"< {0.13 + 0.1 * x:.6g}",
    )
    _check_float(
        details,
        snapshot,
        "sell_tax",
        ["sell_tax", "sell_tax_rate"],
        lambda v: v < 0.1 * x,
        f"< {0.1 * x:.6g}",
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
        details.append(_mk_pass("has_at_least_one_social", val, "required when x < 0.15", 1) if ok else _mk_failed("has_at_least_one_social", val, "required when x < 0.15", 1, missing=(val is None)))

    creator_status = _norm_str(_first_present(snapshot, ["creator_token_status", "creator_status"])).lower()
    dev_hold = _to_float(_first_present(snapshot, ["dev_team_hold_rate", "dev_hold_rate", "creator_hold_rate"]))
    dev_threshold = 0.03 + 0.1 * x
    creator_ok = creator_status in CREATOR_CLOSE_VALUES or (dev_hold is not None and dev_hold < dev_threshold)
    creator_value = creator_status or dev_hold
    details.append(_mk_pass("creator_token_status_or_dev_team_hold_rate", creator_value, f"creator_close OR dev_hold < {dev_threshold:.6g}", ("creator_close", dev_threshold)) if creator_ok else _mk_failed("creator_token_status_or_dev_team_hold_rate", creator_value, f"creator_close OR dev_hold < {dev_threshold:.6g}", ("creator_close", dev_threshold), missing=(creator_value in (None, ""))))

    burn_status = _norm_str(_first_present(snapshot, ["burn_status", "lp_burn_status", "burnt_status"])).lower()
    details.append(_mk_pass("burn_status", burn_status, "burn", "burn") if burn_status in BURN_VALUES else _mk_failed("burn_status", burn_status, "must be burn", "burn", missing=(burn_status == "")))

    _check_float(
        details,
        snapshot,
        "sniper_count",
        ["sniper_count", "snipers", "sniper_trader_count"],
        lambda v: v < 50 * x,
        f"< {50 * x:.6g}",
    )

    if include_platform:
        platform = _norm_str(_first_present(snapshot, ["launchpad", "platform", "source_platform", "pool_platform"]))
        details.append(_mk_pass("platform", platform, f"in {sorted(PLATFORMS)}", sorted(PLATFORMS)) if platform in PLATFORMS else _mk_failed("platform", platform, f"in {sorted(PLATFORMS)}", sorted(PLATFORMS), missing=(platform == "")))
    else:
        platform = _norm_str(_first_present(snapshot, ["launchpad", "platform", "source_platform", "pool_platform"]))

    feature_vector = {
        "x": x,
        "type": typ,
        "liquidity_usd": liquidity,
        "top_10_holder_rate": top10,
        "top1_holder_rate": top1,
        "renounced_mint": _to_int_bool(snapshot.get("renounced_mint")),
        "renounced_freeze_account": _to_int_bool(snapshot.get("renounced_freeze_account")),
        "platform": platform,
    }
    return details, feature_vector


async def run_initial_filter(snapshot: Dict[str, Any], strategy_group: Dict[str, Any], now: datetime | None = None) -> FilterResult:
    """First-stage filter for pools already returned by the strategy t-window query."""
    details, feature_vector = _evaluate_core_risk_rules(snapshot, strategy_group, include_type=True, include_platform=True)
    feature_vector.update({"t_seconds": int(strategy_group.get("t_seconds", 150))})
    return FilterResult(all(d.passed for d in details), details, feature_vector)


async def run_risk_filter(snapshot: Dict[str, Any], strategy_group: Dict[str, Any], now: datetime | None = None) -> FilterResult:
    """Reusable post-entry risk-feature filter; no pool-age/t-window condition is applied."""
    details, feature_vector = _evaluate_core_risk_rules(snapshot, strategy_group, include_type=True, include_platform=True)
    return FilterResult(all(d.passed for d in details), details, feature_vector)
