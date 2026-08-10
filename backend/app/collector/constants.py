"""Frozen collector policy constants.

Only business-invariant rules belong here.  Credentials, URLs, polling
intervals and operational retry limits remain configuration supplied by the
application layer.
"""

from __future__ import annotations

from dataclasses import dataclass


DISCOVERY_TYPES = ("new_creation", "near_completion", "completed")

LAUNCHPADS = (
    "Pump.fun",
    "Moonshot",
    "moonshot_app",
    "letsbonk",
    "memoo",
    "token_mill",
    "jup_studio",
    "bags",
    "believe",
    "heaven",
)


@dataclass(frozen=True, slots=True)
class FilterThresholds:
    max_rug_ratio: float = 0.2
    max_insider_ratio: float = 0.2
    max_bundler_rate: float = 0.2
    min_liquidity: float = 4_800.0
    min_top_10_holder_rate: float = 0.145
    max_top_10_holder_rate: float = 0.29
    max_fresh_wallet_rate: float = 0.2
    max_rat_trader_amount_rate: float = 0.2
    min_holder_count_exclusive: int = 29
    max_holder_count_exclusive: int = 1_000
    min_marketcap: float = 5_000.0
    max_sell_tax: float = 0.025
    max_buy_tax: float = 0.025
    max_sniper_count: int = 10
    min_age_minutes: float = 3.0
    min_liquidity_per_holder: float = 50.0
    min_swaps_1h: int = 19
    min_volume_per_swap_1h: float = 30.0
    min_weighted_activity: float = 5_000.0
    min_top1_addr_type0_rate: float = 0.028
    max_top1_addr_type0_rate: float = 0.056


@dataclass(frozen=True, slots=True)
class LabelPolicy:
    """Current label version: SL 0.9, TP 1.6, 2h close winner 1.2."""

    stop_loss_ratio: float = 0.9
    take_profit_ratio: float = 1.6
    final_close_positive_ratio: float = 1.2
    window_seconds: int = 2 * 60 * 60
    history_seconds: int = 60 * 60
    label_version: str = "sl090_tp160_close120_h2_v1"


TRENCH_PREFILTERS = {
    "max_rug_ratio": 0.2,
    "max_insider_ratio": 0.2,
    "max_bundler_rate": 0.2,
    "min_liquidity": 4_800,
    "min_top_holder_rate": 0.145,
    "max_top_holder_rate": 0.29,
    "max_fresh_wallet_rate": 0.2,
    "renounced_mint": 1,
    "renounced_freeze_account": 1,
    "min_holder_count": 30,
    "max_holder_count": 999,
    "min_marketcap": 5_000,
}

