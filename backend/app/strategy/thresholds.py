"""Unified x-based strategy thresholds — single source of truth.

All formulas from 思路.md.  No other file should hardcode these formulas.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..logging_config import logger


KNOWN_TRENCH_FILTER_KEYS: List[str] = [
    "max_rug_ratio", "max_entrapment_ratio", "max_insider_ratio", "max_bundler_rate",
    "min_liquidity", "min_top_holder_rate", "max_top_holder_rate", "max_fresh_wallet_rate",
    "max_creator_balance_rate",
    "min_holder_count", "max_holder_count", "min_marketcap", "min_volume_24h",
    "min_smart_degen_count",
]


def normalize_rate_fraction(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if 0.0 <= value <= 1.0:
        return value
    if 1.0 < value <= 100.0:
        return value / 100.0
    logger.warning(f"normalize_rate_fraction: unexpected value {value}")
    return None


@dataclass(frozen=True)
class StrategyThresholds:
    x: float

    common_risk: float
    max_rug_ratio: float
    max_entrapment_ratio: float
    max_insider_ratio: float
    max_bundler_rate: float

    entry_rug_ratio: float
    entry_entrapment_ratio: float
    entry_insider_ratio: float
    entry_bundler_rate: float

    min_liquidity: float
    min_liquidity_holder_ratio: float
    min_top_holder_rate: float
    max_top_holder_rate: float
    max_fresh_wallet_rate: float
    max_creator_balance_rate: float
    entry_max_creator_balance_rate: float

    min_holder_count_raw: float
    min_holder_count_api: int
    max_holder_count_raw: float
    max_holder_count_api: int
    min_marketcap_raw: float
    min_marketcap_api: float

    min_smart_degen_count_raw: float
    min_smart_degen_count_api: Optional[int]

    min_volume_24h: float

    sell_tax_max: float
    sniper_count_max: float
    entry_sniper_count_max: float
    top1_addr_type0_min: float
    top1_addr_type0_max: float

    price_change_1h_min_pct: float
    price_change_1h_max_pct: float
    swaps_1h_min: float
    volume_per_swap_1h_min: float
    price_range_24h_percentile_min: float
    price_range_24h_percentile_max: float

    smart_degen_max_pct: float = 0.004
    smart_degen_min_pct: float = 0.002
    smart_degen_max_usd: float = 40.0
    smart_degen_min_usd: float = 20.0

    @property
    def min_holder_count(self) -> float:
        return self.min_holder_count_raw

    @property
    def min_marketcap(self) -> float:
        return self.min_marketcap_raw

    @property
    def min_smart_degen_count(self) -> float:
        return self.min_smart_degen_count_raw

    @property
    def requires_smart_degen_entry(self) -> bool:
        """True 表示买入筛选需要聪明钱条件（x <= 0.15）；False 表示跳过。"""
        return self.min_smart_degen_count_api is not None

    @property
    def holding_rug_ratio(self) -> float:
        return self.max_rug_ratio

    @property
    def holding_entrapment_ratio(self) -> float:
        return self.max_entrapment_ratio

    @property
    def holding_insider_ratio(self) -> float:
        return self.max_insider_ratio

    @property
    def holding_bundler_rate(self) -> float:
        return self.max_bundler_rate

    @classmethod
    def compute(cls, x: float) -> StrategyThresholds:
        xf = float(x)
        common_risk = 0.05 + 0.5 * xf

        max_rug_ratio = xf
        max_entrapment_ratio = xf
        max_insider_ratio = xf
        max_bundler_rate = xf

        entry_rug_ratio = common_risk
        entry_entrapment_ratio = common_risk
        entry_insider_ratio = common_risk
        entry_bundler_rate = common_risk

        min_liquidity = 5000.0 - 2500.0 * xf
        min_liquidity_holder_ratio = 70.0 - 100.0 * xf

        min_top_holder_rate = 0.155 - 0.05 * xf
        max_top_holder_rate = 0.225 + 0.25 * xf

        max_fresh_wallet_rate = 0.13 + 0.1 * xf

        # 买入筛选（GMGN trenches）与 持仓风控 使用不同公式
        max_creator_balance_rate = 0.054 + 0.01 * xf      # 持仓风控：creator_balance_rate < 0.054+0.01x
        entry_max_creator_balance_rate = 0.049 + 0.01 * xf  # 买入筛选：max_creator_balance_rate < 0.049+0.01x

        min_holder_count_raw = 37.0 - 40.0 * xf
        min_holder_count_api = int(math.floor(min_holder_count_raw)) + 1
        max_holder_count_raw = 400.0 + 2000.0 * xf
        max_holder_count_api = int(math.ceil(max_holder_count_raw)) - 1

        min_marketcap_raw = min_liquidity * (1.3 - xf)
        min_marketcap_api = min_liquidity * (1.3 - xf)

        min_smart_degen_count_raw = 1.5 - 10.0 * xf
        min_smart_degen_count_api = (
            int(math.floor(min_smart_degen_count_raw)) + 1
            if min_smart_degen_count_raw >= 0
            else None
        )

        min_volume_24h = max(0.0, 1600.0 - 2000.0 * xf)

        sell_tax_max = 0.1 * xf
        # 买入条件 < 50x，持仓风控 < 75x
        entry_sniper_count_max = 50.0 * xf     # 买入本地风控：sniper_count < 50x
        sniper_count_max = 75.0 * xf            # 持仓风控轮询：sniper_count < 75x
        top1_addr_type0_min = 0.032 - 0.02 * xf
        top1_addr_type0_max = 0.049 + 0.01 * xf

        price_change_1h_min_pct = 50.0 * (xf - 0.4)
        price_change_1h_max_pct = 60.0 - 50.0 * xf
        swaps_1h_min = 7.0 + 20.0 * xf
        volume_per_swap_1h_min = 23.0 + 20.0 * xf
        price_range_24h_percentile_min = 0.0
        price_range_24h_percentile_max = 0.45 - 0.5 * xf

        return cls(
            x=xf,
            common_risk=common_risk,
            max_rug_ratio=max_rug_ratio,
            max_entrapment_ratio=max_entrapment_ratio,
            max_insider_ratio=max_insider_ratio,
            max_bundler_rate=max_bundler_rate,
            entry_rug_ratio=entry_rug_ratio,
            entry_entrapment_ratio=entry_entrapment_ratio,
            entry_insider_ratio=entry_insider_ratio,
            entry_bundler_rate=entry_bundler_rate,
            min_liquidity=min_liquidity,
            min_liquidity_holder_ratio=min_liquidity_holder_ratio,
            min_top_holder_rate=min_top_holder_rate,
            max_top_holder_rate=max_top_holder_rate,
            max_fresh_wallet_rate=max_fresh_wallet_rate,
            max_creator_balance_rate=max_creator_balance_rate,
            entry_max_creator_balance_rate=entry_max_creator_balance_rate,
            min_holder_count_raw=min_holder_count_raw,
            min_holder_count_api=min_holder_count_api,
            max_holder_count_raw=max_holder_count_raw,
            max_holder_count_api=max_holder_count_api,
            min_marketcap_raw=min_marketcap_raw,
            min_marketcap_api=min_marketcap_api,
            min_smart_degen_count_raw=min_smart_degen_count_raw,
            min_smart_degen_count_api=min_smart_degen_count_api,
            min_volume_24h=min_volume_24h,
            sell_tax_max=sell_tax_max,
            sniper_count_max=sniper_count_max,
            entry_sniper_count_max=entry_sniper_count_max,
            top1_addr_type0_min=top1_addr_type0_min,
            top1_addr_type0_max=top1_addr_type0_max,
            price_change_1h_min_pct=price_change_1h_min_pct,
            price_change_1h_max_pct=price_change_1h_max_pct,
            swaps_1h_min=swaps_1h_min,
            volume_per_swap_1h_min=volume_per_swap_1h_min,
            price_range_24h_percentile_min=price_range_24h_percentile_min,
            price_range_24h_percentile_max=price_range_24h_percentile_max,
        )

    def to_trench_filters(self) -> Dict[str, Any]:
        """Build the constant-value payload for GMGN trenches API.

        Every value is a computed numeric constant.  No formula strings, no x.
        Internal debug fields (_x, _computed_from_x) are included and must be
        stripped before sending to GMGN.
        """
        filters = {
            "max_rug_ratio": self.entry_rug_ratio,
            "max_entrapment_ratio": self.entry_entrapment_ratio,
            "max_insider_ratio": self.entry_insider_ratio,
            "max_bundler_rate": self.entry_bundler_rate,
            "min_liquidity": self.min_liquidity,
            "min_top_holder_rate": self.min_top_holder_rate,
            "max_top_holder_rate": self.max_top_holder_rate,
            "max_fresh_wallet_rate": self.max_fresh_wallet_rate,
            # 买入筛选用 entry_* 值（比持仓风控更严格）
            "max_creator_balance_rate": self.entry_max_creator_balance_rate,
            "min_holder_count": self.min_holder_count_api,
            "max_holder_count": self.max_holder_count_api,
            "min_marketcap": self.min_marketcap_api,
            "min_volume_24h": self.min_volume_24h,
            "_x": self.x,
            "_computed_from_x": self.x,
        }
        if self.min_smart_degen_count_api is not None:
            filters["min_smart_degen_count"] = self.min_smart_degen_count_api
        return filters


def compute_thresholds(x: float) -> StrategyThresholds:
    return StrategyThresholds.compute(x)


def requires_smart_degen_for_x(x: float) -> bool:
    """快捷判断：给定 x 值，买入阶段是否需要聪明钱条件。"""
    return compute_thresholds(x).requires_smart_degen_entry


def compute_holding_thresholds(x: float) -> Dict[str, float]:
    """Return holding-specific threshold values (all = x directly)."""
    xf = float(x)
    return {
        "holding_rug_ratio": xf,
        "holding_entrapment_ratio": xf,
        "holding_insider_ratio": xf,
        "holding_bundler_rate": xf,
    }


# 思路.md: 模拟盘 min(1% liquidity, $100); 实盘 = min(1% liquidity, $100, wallet_balance)
def entry_size_usd(liquidity_usd: float, x: float, max_usd: float = 100.0) -> float:
    return min(liquidity_usd * 0.01, max_usd)


def strip_internal_debug_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove all _-prefixed internal debug fields before sending to GMGN."""
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def build_trench_filters_for_x(x: float) -> Dict[str, Any]:
    """High-level helper: compute thresholds and return the trench filter dict.

    Returns ONLY GMGN-safe constant values (no _-prefixed debug fields).
    For internal logging, call .to_trench_filters() separately.
    """
    t = compute_thresholds(x)
    raw = t.to_trench_filters()
    return strip_internal_debug_fields(raw)
