from dataclasses import dataclass
from typing import Any, Dict, List
from datetime import datetime


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


PLATFORMS = {"Pump.fun", "Moonshot", "moonshot_app", "letsbonk", "memoo", "token_mill", "jup_studio", "bags", "believe", "heaven"}


def _mk_failed(rule_name, actual, expr, thresh, missing=False, reason=None):
    return RuleCheck(rule_name, False, actual, expr, thresh, missing, reason)


def _mk_pass(rule_name, actual, expr, thresh):
    return RuleCheck(rule_name, True, actual, expr, thresh, False, None)


async def run_initial_filter(snapshot: Dict[str, Any], strategy_group: Dict[str, Any], now: datetime) -> FilterResult:
    details: List[RuleCheck] = []
    x = float(strategy_group.get("x", 0.2))

    # type == new_creation
    typ = snapshot.get("type")
    if typ is None:
        details.append(_mk_failed("type_is_new_creation", None, "== 'new_creation'", "new_creation", missing=True))
    else:
        if typ == "new_creation":
            details.append(_mk_pass("type_is_new_creation", typ, "== 'new_creation'", "new_creation"))
        else:
            details.append(_mk_failed("type_is_new_creation", typ, "== 'new_creation'", "new_creation"))

    # liquidity_usd
    liquidity = snapshot.get("liquidity_usd")
    thresh_liq = 13000 - 20000 * x
    if liquidity is None:
        details.append(_mk_failed("liquidity_usd", None, ">= 13000 - 20000 * x", thresh_liq, missing=True))
    else:
        details.append(_mk_pass("liquidity_usd", liquidity, ">= 13000 - 20000 * x", thresh_liq) if liquidity >= thresh_liq else _mk_failed("liquidity_usd", liquidity, ">= 13000 - 20000 * x", thresh_liq))

    # top_10_holder_rate
    top10 = snapshot.get("top_10_holder_rate")
    tmin = 0.175 - 0.15 * x
    tmax = 0.25 + 0.25 * x
    if top10 is None:
        details.append(_mk_failed("top_10_holder_rate", None, f"{tmin} < v < {tmax}", (tmin, tmax), missing=True))
    else:
        details.append(_mk_pass("top_10_holder_rate", top10, f"{tmin} < v < {tmax}", (tmin, tmax)) if (tmin < top10 < tmax) else _mk_failed("top_10_holder_rate", top10, f"{tmin} < v < {tmax}", (tmin, tmax)))

    # top1_holder_rate
    top1 = snapshot.get("top1_holder_rate")
    t1min = 0.0335 - 0.02 * x
    t1max = 0.044 + 0.04 * x
    if top1 is None:
        details.append(_mk_failed("top1_holder_rate", None, f"{t1min} < v < {t1max}", (t1min, t1max), missing=True))
    else:
        details.append(_mk_pass("top1_holder_rate", top1, f"{t1min} < v < {t1max}", (t1min, t1max)) if (t1min < top1 < t1max) else _mk_failed("top1_holder_rate", top1, f"{t1min} < v < {t1max}", (t1min, t1max)))

    # renounced_mint
    ren_mint = snapshot.get("renounced_mint")
    if ren_mint is None:
        details.append(_mk_failed("renounced_mint", None, "== 1", 1, missing=True))
    else:
        details.append(_mk_pass("renounced_mint", ren_mint, "== 1", 1) if ren_mint == 1 else _mk_failed("renounced_mint", ren_mint, "== 1", 1))

    # renounced_freeze_account
    ren_freeze = snapshot.get("renounced_freeze_account")
    if ren_freeze is None:
        details.append(_mk_failed("renounced_freeze_account", None, "== 1", 1, missing=True))
    else:
        details.append(_mk_pass("renounced_freeze_account", ren_freeze, "== 1", 1) if ren_freeze == 1 else _mk_failed("renounced_freeze_account", ren_freeze, "== 1", 1))

    # various ratios
    def check_lt(name, field, expr_value):
        val = snapshot.get(field)
        if val is None:
            details.append(_mk_failed(name, None, f"< {expr_value}", expr_value, missing=True))
        else:
            details.append(_mk_pass(name, val, f"< {expr_value}", expr_value) if val < expr_value else _mk_failed(name, val, f"< {expr_value}", expr_value))

    check_lt("max_rug_ratio", "max_rug_ratio", -0.05 + x)
    check_lt("max_insider_ratio", "max_insider_ratio", -0.05 + x)
    check_lt("max_entrapment_ratio", "max_entrapment_ratio", -0.05 + x)
    # is_wash_trading == 0
    wash = snapshot.get("is_wash_trading")
    if wash is None:
        details.append(_mk_failed("is_wash_trading", None, "== 0", 0, missing=True))
    else:
        details.append(_mk_pass("is_wash_trading", wash, "== 0", 0) if wash == 0 else _mk_failed("is_wash_trading", wash, "== 0", 0))

    check_lt("rat_trader_amount_rate", "rat_trader_amount_rate", -0.05 + x)
    # suspected_insider_hold_rate < x
    si = snapshot.get("suspected_insider_hold_rate")
    if si is None:
        details.append(_mk_failed("suspected_insider_hold_rate", None, f"< {x}", x, missing=True))
    else:
        details.append(_mk_pass("suspected_insider_hold_rate", si, f"< {x}", x) if si < x else _mk_failed("suspected_insider_hold_rate", si, f"< {x}", x))

    check_lt("max_bundler_rate", "max_bundler_rate", -0.05 + x)

    # fresh_wallet_rate
    fwr = snapshot.get("fresh_wallet_rate")
    fwr_thresh = 0.13 + 0.1 * x
    if fwr is None:
        details.append(_mk_failed("fresh_wallet_rate", None, f"< {fwr_thresh}", fwr_thresh, missing=True))
    else:
        details.append(_mk_pass("fresh_wallet_rate", fwr, f"< {fwr_thresh}", fwr_thresh) if fwr < fwr_thresh else _mk_failed("fresh_wallet_rate", fwr, f"< {fwr_thresh}", fwr_thresh))

    # sell_tax
    sell_tax = snapshot.get("sell_tax")
    sell_thresh = 0.1 * x
    if sell_tax is None:
        details.append(_mk_failed("sell_tax", None, f"< {sell_thresh}", sell_thresh, missing=True))
    else:
        details.append(_mk_pass("sell_tax", sell_tax, f"< {sell_thresh}", sell_thresh) if sell_tax < sell_thresh else _mk_failed("sell_tax", sell_tax, f"< {sell_thresh}", sell_thresh))

    # has_social if x <= 0.3
    has_social = snapshot.get("has_social")
    if x <= 0.3:
        if has_social is None:
            details.append(_mk_failed("has_social", None, "== 1 (required when x<=0.3)", 1, missing=True))
        else:
            details.append(_mk_pass("has_social", has_social, "== 1", 1) if has_social == 1 else _mk_failed("has_social", has_social, "== 1", 1))
    else:
        # not mandatory; mark as pass if present or not
        details.append(_mk_pass("has_social", has_social, "optional (x>0.3)", has_social))

    # creator_token_status == "creator_close" OR dev_team_hold_rate < 0.04 + 0.1 * x
    creator_status = snapshot.get("creator_token_status")
    dev_hold = snapshot.get("dev_team_hold_rate")
    dev_thresh = 0.04 + 0.1 * x
    cond_creator = (creator_status == "creator_close") if creator_status is not None else False
    cond_dev = (dev_hold is not None and dev_hold < dev_thresh)
    if cond_creator or cond_dev:
        details.append(_mk_pass("creator_or_dev_hold", (creator_status, dev_hold), f"creator=='creator_close' OR dev_team_hold_rate < {dev_thresh}", ("creator_close", dev_thresh)))
    else:
        details.append(_mk_failed("creator_or_dev_hold", (creator_status, dev_hold), f"creator=='creator_close' OR dev_team_hold_rate < {dev_thresh}", ("creator_close", dev_thresh)))

    # dev_token_burn_ratio > 1 - 0.1 * x
    burn = snapshot.get("dev_token_burn_ratio")
    burn_thresh = 1 - 0.1 * x
    if burn is None:
        details.append(_mk_failed("dev_token_burn_ratio", None, f"> {burn_thresh}", burn_thresh, missing=True))
    else:
        details.append(_mk_pass("dev_token_burn_ratio", burn, f"> {burn_thresh}", burn_thresh) if burn > burn_thresh else _mk_failed("dev_token_burn_ratio", burn, f"> {burn_thresh}", burn_thresh))

    # sniper_count < 50 * x
    sniper = snapshot.get("sniper_count")
    sniper_thresh = 50 * x
    if sniper is None:
        details.append(_mk_failed("sniper_count", None, f"< {sniper_thresh}", sniper_thresh, missing=True))
    else:
        details.append(_mk_pass("sniper_count", sniper, f"< {sniper_thresh}", sniper_thresh) if sniper < sniper_thresh else _mk_failed("sniper_count", sniper, f"< {sniper_thresh}", sniper_thresh))

    # pool_created_at and time window
    pool_created_at = snapshot.get("pool_created_at")
    if not pool_created_at:
        details.append(_mk_failed("pool_created_at", None, "ISO timestamp required", "required", missing=True))
        return FilterResult(False, details, {})
    try:
        pc = datetime.fromisoformat(pool_created_at)
    except Exception:
        details.append(_mk_failed("pool_created_at_parse", pool_created_at, "ISO timestamp", "isoformat", missing=True))
        return FilterResult(False, details, {})
    delta = (now - pc).total_seconds()
    tsec = int(strategy_group.get("t_seconds", 150))
    in_window = (tsec <= delta <= tsec + 60)
    details.append(_mk_pass("time_window", delta, f"in [{tsec},{tsec+60}]", (tsec, tsec + 60)) if in_window else _mk_failed("time_window", delta, f"in [{tsec},{tsec+60}]", (tsec, tsec + 60)))

    # platform
    platform = snapshot.get("platform")
    if platform is None:
        details.append(_mk_failed("platform", None, f"in {PLATFORMS}", PLATFORMS, missing=True))
    else:
        details.append(_mk_pass("platform", platform, f"in {PLATFORMS}", PLATFORMS) if platform in PLATFORMS else _mk_failed("platform", platform, f"in {PLATFORMS}", PLATFORMS))

    # aggregate pass: all rules must pass
    passed = all(d.passed for d in details)

    feature_vector = {
        "liquidity_usd": liquidity,
        "top_10_holder_rate": top10,
        "top1_holder_rate": top1,
        "delta_seconds": delta,
        "platform": platform,
    }

    return FilterResult(passed, details, feature_vector)
