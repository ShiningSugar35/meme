"""Initial and risk filter rules for the GMGN trenches strategy.

This module is intentionally field-normalisation heavy and calculation-light:
provider code maps GMGN responses into canonical names where possible, while the
filters here accept a small set of aliases so mock data and older DB snapshots
continue to work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import math


DEFAULT_PLATFORM_SET = {
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


@dataclass(frozen=True)
class StrategyParams:
    """Runtime knobs for the current strategy band.

    x controls risk strictness and liquidity/holder bounds; t_seconds controls
    the discovery age window [t, t + 60).  These names are kept short because
    the user's strategy specification uses x/y/t explicitly.
    """

    x: float = 0.20
    t_seconds: int = 150
    allowed_platforms: Tuple[str, ...] = tuple(sorted(DEFAULT_PLATFORM_SET))

    @classmethod
    def from_strategy(cls, strategy: Optional[Dict[str, Any]] = None) -> "StrategyParams":
        strategy = strategy or {}
        x = _first_number(
            strategy,
            ("x", "risk_x", "initial_x", "filter_x", "strictness_x"),
            0.20,
        )
        t_seconds = int(
            _first_number(
                strategy,
                ("t", "t_seconds", "target_age_seconds", "min_created_age_seconds", "created_age_seconds"),
                150,
            )
            or 150
        )
        platforms_raw = _first_present(strategy, ("allowed_platforms", "platforms", "launchpad_platforms"))
        platforms = _parse_platforms(platforms_raw) or tuple(sorted(DEFAULT_PLATFORM_SET))
        return cls(x=max(0.0, float(x)), t_seconds=max(0, t_seconds), allowed_platforms=platforms)


@dataclass
class FilterResult:
    passed: bool
    reasons: List[str]
    details: Dict[str, Any]
    feature_vector: Dict[str, Any]


def _parse_platforms(value: Any) -> Tuple[str, ...]:
    if value is None:
        return tuple()
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
    elif isinstance(value, Iterable):
        parts = [str(p).strip() for p in value if str(p).strip()]
    else:
        parts = []
    return tuple(parts)


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    v = _to_float(value)
    if v is None:
        return default
    return int(v)


def _to_bool01(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "renounced", "burn", "burned"}:
        return True
    if s in {"0", "false", "no", "n", "off", "none", "null"}:
        return False
    return default


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


def _normalise_ratio(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Accept ratio fields as 0.12 or percentage fields as 12."""
    v = _to_float(value, default)
    if v is None:
        return default
    return v / 100.0 if abs(v) > 1.0 else v


def _parse_ts(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).timestamp()
    n = _to_float(value)
    if n is not None:
        # Milliseconds timestamps are common in GMGN/browser APIs.
        return n / 1000.0 if n > 10_000_000_000 else n
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).timestamp()
    except Exception:
        return None


def _created_age_seconds(data: Dict[str, Any]) -> Optional[float]:
    direct = _to_float(
        _first_present(
            data,
            (
                "created_age_seconds",
                "age_seconds",
                "seconds_since_created",
                "pool_age_seconds",
                "token_age_seconds",
            ),
        )
    )
    if direct is not None:
        return max(0.0, direct)

    ts = _parse_ts(
        _first_present(
            data,
            (
                "created_ts",
                "created_timestamp",
                "creation_timestamp",
                "open_ts",
                "pool_open_timestamp",
                "launch_timestamp",
                "created_at",
            ),
        )
    )
    if ts is None:
        return None
    return max(0.0, datetime.now(timezone.utc).timestamp() - ts)


def _has_social(data: Dict[str, Any]) -> bool:
    explicit = _to_bool01(_first_present(data, ("has_at_least_one_social", "has_social", "social")))
    if explicit is not None:
        return explicit
    for key in (
        "twitter",
        "telegram",
        "website",
        "website_url",
        "twitter_username",
        "telegram_url",
        "discord",
        "social_links",
    ):
        v = data.get(key)
        if isinstance(v, list) and len(v) > 0:
            return True
        if isinstance(v, dict) and any(v.values()):
            return True
        if isinstance(v, str) and v.strip():
            return True
    return False


