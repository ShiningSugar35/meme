"""Position sizing rules.

Simulation: min(liquidity_usd * ENTRY_SIZE_LIQUIDITY_PCT, ENTRY_MAX_USD)
Live:       min(liquidity_usd * ENTRY_SIZE_LIQUIDITY_PCT, ENTRY_MAX_USD, wallet_balance_usd)

The executor converts USD notional into SOL/token amount later.  Values are read
from the mutable settings object so Control Center changes take effect without
restarting the process.
"""

from __future__ import annotations

import math
from typing import Optional

from ..config import settings

ENTRY_LIQUIDITY_PCT = 0.015
ENTRY_MAX_USD = 200.0
LIVE_MIN_ENTRY_USD = 10.0


def _as_finite_float(value: object) -> Optional[float]:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


async def compute_entry_size_usd(
    liquidity_usd: float,
    *,
    liquidity_pct: Optional[float] = None,
    max_entry_usd: Optional[float] = None,
    min_entry_usd: float = 0.0,
    wallet_balance_usd: Optional[float] = None,
    is_live: bool = False,
) -> float:
    """Return the USD entry notional for a token.

    LIVE entries are additionally capped by available wallet balance and are
    suppressed when the resulting amount is below $10.  SIM entries keep the old
    min(liquidity * pct, max_usd) behavior unless a caller explicitly supplies a
    different ``min_entry_usd``.
    """
    liq = _as_finite_float(liquidity_usd)
    pct = _as_finite_float(liquidity_pct if liquidity_pct is not None else getattr(settings, "ENTRY_SIZE_LIQUIDITY_PCT", ENTRY_LIQUIDITY_PCT))
    cap = _as_finite_float(max_entry_usd if max_entry_usd is not None else getattr(settings, "ENTRY_MAX_USD", ENTRY_MAX_USD))
    min_size = _as_finite_float(min_entry_usd)

    if liq is None or pct is None or cap is None or min_size is None:
        return 0.0
    if liq <= 0 or pct <= 0 or cap <= 0 or min_size < 0:
        return 0.0

    candidates = [liq * pct, cap]
    if is_live:
        wallet_balance = _as_finite_float(wallet_balance_usd)
        if wallet_balance is None or wallet_balance <= 0:
            return 0.0
        candidates.append(wallet_balance)
        min_size = max(min_size, LIVE_MIN_ENTRY_USD)

    size = min(candidates)
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
    wallet_balance_usd: Optional[float] = None,
    is_live: bool = False,
) -> float:
    """Backward-compatible wrapper returning USD notional."""
    _ = max_entry_sol, min_entry_sol
    return await compute_entry_size_usd(
        liquidity_usd,
        liquidity_pct=liquidity_pct,
        max_entry_usd=max_entry_usd,
        min_entry_usd=min_entry_usd,
        wallet_balance_usd=wallet_balance_usd,
        is_live=is_live,
    )
