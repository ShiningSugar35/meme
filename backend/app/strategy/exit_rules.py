from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set
import json
import math
import os


DUST_FORCE_EXIT_USD_DEFAULT = 12.5
DUST_FORCE_EXIT_SOL_DEFAULT = 0.125  # legacy fallback only


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
    return dt.astimezone(timezone.utc)


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
    for container in (position or {}, latest_snapshot or {}):
        for key in ("latest_token_type", "type", "latest_type", "token_type"):
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


def _current_price_sol(tick: Dict[str, Any], position: Dict[str, Any]) -> Optional[float]:
    return _to_float(
        tick.get("price_sol")
        or tick.get("latest_price_sol")
        or tick.get("current_price_sol")
        or position.get("last_fill_price_sol")
        or position.get("entry_price_sol")
    )


def _current_price_usd(tick: Dict[str, Any], position: Dict[str, Any]) -> Optional[float]:
    return _to_float(
        tick.get("price_usd")
        or tick.get("latest_price_usd")
        or tick.get("current_price_usd")
        or position.get("last_fill_price_usd")
        or position.get("entry_price_usd")
    )


async def decide_exit(
    position: Dict[str, Any],
    tick: Dict[str, Any],
    rolling_60s: Dict[str, Any],
    latest_snapshot: Dict[str, Any],
    now: Optional[datetime] = None,
    dust_force_exit_sol: float = DUST_FORCE_EXIT_SOL_DEFAULT,
    dust_force_exit_usd: Optional[float] = None,
) -> ExitDecision:
    """Decide whether an open position should be exited.

    Current strategy conventions:
    - price triggers use SOL-denominated token price;
    - dust force-exit is USD-denominated when a USD remaining value is present;
    - dynamic stop exits 50% when current price breaks the latest/rolling 1m low;
    - time stop exits 50% when there is no fill for 5 minutes and gain since the
      last fill price is under 15%.
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

    entry_price = _to_float(position.get("entry_price_sol"))
    current_price = _current_price_sol(tick, position)
    current_price_usd = _current_price_usd(tick, position)

    remaining_value_usd = _to_float(tick.get("remaining_value_usd") or position.get("remaining_value_usd"))
    remaining_value_sol = _to_float(tick.get("remaining_value_sol") or position.get("remaining_value_sol"))
    remaining_token_amount = _to_float(position.get("remaining_token_amount"))

    if remaining_value_sol is None and remaining_token_amount is not None and current_price is not None:
        remaining_value_sol = remaining_token_amount * current_price
    if remaining_value_usd is None and remaining_token_amount is not None and current_price_usd is not None:
        remaining_value_usd = remaining_token_amount * current_price_usd

    dust_usd = dust_force_exit_usd if dust_force_exit_usd is not None else _env_float("DUST_FORCE_EXIT_USD", DUST_FORCE_EXIT_USD_DEFAULT)
    if remaining_value_usd is not None:
        if remaining_value_usd < dust_usd:
            _append_reason(
                reasons,
                executed,
                "DUST_FORCE_EXIT",
                1.0,
                {"remaining_value_usd": remaining_value_usd, "threshold_usd": dust_usd},
                repeatable=True,
            )
    elif remaining_value_sol is not None and remaining_value_sol < dust_force_exit_sol:
        _append_reason(
            reasons,
            executed,
            "DUST_FORCE_EXIT",
            1.0,
            {"remaining_value_sol": remaining_value_sol, "threshold_sol": dust_force_exit_sol, "fallback": "legacy_sol"},
            repeatable=True,
        )

    token_type = _token_type(position, latest_snapshot)
    if token_type == "completed":
        _append_reason(reasons, executed, "COMPLETED", 1.0, {"type": token_type}, repeatable=True)

    # Hard TP/SL.  Use ladders so the highest applicable TP/SL dominates.
    if entry_price and entry_price > 0 and current_price and current_price > 0:
        multiple = current_price / entry_price
        if multiple >= 3.10:
            _append_reason(reasons, executed, "HARD_TP_310", 1.0, {"multiple": multiple, "entry_price_sol": entry_price, "current_price_sol": current_price})
        elif multiple >= 2.50:
            _append_reason(reasons, executed, "HARD_TP_250", 0.5, {"multiple": multiple, "entry_price_sol": entry_price, "current_price_sol": current_price})
        elif multiple >= 1.80:
            _append_reason(reasons, executed, "HARD_TP_180", 0.5, {"multiple": multiple, "entry_price_sol": entry_price, "current_price_sol": current_price})

        if multiple <= 0.60:
            _append_reason(reasons, executed, "HARD_SL_60", 1.0, {"multiple": multiple, "entry_price_sol": entry_price, "current_price_sol": current_price}, repeatable=True)
        elif multiple <= 0.80:
            _append_reason(reasons, executed, "HARD_SL_80", 0.5, {"multiple": multiple, "entry_price_sol": entry_price, "current_price_sol": current_price})

    # Dynamic stop: current price breaks the latest completed/rolling 1m low.
    # Prefer explicit low_1m from caller; fall back to rolling low for backward
    # compatibility with the existing runner.
    low_1m = _to_float(
        rolling_60s.get("low_1m")
        or rolling_60s.get("completed_1m_low")
        or rolling_60s.get("low_excluding_current")
        or rolling_60s.get("low")
        or rolling_60s.get("low_sol")
    )
    if low_1m and low_1m > 0 and current_price and current_price > 0 and current_price < low_1m:
        _append_reason(
            reasons,
            executed,
            "DYN_SL_LOW_1M",
            0.5,
            {"low_1m_sol": low_1m, "current_price_sol": current_price},
        )

    # Time stop: 5 minutes after the last fill, if price appreciation from the
    # last fill is <15%, withdraw 50% rather than full exit.
    last_fill_at = _parse_dt(position.get("last_fill_at"))
    last_fill_price = _to_float(position.get("last_fill_price_sol") or position.get("entry_price_sol"))
    if last_fill_at and last_fill_price and last_fill_price > 0 and current_price and current_price > 0:
        if now >= last_fill_at + timedelta(minutes=5):
            growth = current_price / last_fill_price - 1.0
            if growth < 0.15:
                _append_reason(
                    reasons,
                    executed,
                    "TIME_STOPLOSS",
                    0.5,
                    {
                        "growth_from_last_fill": growth,
                        "last_fill_price_sol": last_fill_price,
                        "current_price_sol": current_price,
                        "last_fill_at": last_fill_at.isoformat(),
                    },
                )

    if not reasons:
        return ExitDecision(False, 0.0, [], False)

    full_exit_reasons = [r for r in reasons if r.desired_exit_pct >= 1.0]
    if full_exit_reasons:
        return ExitDecision(True, 1.0, full_exit_reasons, True)

    exit_pct = max(r.desired_exit_pct for r in reasons)
    return ExitDecision(True, exit_pct, reasons, False)
