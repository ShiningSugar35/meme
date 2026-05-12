from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set
from datetime import datetime, timedelta, timezone
import json


DUST_FORCE_EXIT_SOL_DEFAULT = 0.125


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


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        v = float(value)
        if v != v:  # NaN
            return default
        return v
    except (TypeError, ValueError):
        return default


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


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
    for key in ("latest_token_type", "type", "latest_type"):
        value = position.get(key)
        if value:
            return str(value)

    latest_snapshot = latest_snapshot or {}
    for key in ("type", "latest_type", "token_type"):
        value = latest_snapshot.get(key)
        if value:
            return str(value)

    return None


def _append_reason(
    reasons: List[ExitReason],
    executed: Set[str],
    name: str,
    desired_exit_pct: float,
    detail: Optional[Dict[str, Any]] = None,
    repeatable: bool = False,
):
    if not repeatable and name in executed:
        return
    reasons.append(ExitReason(name, desired_exit_pct, detail or {}))


async def decide_exit(
    position: Dict[str, Any],
    tick: Dict[str, Any],
    rolling_60s: Dict[str, Any],
    latest_snapshot: Dict[str, Any],
    now: datetime = None,
    dust_force_exit_sol: float = DUST_FORCE_EXIT_SOL_DEFAULT,
) -> ExitDecision:
    """
    Decide whether an open position should be exited.

    Important conventions:
    - All trigger comparisons use SOL-denominated token price.
    - DUST_FORCE_EXIT is based on current remaining SOL value, not USD.
    - Time stop uses last_fill_price_sol, not entry_price_sol.
    - One-shot partial exit rules are skipped once listed in executed_exit_rules_json.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    reasons: List[ExitReason] = []
    executed = _executed_exit_rules(position)

    entry_price = _to_float(position.get("entry_price_sol"))
    current_price = _to_float(
        tick.get("price_sol")
        or tick.get("latest_price_sol")
        or tick.get("current_price_sol")
    )

    remaining_value_sol = _to_float(
        tick.get("remaining_value_sol")
        or position.get("remaining_value_sol")
    )
    remaining_token_amount = _to_float(position.get("remaining_token_amount"))

    if remaining_value_sol is None and remaining_token_amount is not None and current_price is not None:
        remaining_value_sol = remaining_token_amount * current_price

    # Small residual position: force full exit.
    if remaining_value_sol is not None and remaining_value_sol < dust_force_exit_sol:
        _append_reason(
            reasons,
            executed,
            "DUST_FORCE_EXIT",
            1.0,
            {"remaining_value_sol": remaining_value_sol, "threshold_sol": dust_force_exit_sol},
            repeatable=True,
        )

    # Pool graduated to open market / DEX: full exit.
    token_type = _token_type(position, latest_snapshot)
    if token_type == "completed":
        _append_reason(
            reasons,
            executed,
            "COMPLETED",
            1.0,
            {"type": token_type},
            repeatable=True,
        )

    # Hard TP/SL. Use elif ladders so 3.1x does not also fire 2.5x and 1.8x.
    if entry_price and entry_price > 0 and current_price and current_price > 0:
        multiple = current_price / entry_price

        if multiple >= 3.10:
            _append_reason(
                reasons,
                executed,
                "HARD_TP_310",
                1.0,
                {"multiple": multiple, "entry_price_sol": entry_price, "current_price_sol": current_price},
            )
        elif multiple >= 2.50:
            _append_reason(
                reasons,
                executed,
                "HARD_TP_250",
                0.5,
                {"multiple": multiple, "entry_price_sol": entry_price, "current_price_sol": current_price},
            )
        elif multiple >= 1.80:
            _append_reason(
                reasons,
                executed,
                "HARD_TP_180",
                0.5,
                {"multiple": multiple, "entry_price_sol": entry_price, "current_price_sol": current_price},
            )

        if multiple <= 0.60:
            _append_reason(
                reasons,
                executed,
                "HARD_SL_60",
                1.0,
                {"multiple": multiple, "entry_price_sol": entry_price, "current_price_sol": current_price},
                repeatable=True,
            )
        elif multiple <= 0.80:
            _append_reason(
                reasons,
                executed,
                "HARD_SL_80",
                0.5,
                {"multiple": multiple, "entry_price_sol": entry_price, "current_price_sol": current_price},
            )

    # Dynamic TP/SL using the last real 60 seconds of SOL prices.
    rolling_60s = rolling_60s or {}
    low_60 = _to_float(rolling_60s.get("low") or rolling_60s.get("low_sol"))
    high_60 = _to_float(rolling_60s.get("high") or rolling_60s.get("high_sol"))

    if low_60 and low_60 > 0 and current_price and current_price > 0:
        low_multiple = current_price / low_60
        if low_multiple >= 3.0:
            _append_reason(
                reasons,
                executed,
                "DYN_TP_3X_LOW60",
                1.0,
                {"low_60s_sol": low_60, "current_price_sol": current_price, "multiple": low_multiple},
            )
        elif low_multiple >= 2.0:
            _append_reason(
                reasons,
                executed,
                "DYN_TP_2X_LOW60",
                0.5,
                {"low_60s_sol": low_60, "current_price_sol": current_price, "multiple": low_multiple},
            )

    if high_60 and high_60 > 0 and current_price and current_price > 0:
        drawdown_from_high = current_price / high_60
        if drawdown_from_high <= 0.55:
            _append_reason(
                reasons,
                executed,
                "DYN_SL_55_HIGH60",
                1.0,
                {"high_60s_sol": high_60, "current_price_sol": current_price, "ratio": drawdown_from_high},
                repeatable=True,
            )
        elif drawdown_from_high <= 0.75:
            _append_reason(
                reasons,
                executed,
                "DYN_SL_75_HIGH60",
                0.5,
                {"high_60s_sol": high_60, "current_price_sol": current_price, "ratio": drawdown_from_high},
            )

    # Time stop: if 5 minutes after the last fill there is still <15% gain from the last fill price, exit all.
    last_fill_at = _parse_dt(position.get("last_fill_at"))
    last_fill_price = _to_float(position.get("last_fill_price_sol"))
    if last_fill_at and last_fill_price and last_fill_price > 0 and current_price and current_price > 0:
        if now >= last_fill_at + timedelta(minutes=5):
            pct_change = current_price / last_fill_price - 1.0
            if pct_change < 0.15:
                _append_reason(
                    reasons,
                    executed,
                    "TIME_STOPLOSS",
                    1.0,
                    {
                        "pct_change_from_last_fill": pct_change,
                        "last_fill_price_sol": last_fill_price,
                        "current_price_sol": current_price,
                        "last_fill_at": last_fill_at.isoformat(),
                    },
                    repeatable=True,
                )

    if not reasons:
        return ExitDecision(False, 0.0, [], False)

    # Full-exit reasons dominate partial exits.
    full_exit_reasons = [r for r in reasons if r.desired_exit_pct >= 1.0]
    if full_exit_reasons:
        return ExitDecision(True, 1.0, full_exit_reasons, True)

    # Otherwise use the largest partial exit. Keep all equal/lower-priority reasons for logging.
    exit_pct = max(r.desired_exit_pct for r in reasons)
    return ExitDecision(True, exit_pct, reasons, False)
