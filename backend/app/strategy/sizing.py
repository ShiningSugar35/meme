"""Position sizing rules for the meme trading system.

The intended entry rule is deliberately simple and conservative:

    entry_size_sol = min(1.25% * current SOL-side pool liquidity, 2 SOL)

This module keeps that rule, but adds defensive validation so missing/invalid
liquidity never turns into an accidental trade size.
"""

from __future__ import annotations

import math
from typing import Optional


ENTRY_LIQUIDITY_PCT = 0.0125
ENTRY_MAX_SOL = 2.0


def _as_finite_float(value: object) -> Optional[float]:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


async def compute_entry_size(
    sol_side_liquidity: float,
    *,
    liquidity_pct: float = ENTRY_LIQUIDITY_PCT,
    max_entry_sol: float = ENTRY_MAX_SOL,
    min_entry_sol: float = 0.0,
) -> float:
    """Return the SOL entry size for a token.

    Args:
        sol_side_liquidity: Current SOL-side pool liquidity, in SOL.
        liquidity_pct: Fraction of the SOL-side liquidity to use.
        max_entry_sol: Absolute per-entry cap, in SOL.
        min_entry_sol: Optional minimum executable size. When the computed size is
            below this value, return 0 rather than rounding up.

    Returns:
        A non-negative SOL amount. Returns 0.0 when inputs are missing, invalid,
        non-positive, or below the optional minimum.
    """
    liq = _as_finite_float(sol_side_liquidity)
    pct = _as_finite_float(liquidity_pct)
    cap = _as_finite_float(max_entry_sol)
    min_size = _as_finite_float(min_entry_sol)

    if liq is None or pct is None or cap is None or min_size is None:
        return 0.0
    if liq <= 0 or pct <= 0 or cap <= 0 or min_size < 0:
        return 0.0

    size = min(liq * pct, cap)
    if size <= 0 or size < min_size:
        return 0.0

    # Round down to a practical precision; never round up a risk amount.
    return math.floor(size * 1_000_000_000) / 1_000_000_000
