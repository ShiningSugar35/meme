from dataclasses import dataclass
from typing import Any, Dict, List
from datetime import datetime, timedelta, timezone
import json


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


async def decide_exit(position: Dict[str, Any], tick: Dict[str, Any], rolling_60s: Dict[str, Any], latest_snapshot: Dict[str, Any], now: datetime = None) -> ExitDecision:
    now = now or datetime.now(timezone.utc)
    reasons: List[ExitReason] = []
    entry_price = position.get("entry_price_sol")
    current_price = tick.get("price_sol")
    remaining_value_usd = position.get("remaining_value_usd", 0)

    # small dust
    if remaining_value_usd is not None and remaining_value_usd < 10:
        reasons.append(ExitReason("DUST_FORCE_EXIT", 1.0, {"remaining_value_usd": remaining_value_usd}))

    # hard TP/SL
    if entry_price and current_price:
        if current_price > 3.1 * entry_price:
            reasons.append(ExitReason("HARD_TP_310", 1.0, {}))
        if current_price > 2.5 * entry_price:
            reasons.append(ExitReason("HARD_TP_250", 0.5, {}))
        if current_price > 1.8 * entry_price:
            reasons.append(ExitReason("HARD_TP_180", 0.5, {}))

        if current_price < 0.6 * entry_price:
            reasons.append(ExitReason("HARD_SL_60", 1.0, {}))
        if current_price < 0.8 * entry_price:
            reasons.append(ExitReason("HARD_SL_80", 0.5, {}))

    # dynamic rules
    low_60 = rolling_60s.get("low")
    high_60 = rolling_60s.get("high")
    if low_60 and current_price:
        if current_price > 3 * low_60:
            reasons.append(ExitReason("DYN_TP_3X_LOW60", 1.0, {}))
        elif current_price > 2 * low_60:
            reasons.append(ExitReason("DYN_TP_2X_LOW60", 0.5, {}))

    if high_60 and current_price:
        if current_price < 0.55 * high_60:
            reasons.append(ExitReason("DYN_SL_55_HIGH60", 1.0, {}))
        elif current_price < 0.75 * high_60:
            reasons.append(ExitReason("DYN_SL_75_HIGH60", 0.5, {}))

    # time stop loss: check last confirmed fill
    last_fill_at = position.get("last_fill_at")
    last_fill_price = position.get("last_fill_price_usd")
    if last_fill_at and last_fill_price and current_price:
        try:
            t = datetime.fromisoformat(last_fill_at)
            if now >= t + timedelta(minutes=5):
                pct_change = current_price / last_fill_price - 1
                if pct_change < 0.15 and (position.get("last_exit_action_count", 0) == 0):
                    reasons.append(ExitReason("TIME_STOPLOSS", 1.0, {"pct_change": pct_change}))
        except Exception:
            pass

    # risk stoploss: re-run filters with locked_strategy_config_json
    locked = position.get("locked_strategy_config_json")
    if locked:
        try:
            locked_cfg = json.loads(locked)
            # if there's an embedded x and snapshot available, caller should re-evaluate using filters; here we just note the hook
            # For v1, assume caller will call filters explicitly; if mismatch, trigger full exit via external check
        except Exception:
            pass

    # completed
    if position.get("type") == "completed":
        reasons.append(ExitReason("COMPLETED", 1.0, {}))

    if reasons:
        exit_pct = max(r.desired_exit_pct for r in reasons)
        emergency = any(r.desired_exit_pct == 1.0 for r in reasons)
        return ExitDecision(True, exit_pct, reasons, emergency)

    return ExitDecision(False, 0.0, [], False)
