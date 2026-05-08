from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class SecondFilterResult:
    passed: bool
    details: List[Dict[str, Any]]
    feature_vector: Dict[str, Any]


async def run_second_filter(token: Dict[str, Any], strategy_group: Dict[str, Any], latest_price: Dict[str, Any], klines: List[Dict[str, Any]], buy_sell_1m: Dict[str, float]) -> SecondFilterResult:
    details: List[Dict[str, Any]] = []
    y = float(strategy_group.get("y", 2.25))

    prices = [k.get("close") for k in klines if k.get("close") is not None]
    if len(prices) < 2:
        details.append({"rule": "min_price_points", "passed": False, "actual": len(prices), "required": 2, "reason": "need at least 2 price points"})
        return SecondFilterResult(False, details, {})

    high_5m = max(prices)
    low_5m = min(prices)
    if high_5m == low_5m:
        details.append({"rule": "high_eq_low", "passed": False, "actual": high_5m, "reason": "high equals low"})
        return SecondFilterResult(False, details, {})

    current_price = latest_price.get("price")
    if current_price is None:
        details.append({"rule": "latest_price_present", "passed": False, "reason": "missing GMGN latest price"})
        return SecondFilterResult(False, details, {})

    buy_v = buy_sell_1m.get("buy_volume", 0)
    sell_v = buy_sell_1m.get("sell_volume", 0)
    cond1 = buy_v > sell_v * (1.2 - y * 0.05)
    details.append({"rule": "buy_vs_sell_1m", "passed": cond1, "buy": buy_v, "sell": sell_v, "threshold": f"sell*(1.2 - y*0.05)"})

    cond2 = current_price > high_5m / y
    details.append({"rule": "price_gt_high_over_y", "passed": cond2, "current": current_price, "high_5m": high_5m, "y": y})

    cond3 = current_price < low_5m * y
    details.append({"rule": "price_lt_low_times_y", "passed": cond3, "current": current_price, "low_5m": low_5m, "y": y})

    frac = (current_price - low_5m) / (high_5m - low_5m)
    low_frac = 0.8 - 0.2 * y
    high_frac = 0.35 + 0.2 * y
    cond4 = (low_frac < frac < high_frac)
    details.append({"rule": "fraction_range", "passed": cond4, "frac": frac, "range": [low_frac, high_frac]})

    passed = all(d.get("passed") for d in details)
    feature_vector = {"current_price": current_price, "high_5m": high_5m, "low_5m": low_5m, "frac": frac}
    return SecondFilterResult(passed, details, feature_vector)
