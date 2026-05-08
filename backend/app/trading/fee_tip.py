from typing import Dict, Any

# tip percentiles mapping attempt index to percentile
TIP_PERCENTILE_MAP = {
    0: 'landed_tips_50th_percentile',
    1: 'landed_tips_75th_percentile',
    2: 'landed_tips_95th_percentile',
}

def get_tip_for_attempt(attempt: int, tip_floor: Dict[str, Any]) -> int:
    """Return tip lamports for given attempt (0-based)."""
    if attempt < 0:
        attempt = 0
    if attempt > 2:
        attempt = 2
    key = TIP_PERCENTILE_MAP.get(attempt)
    if not key:
        key = TIP_PERCENTILE_MAP[0]
    val = tip_floor.get(key)
    if val is None:
        return 1000  # default min
    # ensure at least min
    min_tip = tip_floor.get('min_tip_lamports', 1000)
    return max(val, min_tip)
