"""Unified x-based strategy thresholds.

All formulas derived from the single strategy parameter x.
Trench-request thresholds are intended for GMGN market trenches pre-filtering.
Local thresholds are for post-fetch in-process checks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class StrategyThresholds:
    x: float

    # --- Trenches pre-filter (GMGN API request params) ---
    max_risk_ratio: float = 0.0
    max_entrapment_ratio: float = 0.0
    max_insider_ratio: float = 0.0
    max_bundler_rate: float = 0.0
    min_liquidity_usd: float = 0.0
    min_top_holder_rate: float = 0.0
    max_top_holder_rate: float = 0.0
    max_fresh_wallet_rate: float = 0.0
    max_creator_balance_rate: float = 0.0
    max_progress: float = 0.0
    min_holder_count: float = 0.0
    min_marketcap: float = 0.0
    min_smart_degen_count: float = 0.0
    min_volume_24h: float = 0.0

    # --- Local risk (post-fetch) ---
    sell_tax_max: float = 0.0
    sniper_count_max: float = 0.0
    top1_addr_type0_max: float = 0.0

    # --- Price face ---
    price_change_1h_min_pct: float = 0.0
    volume_per_swap_5m_min: float = 0.0
    swaps_5m_multiplier: float = 0.0

    @classmethod
    def compute(cls, x: float) -> StrategyThresholds:
        xf = float(x)
        common_risk = 0.05 + 0.5 * xf
        return cls(
            x=xf,

            # Trenches pre-filter
            max_risk_ratio=common_risk,
            max_entrapment_ratio=common_risk,
            max_insider_ratio=common_risk,
            max_bundler_rate=common_risk,
            min_liquidity_usd=5750.0 - 2500.0 * xf,
            min_top_holder_rate=0.155 - 0.05 * xf,
            max_top_holder_rate=0.225 + 0.25 * xf,
            max_fresh_wallet_rate=0.13 + 0.1 * xf,
            max_creator_balance_rate=0.049 + 0.01 * xf,
            max_progress=0.7,
            min_holder_count=max(0.0, 37.0 - 40.0 * xf),
            min_marketcap=100.0 * max(0.0, 37.0 - 40.0 * xf),
            min_smart_degen_count=max(0.0, 2.0 - 10.0 * xf),
            min_volume_24h=max(0.0, 1600.0 - 2000.0 * xf),

            # Local risk
            sell_tax_max=0.1 * xf,
            sniper_count_max=50.0 * xf,
            top1_addr_type0_max=0.049 + 0.01 * xf,

            # Price face
            price_change_1h_min_pct=100.0 * (0.3 - xf),
            volume_per_swap_5m_min=14.0 - 20.0 * xf,
            swaps_5m_multiplier=1.75 - 2.5 * xf,
        )

    def to_trench_filters(self) -> Dict[str, Any]:
        return {
            "max_rug_ratio": self.max_risk_ratio,
            "max_entrapment_ratio": self.max_entrapment_ratio,
            "max_insider_ratio": self.max_insider_ratio,
            "max_bundler_rate": self.max_bundler_rate,
            "min_liquidity_usd": self.min_liquidity_usd,
            "min_top_holder_rate": self.min_top_holder_rate,
            "max_top_holder_rate": self.max_top_holder_rate,
            "max_fresh_wallet_rate": self.max_fresh_wallet_rate,
            "max_creator_balance_rate": self.max_creator_balance_rate,
            "max_progress": self.max_progress,
            "min_holder_count": int(math.floor(self.min_holder_count)) + 1,
            "min_marketcap": self.min_marketcap,
            "min_smart_degen_count": int(math.floor(self.min_smart_degen_count)) + 1,
            "min_volume_24h": self.min_volume_24h,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in self.__dataclass_fields__.values()}


def compute_thresholds(x: float) -> StrategyThresholds:
    return StrategyThresholds.compute(x)
