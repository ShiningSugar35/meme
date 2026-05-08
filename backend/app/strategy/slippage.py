BUY_SLIPPAGE_CAP_BPS = 1500
SELL_SLIPPAGE_CAP_BPS = 2000
EMERGENCY_SLIPPAGE_CAP_BPS = 3500
PRICE_IMPACT_HARD_CAP_PCT = 10


async def compute_slippage_bps(order_size_sol: float, sol_side_liquidity: float, cap_bps: int = BUY_SLIPPAGE_CAP_BPS) -> int:
    # Simplified: return cap for now, but interface preserved
    return int(cap_bps)
