"""Dynamic slippage calculation.

The old implementation always returned the cap. That contradicted the strategy
idea: the cap should be a hard upper bound, not the normal quote slippage.
"""

from __future__ import annotations

import math
from typing import Optional


BUY_SLIPPAGE_CAP_BPS = 1500
SELL_SLIPPAGE_CAP_BPS = 2000
EMERGENCY_SLIPPAGE_CAP_BPS = 3500
PRICE_IMPACT_HARD_CAP_PCT = 10.0


def _finite_float(value: object) -> Optional[float]:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _normalise_volatility_ratio(value: object) -> float:
    """Accept volatility as ratio (0.08) or percent (8) and return ratio."""
    v = _finite_float(value)
    if v is None or v <= 0:
        return 0.0
    return v / 100.0 if v > 1 else v


def _clip_int(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))


async def compute_slippage_bps(
    order_size_sol: float,
    sol_side_liquidity: float,
    cap_bps: int = BUY_SLIPPAGE_CAP_BPS,
    *,
    side: str = "BUY",
    emergency: bool = False,
    recent_volatility_pct: Optional[float] = None,
    min_bps: Optional[int] = None,
) -> int:
    """Compute slippage in basis points, bounded by the configured cap.

    The formula intentionally uses only information the current system already
    has: order size, SOL-side pool liquidity, side, and optional recent
    volatility. It is not a price-impact oracle; it is a safer default for quote
    requests so ordinary trades do not always use maximum allowed slippage.

    In emergency exits, return the emergency cap immediately because execution
    certainty is more important than price precision.
    """
    side_upper = (side or "BUY").upper()

    cap = int(cap_bps or (SELL_SLIPPAGE_CAP_BPS if side_upper == "SELL" else BUY_SLIPPAGE_CAP_BPS))
    cap = max(1, min(cap, EMERGENCY_SLIPPAGE_CAP_BPS))

    if emergency:
        # Emergency exits use the emergency hard cap because execution certainty
        # is more important than price precision.
        return int(EMERGENCY_SLIPPAGE_CAP_BPS)

    order = _finite_float(order_size_sol)
    liq = _finite_float(sol_side_liquidity)
    if order is None or liq is None or order <= 0 or liq <= 0:
        return cap

    default_min = 500 if side_upper == "SELL" else 300
    floor_bps = int(min_bps if min_bps is not None else default_min)
    floor_bps = max(1, min(floor_bps, cap))

    ratio = max(0.0, order / liq)

    # If the order is far too large relative to the SOL side, do not pretend a
    # small slippage setting is safe. Quote with the hard cap and let price-impact
    # checks decide whether the trade is acceptable.
    if ratio >= 0.05:
        return cap

    # Constant-product approximation: impact ~= r / (1-r). A multiplier is kept
    # because newly-created meme pools are volatile and routing can be worse than
    # the simple pool model.
    curve_bps = 10_000.0 * (ratio / max(1.0 - ratio, 0.01)) * 2.5

    vol_ratio = _normalise_volatility_ratio(recent_volatility_pct)
    volatility_bps = 10_000.0 * vol_ratio * 1.5

    raw = floor_bps + curve_bps + volatility_bps
    return _clip_int(raw, floor_bps, cap)
