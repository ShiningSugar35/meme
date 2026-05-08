async def compute_entry_size(sol_side_liquidity: float):
    """Return dict with size and blocked reason if missing liquidity.

    Returns:
      {"size_sol": float, "blocked": bool, "reason": Optional[str]}
    """
    if sol_side_liquidity is None:
        return {"size_sol": 0.0, "blocked": True, "reason": "sol_side_liquidity_missing"}
    entry_size_sol = min(0.0125 * sol_side_liquidity, 2.0)
    return {"size_sol": float(entry_size_sol), "blocked": False, "reason": None}