def normalise_features(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Return canonical features used by initial/risk filters."""
    snapshot = snapshot or {}
    platform = str(_first_present(snapshot, ("launchpad_platform", "platform", "launchpad", "launchpad_name"), "") or "").strip()
    token_type = str(_first_present(snapshot, ("type", "latest_type", "token_type", "pool_type"), "") or "").strip()
    burn_status = str(_first_present(snapshot, ("burn_status", "pool_burn_status"), "") or "").strip().lower()
    creator_status = str(_first_present(snapshot, ("creator_token_status",), "") or "").strip().lower()

    features = {
        "type": token_type,
        "platform": platform,
        "liquidity_usd": _first_number(snapshot, ("liquidity_usd", "liquidity", "usd_liquidity", "latest_liquidity_usd"), 0.0),
        "top_10_holder_rate": _normalise_ratio(_first_present(snapshot, ("top_10_holder_rate", "top10_holder_rate", "top10_rate", "top_10_rate"))),
        "renounced_mint": _to_bool01(_first_present(snapshot, ("renounced_mint", "mint_renounced", "is_mint_renounced", "can_mint"))),
        "renounced_freeze_account": _to_bool01(_first_present(snapshot, ("renounced_freeze_account", "freeze_renounced", "is_freeze_renounced", "can_freeze"))),
        "rug_ratio": _normalise_ratio(_first_present(snapshot, ("rug_ratio", "max_rug_ratio", "rug_rate"))),
        "entrapment_ratio": _normalise_ratio(_first_present(snapshot, ("entrapment_ratio", "max_entrapment_ratio", "honeypot_ratio"))),
        "is_wash_trading": _to_bool01(_first_present(snapshot, ("is_wash_trading", "wash_trading", "wash_trading_flag")), False),
        "rat_trader_amount_rate": _normalise_ratio(_first_present(snapshot, ("rat_trader_amount_rate", "rat_trader_rate", "max_insider_ratio"))),
        "suspected_insider_hold_rate": _normalise_ratio(_first_present(snapshot, ("suspected_insider_hold_rate", "insider_hold_rate", "insider_holder_rate"))),
        "bundler_trader_amount_rate": _normalise_ratio(_first_present(snapshot, ("bundler_trader_amount_rate", "bundler_rate", "max_bundler_rate"))),
        "fresh_wallet_rate": _normalise_ratio(_first_present(snapshot, ("fresh_wallet_rate", "fresh_wallet_amount_rate", "fresh_rate"))),
        "sell_tax": _normalise_ratio(_first_present(snapshot, ("sell_tax", "sell_tax_rate", "transfer_tax"))),
        "has_at_least_one_social": _has_social(snapshot),
        "creator_token_status": creator_status,
        "dev_team_hold_rate": _normalise_ratio(_first_present(snapshot, ("dev_team_hold_rate", "dev_hold_rate", "creator_hold_rate", "dev_holder_rate"))),
        "burn_status": burn_status,
        "sniper_count": _to_int(_first_present(snapshot, ("sniper_count", "snipers", "sniper_wallet_count")), 0),
        "created_age_seconds": _created_age_seconds(snapshot),
        "top1_holder_rate": _normalise_ratio(_first_present(snapshot, ("top1_holder_rate", "top_1_holder_rate", "largest_addr_type0_holder_rate"))),
    }

    # Some APIs expose "can_mint/can_freeze" rather than renounced flags. When
    # those aliases are used, invert them to keep the canonical value meaningful.
    if features["renounced_mint"] is not None and "can_mint" in snapshot and "renounced_mint" not in snapshot:
        features["renounced_mint"] = not bool(features["renounced_mint"])
    if features["renounced_freeze_account"] is not None and "can_freeze" in snapshot and "renounced_freeze_account" not in snapshot:
        features["renounced_freeze_account"] = not bool(features["renounced_freeze_account"])

    return features


def _fail(reasons: List[str], name: str, detail: Dict[str, Any]) -> None:
    reasons.append(name)
    detail[name] = False


def _pass(detail: Dict[str, Any], name: str) -> None:
    detail[name] = True


def _lt(value: Optional[float], threshold: float) -> bool:
    return value is not None and value < threshold


def _between_exclusive(value: Optional[float], low: float, high: float) -> bool:
    return value is not None and low < value < high


def _evaluate(snapshot: Dict[str, Any], params: StrategyParams, *, include_creation_window: bool) -> FilterResult:
    f = normalise_features(snapshot)
    x = params.x
    reasons: List[str] = []
    checks: Dict[str, Any] = {}

    type_ok = f["type"] == "new_creation"
    if type_ok:
        checks["type_new_creation"] = True
    else:
        _fail(reasons, "type_new_creation", checks)

    min_liquidity = max(0.0, 10_000.0 - 20_000.0 * x)
    if f["liquidity_usd"] >= min_liquidity:
        _pass(checks, "min_liquidity_usd")
    else:
        _fail(reasons, "min_liquidity_usd", checks)

    top10_low = 0.175 - 0.15 * x
    top10_high = 0.25 + 0.25 * x
    if _between_exclusive(f["top_10_holder_rate"], top10_low, top10_high):
        _pass(checks, "top_10_holder_rate_range")
    else:
        _fail(reasons, "top_10_holder_rate_range", checks)

    if f["renounced_mint"] is True:
        _pass(checks, "renounced_mint")
    else:
        _fail(reasons, "renounced_mint", checks)

    if f["renounced_freeze_account"] is True:
        _pass(checks, "renounced_freeze_account")
    else:
        _fail(reasons, "renounced_freeze_account", checks)

    risk_threshold = -0.05 + x
    for key in ("rug_ratio", "entrapment_ratio", "rat_trader_amount_rate", "bundler_trader_amount_rate"):
        if _lt(f[key], risk_threshold):
            _pass(checks, key)
        else:
            _fail(reasons, key, checks)

    if f["is_wash_trading"] is False:
        _pass(checks, "is_wash_trading")
    else:
        _fail(reasons, "is_wash_trading", checks)

    if _lt(f["suspected_insider_hold_rate"], x):
        _pass(checks, "suspected_insider_hold_rate")
    else:
        _fail(reasons, "suspected_insider_hold_rate", checks)

    if _lt(f["fresh_wallet_rate"], 0.13 + 0.1 * x):
        _pass(checks, "fresh_wallet_rate")
    else:
        _fail(reasons, "fresh_wallet_rate", checks)

    if _lt(f["sell_tax"], 0.1 * x):
        _pass(checks, "sell_tax")
    else:
        _fail(reasons, "sell_tax", checks)

    if x < 0.15:
        if f["has_at_least_one_social"] is True:
            _pass(checks, "has_at_least_one_social")
        else:
            _fail(reasons, "has_at_least_one_social", checks)
    else:
        checks["has_at_least_one_social"] = "not_required"

    dev_hold_threshold = 0.03 + 0.1 * x
    # Strict by request: creator_token_status itself is not compatibility-expanded.
    creator_ok = f["creator_token_status"] == "creator_close" or _lt(f["dev_team_hold_rate"], dev_hold_threshold)
    if creator_ok:
        _pass(checks, "creator_token_status_or_dev_team_hold_rate")
    else:
        _fail(reasons, "creator_token_status_or_dev_team_hold_rate", checks)

    if f["burn_status"] == "burn":
        _pass(checks, "burn_status")
    else:
        _fail(reasons, "burn_status", checks)

    if (f["sniper_count"] or 0) < 50.0 * x:
        _pass(checks, "sniper_count")
    else:
        _fail(reasons, "sniper_count", checks)

    if include_creation_window:
        age = f["created_age_seconds"]
        age_ok = age is not None and params.t_seconds <= age < params.t_seconds + 60
        if age_ok:
            _pass(checks, "created_age_window")
        else:
            _fail(reasons, "created_age_window", checks)
    else:
        checks["created_age_window"] = "not_checked_on_risk_recheck"

    if f["platform"] in params.allowed_platforms:
        _pass(checks, "launchpad_platform")
    else:
        _fail(reasons, "launchpad_platform", checks)

    thresholds = {
        "x": x,
        "t_seconds": params.t_seconds,
        "min_liquidity_usd": min_liquidity,
        "top_10_holder_rate_low": top10_low,
        "top_10_holder_rate_high": top10_high,
        "risk_ratio_threshold": risk_threshold,
        "fresh_wallet_rate_threshold": 0.13 + 0.1 * x,
        "sell_tax_threshold": 0.1 * x,
        "dev_team_hold_rate_threshold": dev_hold_threshold,
        "sniper_count_threshold": 50.0 * x,
        "allowed_platforms": list(params.allowed_platforms),
        "include_creation_window": include_creation_window,
    }

    details = {"checks": checks, "reasons": reasons, "thresholds": thresholds}
    return FilterResult(not reasons, reasons, details, f)


def _result_to_dict(result: FilterResult) -> Dict[str, Any]:
    return {
        "passed": result.passed,
        "reasons": result.reasons,
        "details": result.details,
        "feature_vector": result.feature_vector,
    }


async def run_initial_filter(snapshot: Dict[str, Any], strategy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Evaluate the first-stage GMGN trenches risk screen."""
    params = StrategyParams.from_strategy(strategy)
    return _result_to_dict(_evaluate(snapshot, params, include_creation_window=True))


async def run_risk_filter(snapshot: Dict[str, Any], strategy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Evaluate post-entry risk rechecks.

    The age window is intentionally skipped: once a position is open, it would be
    nonsensical to close it simply because the pool aged out of the discovery
    window. Other risk gates remain the same.
    """
    params = StrategyParams.from_strategy(strategy)
    return _result_to_dict(_evaluate(snapshot, params, include_creation_window=False))


def _top1_threshold(strategy_or_x: Any = None) -> float:
    if isinstance(strategy_or_x, dict):
        x = StrategyParams.from_strategy(strategy_or_x).x
    else:
        x = _to_float(strategy_or_x, 0.20) or 0.20
    return 0.048 + 0.01 * max(0.0, float(x))


def extract_top1_addr_type0_holder_rate(holders: Any) -> Optional[float]:
    """Return the largest holder rate among normal holders (`addr_type == 0`)."""
    if isinstance(holders, dict):
        holders = holders.get("holders") or holders.get("data") or holders.get("list") or []
    if not isinstance(holders, list):
        return None

    best: Optional[float] = None
    for h in holders:
        if not isinstance(h, dict):
            continue
        addr_type = _to_int(_first_present(h, ("addr_type", "address_type", "holder_type")), 0)
        if addr_type != 0:
            continue
        rate = _normalise_ratio(_first_present(h, ("rate", "holder_rate", "hold_rate", "amount_rate", "balance_rate", "percentage", "percent")))
        if rate is None:
            continue
        if best is None or rate > best:
            best = rate
    return best


async def evaluate_top1_holder_filter(top_holders: Any, strategy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    rate = extract_top1_addr_type0_holder_rate(top_holders)
    threshold = _top1_threshold(strategy or {})
    passed = rate is not None and rate < threshold
    return {
        "passed": passed,
        "top1_holder_rate": rate,
        "threshold": threshold,
        "reason": None if passed else "top1_holder_rate_failed",
    }
