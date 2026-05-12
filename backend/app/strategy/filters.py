from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class RuleCheck:
    rule_name: str
    passed: bool
    actual_value: Any
    threshold_expression: str
    threshold_value: Any
    missing_field: bool = False
    reason: str | None = None


@dataclass
class FilterResult:
    passed: bool
    details: List[RuleCheck]
    feature_vector: Dict[str, Any]


PLATFORMS = {
    "Pump.fun",
    "Moonshot",
    "moonshot_app",
    "letsbonk",
    "memoo",
    "token_mill",
    "jup_studio",
    "bags",
    "believe",
    "heaven",
}


_BOOL_TRUE_VALUES = {1, True, "1", "true", "True", "TRUE", "yes", "YES", "y", "Y"}
_BOOL_FALSE_VALUES = {0, False, "0", "false", "False", "FALSE", "no", "NO", "n", "N"}


def _mk_failed(rule_name, actual, expr, thresh, missing=False, reason=None):
    return RuleCheck(rule_name, False, actual, expr, thresh, missing, reason)


def _mk_pass(rule_name, actual, expr, thresh):
    return RuleCheck(rule_name, True, actual, expr, thresh, False, None)


def _first_present(snapshot: Dict[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        val = snapshot.get(key)
        if val is not None and val != "":
            return val
    return default


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int_bool(value: Any) -> Optional[int]:
    if value in _BOOL_TRUE_VALUES:
        return 1
    if value in _BOOL_FALSE_VALUES:
        return 0
    if value is None or value == "":
        return None
    try:
        return 1 if int(value) == 1 else 0
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _check_eq_bool(details: List[RuleCheck], snapshot: Dict[str, Any], field: str, expected: int):
    raw = snapshot.get(field)
    val = _to_int_bool(raw)
    if val is None:
        details.append(_mk_failed(field, raw, f"== {expected}", expected, missing=True))
    else:
        details.append(
            _mk_pass(field, val, f"== {expected}", expected)
            if val == expected
            else _mk_failed(field, val, f"== {expected}", expected)
        )


def _check_lt(details: List[RuleCheck], snapshot: Dict[str, Any], rule_name: str, field: str, expr_value: float):
    raw = snapshot.get(field)
    val = _to_float(raw)
    if val is None:
        details.append(_mk_failed(rule_name, raw, f"< {expr_value}", expr_value, missing=True))
    else:
        details.append(
            _mk_pass(rule_name, val, f"< {expr_value}", expr_value)
            if val < expr_value
            else _mk_failed(rule_name, val, f"< {expr_value}", expr_value)
        )


def _evaluate_core_risk_rules(
    snapshot: Dict[str, Any],
    strategy_group: Dict[str, Any],
    *,
    include_type: bool = True,
    include_platform: bool = True,
) -> tuple[List[RuleCheck], Dict[str, Any]]:
    details: List[RuleCheck] = []
    x = float(strategy_group.get("x", 0.2))

    typ = _first_present(snapshot, ["type", "latest_type"])
    if include_type:
        if typ is None:
            details.append(_mk_failed("type_is_new_creation", None, "== 'new_creation'", "new_creation", missing=True))
        elif typ == "new_creation":
            details.append(_mk_pass("type_is_new_creation", typ, "== 'new_creation'", "new_creation"))
        else:
            details.append(_mk_failed("type_is_new_creation", typ, "== 'new_creation'", "new_creation"))

    liquidity = _to_float(_first_present(snapshot, ["liquidity_usd", "latest_liquidity_usd"]))
    thresh_liq = 13000 - 20000 * x
    if liquidity is None:
        details.append(_mk_failed("liquidity_usd", None, ">= 13000 - 20000 * x", thresh_liq, missing=True))
    else:
        details.append(
            _mk_pass("liquidity_usd", liquidity, ">= 13000 - 20000 * x", thresh_liq)
            if liquidity >= thresh_liq
            else _mk_failed("liquidity_usd", liquidity, ">= 13000 - 20000 * x", thresh_liq)
        )

    top10 = _to_float(snapshot.get("top_10_holder_rate"))
    tmin = 0.175 - 0.15 * x
    tmax = 0.25 + 0.25 * x
    if top10 is None:
        details.append(_mk_failed("top_10_holder_rate", snapshot.get("top_10_holder_rate"), f"{tmin} < v < {tmax}", (tmin, tmax), missing=True))
    else:
        details.append(
            _mk_pass("top_10_holder_rate", top10, f"{tmin} < v < {tmax}", (tmin, tmax))
            if (tmin < top10 < tmax)
            else _mk_failed("top_10_holder_rate", top10, f"{tmin} < v < {tmax}", (tmin, tmax))
        )

    top1 = _to_float(snapshot.get("top1_holder_rate"))
    t1min = 0.0335 - 0.02 * x
    t1max = 0.044 + 0.04 * x
    if top1 is None:
        details.append(_mk_failed("top1_holder_rate", snapshot.get("top1_holder_rate"), f"{t1min} < v < {t1max}", (t1min, t1max), missing=True))
    else:
        details.append(
            _mk_pass("top1_holder_rate", top1, f"{t1min} < v < {t1max}", (t1min, t1max))
            if (t1min < top1 < t1max)
            else _mk_failed("top1_holder_rate", top1, f"{t1min} < v < {t1max}", (t1min, t1max))
        )

    _check_eq_bool(details, snapshot, "renounced_mint", 1)
    _check_eq_bool(details, snapshot, "renounced_freeze_account", 1)

    _check_lt(details, snapshot, "max_rug_ratio", "max_rug_ratio", -0.05 + x)
    _check_lt(details, snapshot, "max_insider_ratio", "max_insider_ratio", -0.05 + x)
    _check_lt(details, snapshot, "max_entrapment_ratio", "max_entrapment_ratio", -0.05 + x)

    wash = _to_int_bool(snapshot.get("is_wash_trading"))
    if wash is None:
        details.append(_mk_failed("is_wash_trading", snapshot.get("is_wash_trading"), "== 0", 0, missing=True))
    else:
        details.append(_mk_pass("is_wash_trading", wash, "== 0", 0) if wash == 0 else _mk_failed("is_wash_trading", wash, "== 0", 0))

    _check_lt(details, snapshot, "rat_trader_amount_rate", "rat_trader_amount_rate", -0.05 + x)

    si = _to_float(snapshot.get("suspected_insider_hold_rate"))
    if si is None:
        details.append(_mk_failed("suspected_insider_hold_rate", snapshot.get("suspected_insider_hold_rate"), f"< {x}", x, missing=True))
    else:
        details.append(_mk_pass("suspected_insider_hold_rate", si, f"< {x}", x) if si < x else _mk_failed("suspected_insider_hold_rate", si, f"< {x}", x))

    _check_lt(details, snapshot, "max_bundler_rate", "max_bundler_rate", -0.05 + x)

    fwr = _to_float(snapshot.get("fresh_wallet_rate"))
    fwr_thresh = 0.13 + 0.1 * x
    if fwr is None:
        details.append(_mk_failed("fresh_wallet_rate", snapshot.get("fresh_wallet_rate"), f"< {fwr_thresh}", fwr_thresh, missing=True))
    else:
        details.append(_mk_pass("fresh_wallet_rate", fwr, f"< {fwr_thresh}", fwr_thresh) if fwr < fwr_thresh else _mk_failed("fresh_wallet_rate", fwr, f"< {fwr_thresh}", fwr_thresh))

    sell_tax = _to_float(snapshot.get("sell_tax"))
    sell_thresh = 0.1 * x
    if sell_tax is None:
        details.append(_mk_failed("sell_tax", snapshot.get("sell_tax"), f"< {sell_thresh}", sell_thresh, missing=True))
    else:
        details.append(_mk_pass("sell_tax", sell_tax, f"< {sell_thresh}", sell_thresh) if sell_tax < sell_thresh else _mk_failed("sell_tax", sell_tax, f"< {sell_thresh}", sell_thresh))

    has_social = _to_int_bool(snapshot.get("has_social"))
    if x <= 0.3:
        if has_social is None:
            details.append(_mk_failed("has_social", snapshot.get("has_social"), "== 1 (required when x<=0.3)", 1, missing=True))
        else:
            details.append(_mk_pass("has_social", has_social, "== 1", 1) if has_social == 1 else _mk_failed("has_social", has_social, "== 1", 1))
    else:
        details.append(_mk_pass("has_social", has_social, "optional (x>0.3)", has_social))

    creator_status = snapshot.get("creator_token_status")
    dev_hold = _to_float(snapshot.get("dev_team_hold_rate"))
    dev_thresh = 0.04 + 0.1 * x
    cond_creator = creator_status == "creator_close"
    cond_dev = dev_hold is not None and dev_hold < dev_thresh
    if cond_creator or cond_dev:
        details.append(_mk_pass("creator_or_dev_hold", (creator_status, dev_hold), f"creator=='creator_close' OR dev_team_hold_rate < {dev_thresh}", ("creator_close", dev_thresh)))
    else:
        details.append(_mk_failed("creator_or_dev_hold", (creator_status, dev_hold), f"creator=='creator_close' OR dev_team_hold_rate < {dev_thresh}", ("creator_close", dev_thresh)))

    burn = _to_float(snapshot.get("dev_token_burn_ratio"))
    burn_thresh = 1 - 0.1 * x
    if burn is None:
        details.append(_mk_failed("dev_token_burn_ratio", snapshot.get("dev_token_burn_ratio"), f"> {burn_thresh}", burn_thresh, missing=True))
    else:
        details.append(_mk_pass("dev_token_burn_ratio", burn, f"> {burn_thresh}", burn_thresh) if burn > burn_thresh else _mk_failed("dev_token_burn_ratio", burn, f"> {burn_thresh}", burn_thresh))

    sniper = _to_float(snapshot.get("sniper_count"))
    sniper_thresh = 50 * x
    if sniper is None:
        details.append(_mk_failed("sniper_count", snapshot.get("sniper_count"), f"< {sniper_thresh}", sniper_thresh, missing=True))
    else:
        details.append(_mk_pass("sniper_count", sniper, f"< {sniper_thresh}", sniper_thresh) if sniper < sniper_thresh else _mk_failed("sniper_count", sniper, f"< {sniper_thresh}", sniper_thresh))

    platform = _first_present(snapshot, ["platform", "launchpad"])
    if include_platform:
        if platform is None:
            details.append(_mk_failed("platform", None, f"in {PLATFORMS}", PLATFORMS, missing=True))
        else:
            details.append(_mk_pass("platform", platform, f"in {PLATFORMS}", PLATFORMS) if platform in PLATFORMS else _mk_failed("platform", platform, f"in {PLATFORMS}", PLATFORMS))

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


async def run_initial_filter(snapshot: Dict[str, Any], strategy_group: Dict[str, Any], now: datetime) -> FilterResult:
    """Initial trench filter.

    This implements the first-stage rules from the design: core safety/holder rules,
    pool-age window [t, t+60], and allowed launchpad/platform.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    details, feature_vector = _evaluate_core_risk_rules(snapshot, strategy_group, include_type=True, include_platform=True)

    pool_created_at = _first_present(snapshot, ["pool_created_at", "created_at"])
    pc = _parse_dt(pool_created_at)
    if pc is None:
        details.append(_mk_failed("pool_created_at", pool_created_at, "ISO timestamp required", "required", missing=True))
        return FilterResult(False, details, feature_vector)

    delta = (now - pc).total_seconds()
    tsec = int(strategy_group.get("t_seconds", 150))
    in_window = tsec <= delta <= tsec + 60
    details.append(
        _mk_pass("time_window", delta, f"in [{tsec},{tsec + 60}]", (tsec, tsec + 60))
        if in_window
        else _mk_failed("time_window", delta, f"in [{tsec},{tsec + 60}]", (tsec, tsec + 60))
    )

    feature_vector.update({"delta_seconds": delta, "t_seconds": tsec})
    passed = all(d.passed for d in details)
    return FilterResult(passed, details, feature_vector)


async def run_risk_filter(snapshot: Dict[str, Any], strategy_group: Dict[str, Any], now: datetime | None = None) -> FilterResult:
    """Reusable risk-feature filter for post-entry risk stop.

    It intentionally does not apply the pool-age window, because an open position will
    naturally age beyond [t, t+60]. The position-risk runner should call this and exit
    when passed is False.
    """
    details, feature_vector = _evaluate_core_risk_rules(snapshot, strategy_group, include_type=True, include_platform=True)
    passed = all(d.passed for d in details)
    return FilterResult(passed, details, feature_vector)
