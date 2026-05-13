"""Position sizing rules.

The current strategy sizes entries in USD, then the executor converts USD to SOL
using live token/SOL pricing data:

    entry_size_usd = min(1.25% * current pool liquidity USD, $200)
"""

from __future__ import annotations

import math
import os
from typing import Optional


ENTRY_LIQUIDITY_PCT = 0.0125
ENTRY_MAX_USD = 200.0


def _as_finite_float(value: object) -> Optional[float]:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _env_float(name: str, default: float) -> float:
    v = _as_finite_float(os.getenv(name))
    return default if v is None else v


async def compute_entry_size_usd(
    liquidity_usd: float,
    *,
    liquidity_pct: Optional[float] = None,
    max_entry_usd: Optional[float] = None,
    min_entry_usd: float = 0.0,
) -> float:
    """Return the USD entry notional for a token.

    Missing/invalid liquidity returns 0.0.  The function floors to cents and
    never rounds a risk amount upward.
    """
    liq = _as_finite_float(liquidity_usd)
    pct = _as_finite_float(liquidity_pct if liquidity_pct is not None else _env_float("ENTRY_SIZE_LIQUIDITY_PCT", ENTRY_LIQUIDITY_PCT))
    cap = _as_finite_float(max_entry_usd if max_entry_usd is not None else _env_float("ENTRY_MAX_USD", ENTRY_MAX_USD))
    min_size = _as_finite_float(min_entry_usd)

    if liq is None or pct is None or cap is None or min_size is None:
        return 0.0
    if liq <= 0 or pct <= 0 or cap <= 0 or min_size < 0:
        return 0.0

    size = min(liq * pct, cap)
    if size <= 0 or size < min_size:
        return 0.0

    return math.floor(size * 100.0) / 100.0


async def compute_entry_size(
    liquidity_usd: float,
    *,
    liquidity_pct: float = ENTRY_LIQUIDITY_PCT,
    max_entry_sol: Optional[float] = None,
    max_entry_usd: Optional[float] = None,
    min_entry_sol: float = 0.0,
    min_entry_usd: float = 0.0,
) -> float:
    """Backward-compatible wrapper.

    Older executor code called this function expecting a SOL-denominated value.
    This strategy is now USD-denominated, so the wrapper returns USD notional.
    New code should call :func:`compute_entry_size_usd` explicitly.
    """
    _ = max_entry_sol, min_entry_sol  # retained for call-site compatibility
    return await compute_entry_size_usd(
        liquidity_usd,
        liquidity_pct=liquidity_pct,
        max_entry_usd=max_entry_usd,
        min_entry_usd=min_entry_usd,
    )
