"""Unified x-based strategy thresholds — single source of truth.

All formulas from 思路.md.  No other file should hardcode these formulas.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class StrategyThresholds:
    x: float

    # --- Trenches pre-filter / common risk ---
    common_risk: float = 0.0
    min_liquidity_usd: float = 0.0
    min_top_holder_rate: float = 0.0
    max_top_holder_rate: float = 0.0
    max_fresh_wallet_rate: float = 0.0
    max_creator_balance_rate: float = 0.0
    max_progress: float = 0.7
    min_holder_count: float = 0.0
    min_marketcap: float = 0.0
    min_smart_degen_count: float = 0.0
    min_volume_24h: float = 0.0

    # --- Local entry risk (not pre-filtered by trenches) ---
    sell_tax_max: float = 0.0
    sniper_count_max: float = 0.0
    top1_addr_type0_max: float = 0.0

    # --- Price face ---
    swaps_5m_multiplier: float = 0.0
    volume_per_swap_5m_min: float = 0.0
    price_change_1h_min_pct: float = 0.0

    # --- Smart degen ---
    smart_degen_max_pct: float = 0.015
    smart_degen_min_pct: float = 0.005
    smart_degen_max_usd: float = 150.0
    smart_degen_min_usd: float = 50.0

    @classmethod
    def compute(cls, x: float) -> StrategyThresholds:
        xf = float(x)
        common = 0.05 + 0.5 * xf
        hc = max(0.0, 37.0 - 40.0 * xf)
        return cls(
            x=xf,
            common_risk=common,
            # Trenches
            min_liquidity_usd=5750.0 - 2500.0 * xf,
            min_top_holder_rate=0.155 - 0.05 * xf,
            max_top_holder_rate=0.225 + 0.25 * xf,
            max_fresh_wallet_rate=0.13 + 0.1 * xf,
            max_creator_balance_rate=0.049 + 0.01 * xf,
            max_progress=0.7,
            min_holder_count=hc,
            min_marketcap=100.0 * hc,
            min_smart_degen_count=max(0.0, 2.0 - 10.0 * xf),
            min_volume_24h=max(0.0, 1600.0 - 2000.0 * xf),
            # Entry risk
            sell_tax_max=0.1 * xf,
            sniper_count_max=50.0 * xf,
            top1_addr_type0_max=0.049 + 0.01 * xf,
            # Price face
            swaps_5m_multiplier=1.75 - 2.5 * xf,
            volume_per_swap_5m_min=14.0 - 20.0 * xf,
            price_change_1h_min_pct=100.0 * (0.3 - xf),
        )

    def to_trench_filters(self) -> Dict[str, Any]:
        return {
            "max_rug_ratio": self.common_risk,
            "max_entrapment_ratio": self.common_risk,
            "max_insider_ratio": self.common_risk,
            "max_bundler_rate": self.common_risk,
            "min_liquidity": self.min_liquidity_usd,
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


def compute_thresholds(x: float) -> StrategyThresholds:
    return StrategyThresholds.compute(x)


def entry_size_usd(liquidity_usd: float, x: float, max_usd: float = 150.0) -> float:
    """SIM and LIVE base sizing (LIVE also caps by wallet balance)."""
    return min(liquidity_usd * 0.015, max_usd)
