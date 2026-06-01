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
    "max_creator_balance_rate", "max_progress",
    "min_holder_count", "min_marketcap", "min_volume_24h",
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

    min_liquidity: float
    min_top_holder_rate: float
    max_top_holder_rate: float
    max_fresh_wallet_rate: float
    max_creator_balance_rate: float
    max_progress: float

    min_holder_count_raw: float
    min_holder_count_api: int
    min_marketcap_raw: float
    min_marketcap_api: float

    min_smart_degen_count_raw: float
    min_smart_degen_count_api: Optional[int]

    min_volume_24h: float

    sell_tax_max: float
    sniper_count_max: float
    top1_addr_type0_max: float

    price_change_1h_min_pct: float
    volume_per_swap_5m_min: float
    swaps_5m_multiplier: float

    smart_degen_max_pct: float = 0.015
    smart_degen_min_pct: float = 0.005
    smart_degen_max_usd: float = 150.0
    smart_degen_min_usd: float = 50.0

    @property
    def min_holder_count(self) -> float:
        return self.min_holder_count_raw

    @property
    def min_marketcap(self) -> float:
        return self.min_marketcap_raw

    @property
    def min_smart_degen_count(self) -> float:
        return self.min_smart_degen_count_raw

    @classmethod
    def compute(cls, x: float) -> StrategyThresholds:
        xf = float(x)
        common_risk = 0.05 + 0.5 * xf

        max_rug_ratio = common_risk
        max_entrapment_ratio = common_risk
        max_insider_ratio = common_risk
        max_bundler_rate = common_risk

        min_liquidity = 5750.0 - 2500.0 * xf

        min_top_holder_rate = 0.155 - 0.05 * xf
        max_top_holder_rate = 0.225 + 0.25 * xf

        max_fresh_wallet_rate = 0.13 + 0.1 * xf
        max_creator_balance_rate = 0.049 + 0.01 * xf

        max_progress = 0.7

        min_holder_count_raw = 37.0 - 40.0 * xf
        min_holder_count_api = int(math.floor(min_holder_count_raw)) + 1

        min_marketcap_raw = 100.0 * min_holder_count_raw
        min_marketcap_api = 100.0 * min_holder_count_raw

        min_smart_degen_count_raw = 2.0 - 10.0 * xf
        min_smart_degen_count_api = (
            int(math.floor(min_smart_degen_count_raw)) + 1
            if min_smart_degen_count_raw >= 0
            else None
        )

        min_volume_24h = max(0.0, 1600.0 - 2000.0 * xf)

        sell_tax_max = 0.1 * xf
        sniper_count_max = 50.0 * xf
        top1_addr_type0_max = 0.049 + 0.01 * xf

        price_change_1h_min_pct = 100.0 * (0.3 - xf)
        volume_per_swap_5m_min = 14.0 - 20.0 * xf
        swaps_5m_multiplier = 1.75 - 2.5 * xf

        return cls(
            x=xf,
            common_risk=common_risk,
            max_rug_ratio=max_rug_ratio,
            max_entrapment_ratio=max_entrapment_ratio,
            max_insider_ratio=max_insider_ratio,
            max_bundler_rate=max_bundler_rate,
            min_liquidity=min_liquidity,
            min_top_holder_rate=min_top_holder_rate,
            max_top_holder_rate=max_top_holder_rate,
            max_fresh_wallet_rate=max_fresh_wallet_rate,
            max_creator_balance_rate=max_creator_balance_rate,
            max_progress=max_progress,
            min_holder_count_raw=min_holder_count_raw,
            min_holder_count_api=min_holder_count_api,
            min_marketcap_raw=min_marketcap_raw,
            min_marketcap_api=min_marketcap_api,
            min_smart_degen_count_raw=min_smart_degen_count_raw,
            min_smart_degen_count_api=min_smart_degen_count_api,
            min_volume_24h=min_volume_24h,
            sell_tax_max=sell_tax_max,
            sniper_count_max=sniper_count_max,
            top1_addr_type0_max=top1_addr_type0_max,
            price_change_1h_min_pct=price_change_1h_min_pct,
            volume_per_swap_5m_min=volume_per_swap_5m_min,
            swaps_5m_multiplier=swaps_5m_multiplier,
        )

    def to_trench_filters(self) -> Dict[str, Any]:
        """Build the constant-value payload for GMGN trenches API.

        Every value is a computed numeric constant.  No formula strings, no x.
        Internal debug fields (_x, _computed_from_x) are included and must be
        stripped before sending to GMGN.
        """
        filters = {
            "max_rug_ratio": self.max_rug_ratio,
            "max_entrapment_ratio": self.max_entrapment_ratio,
            "max_insider_ratio": self.max_insider_ratio,
            "max_bundler_rate": self.max_bundler_rate,
            "min_liquidity": self.min_liquidity,
            "min_top_holder_rate": self.min_top_holder_rate,
            "max_top_holder_rate": self.max_top_holder_rate,
            "max_fresh_wallet_rate": self.max_fresh_wallet_rate,
            "max_creator_balance_rate": self.max_creator_balance_rate,
            "max_progress": self.max_progress,
            "min_holder_count": self.min_holder_count_api,
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


def entry_size_usd(liquidity_usd: float, x: float, max_usd: float = 150.0) -> float:
    """SIM and LIVE base sizing (LIVE also caps by wallet balance)."""
    return min(liquidity_usd * 0.015, max_usd)


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
