"""Unified exit rules — single source of truth for all position exits.

No other file should hardcode TP/SL thresholds or reason codes.
All automated exits (price-based, completed, dust) are decided here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
import json
import math
import os

# ---------------------------------------------------------------------------
# Constants — change these, NOT the runner logic
# ---------------------------------------------------------------------------
HARD_TP_FIRST_MULTIPLE = 1.6
HARD_TP_FULL_MULTIPLE = 2.1
HARD_TP_RETRACE_MULTIPLE = 1.5
HARD_SL_FULL_MULTIPLE = 0.75
HARD_TP_FIRST_EXIT_PCT = 0.5
FULL_EXIT_PCT = 1.0

DUST_FORCE_EXIT_USD_DEFAULT = 12.5

# ---------------------------------------------------------------------------
# Unified EXIT_REASON_LABELS — every backend & frontend label map derives from this
# ---------------------------------------------------------------------------
EXIT_REASON_LABELS: Dict[str, str] = {
    "HARD_TP_160": "硬止盈：价格超过 1.6x，撤仓50%",
    "HARD_TP_210": "硬止盈：价格超过 2.1x，全部撤仓",
    "HARD_TP_160_RETRACE": "硬止盈回撤：已超过1.6x后回撤到1.5x以下，全部撤仓",
    "HARD_SL_75": "硬止损：价格低于 0.75x，全部撤仓",
    "COMPLETED": "池子 type 变为 completed，全部撤仓",
    "DULL_DROP_SL": "阴跌止损：1h和5m涨幅均<1%，全部撤仓",
    "LOW_ACTIVITY_SL": "活跃度止损：1h交易<7次且1h涨幅<5%，全部撤仓",
    "RISK_RECHECK_FAILED": "持仓风控复查失败",
    "DUST_FORCE_EXIT": "尘埃仓强制清仓",
    "RISK_DATA_UNAVAILABLE_EXIT": "风控数据连续异常，撤仓",
    "MANUAL_SELL": "手动卖出",
}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------
@dataclass
class ExitReason:
    name: str
    desired_exit_pct: float
    detail: Dict[str, Any]


@dataclass
class ExitDecision:
    should_exit: bool
    exit_pct: float
    reasons: List[ExitReason]
    emergency: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _env_float(name: str, default: float) -> float:
    v = _to_float(os.getenv(name))
    return default if v is None else v


def normalize_gmgn_percent_change(value: Any) -> Optional[float]:
    """Convert GMGN percentage value to decimal ratio (e.g. 1.2 → 0.012).

    GMGN's price_change_percent1h etc. are always percentage numbers
    (e.g. 1.2 for 1.2%, 0.5 for 0.5%), so we always divide by 100.
    Path B/C in soft-stop fallback compute their own decimal ratio and
    do NOT call this function.
    """
    v = _to_float(value)
    if v is None:
        return None
    return v / 100.0


def _executed_exit_rules(position: Dict[str, Any]) -> Set[str]:
    raw = position.get("executed_exit_rules_json") or "[]"
    if isinstance(raw, list):
        return {str(x) for x in raw}
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return {str(x) for x in data}
    except Exception:
        pass
    return set()


def _token_type(position: Dict[str, Any], latest_snapshot: Dict[str, Any]) -> Optional[str]:
    for container in (latest_snapshot or {}, position or {}):
        for key in ("type", "token_type", "latest_token_type", "latest_type"):
            value = container.get(key)
            if value:
                return str(value)
    return None


def _append_reason(
    reasons: List[ExitReason],
    executed: Set[str],
    name: str,
    desired_exit_pct: float,
    detail: Optional[Dict[str, Any]] = None,
    *,
    repeatable: bool = False,
) -> None:
    if not repeatable and name in executed:
        return
    reasons.append(ExitReason(name, max(0.0, min(1.0, desired_exit_pct)), detail or {}))


def _current_price_usd(tick: Dict[str, Any], position: Dict[str, Any]) -> Optional[float]:
    return _to_float(
        tick.get("price_usd")
        or tick.get("price_sol")
        or tick.get("latest_price_usd")
        or tick.get("current_price_usd")
        or position.get("last_fill_price_usd")
        or position.get("entry_price_usd")
        or position.get("entry_price_sol")
        or tick.get("price")
    )


# ---------------------------------------------------------------------------
# Core decision
# ---------------------------------------------------------------------------
async def decide_exit(
    position: Dict[str, Any],
    tick: Dict[str, Any],
    rolling_60s: Dict[str, Any],
    latest_snapshot: Dict[str, Any],
    now: Optional[datetime] = None,
    dust_force_exit_usd: Optional[float] = None,
) -> ExitDecision:
    """Decide whether an open position should be exited.

    Priority (highest first):
      A. completed  type → full exit
      B. >2.1x     → full exit (HARD_TP_210)
      C. retrace   → full exit (HARD_TP_160_RETRACE) after HARD_TP_160 hit
      D. <0.75x    → full exit (HARD_SL_75)
      E. >1.6x     → 50% exit (HARD_TP_160)
      F. dust      → full exit (DUST_FORCE_EXIT)
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    position = position or {}
    tick = tick or {}
    rolling_60s = rolling_60s or {}
    latest_snapshot = latest_snapshot or {}

    reasons: List[ExitReason] = []
    executed = _executed_exit_rules(position)

    entry_price = _to_float(position.get("entry_price_usd") or position.get("entry_price_sol"))
    current_price_usd = _current_price_usd(tick, position)

    remaining_value_usd = _to_float(tick.get("remaining_value_usd") or position.get("remaining_value_usd"))
    remaining_token_amount = _to_float(position.get("remaining_token_amount"))

    if remaining_value_usd is None and remaining_token_amount is not None and current_price_usd is not None:
        remaining_value_usd = remaining_token_amount * current_price_usd

    # ---- A. Completed ----
    token_type = _token_type(position, latest_snapshot)
    if token_type == "completed":
        _append_reason(reasons, executed, "COMPLETED", FULL_EXIT_PCT, {"type": token_type}, repeatable=True)

    # ---- B-E. Hard TP/SL  (only if both prices valid) ----
    if entry_price and entry_price > 0 and current_price_usd and current_price_usd > 0:
        multiple = current_price_usd / entry_price

        # B. >2.1x full exit
        if multiple > HARD_TP_FULL_MULTIPLE and "HARD_TP_210" not in executed:
            _append_reason(
                reasons, executed, "HARD_TP_210", FULL_EXIT_PCT,
                {"multiple": multiple, "entry_price_usd": entry_price, "current_price_usd": current_price_usd},
            )

        # C. Retrace after 1.6x hit
        if (
            "HARD_TP_160" in executed
            and multiple < HARD_TP_RETRACE_MULTIPLE
            and "HARD_TP_160_RETRACE" not in executed
        ):
            _append_reason(
                reasons, executed, "HARD_TP_160_RETRACE", FULL_EXIT_PCT,
                {"multiple": multiple, "entry_price_usd": entry_price, "current_price_usd": current_price_usd},
            )

        # D. <0.75x full stop loss
        if multiple < HARD_SL_FULL_MULTIPLE and "HARD_SL_75" not in executed:
            _append_reason(
                reasons, executed, "HARD_SL_75", FULL_EXIT_PCT,
                {"multiple": multiple, "entry_price_usd": entry_price, "current_price_usd": current_price_usd},
                repeatable=True,
            )

        # E. >1.6x first TP (50%)
        if multiple > HARD_TP_FIRST_MULTIPLE and "HARD_TP_160" not in executed:
            _append_reason(
                reasons, executed, "HARD_TP_160", HARD_TP_FIRST_EXIT_PCT,
                {"multiple": multiple, "entry_price_usd": entry_price, "current_price_usd": current_price_usd},
            )

    # ---- F. Dust force exit (lowest priority, runs last) ----
    dust_usd = dust_force_exit_usd if dust_force_exit_usd is not None else _env_float("DUST_FORCE_EXIT_USD", DUST_FORCE_EXIT_USD_DEFAULT)
    if remaining_value_usd is not None and remaining_value_usd < dust_usd:
        _append_reason(
            reasons, executed, "DUST_FORCE_EXIT", FULL_EXIT_PCT,
            {"remaining_value_usd": remaining_value_usd, "threshold_usd": dust_usd},
            repeatable=True,
        )

    if not reasons:
        return ExitDecision(False, 0.0, [], False)

    # If any reason requires full exit, exit at 100% (emergency)
    full_exit_reasons = [r for r in reasons if r.desired_exit_pct >= 1.0]
    if full_exit_reasons:
        return ExitDecision(True, FULL_EXIT_PCT, full_exit_reasons, True)

    exit_pct = max(r.desired_exit_pct for r in reasons)
    return ExitDecision(True, exit_pct, reasons, False)
