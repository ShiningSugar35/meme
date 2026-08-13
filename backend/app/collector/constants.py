"""Frozen collector policy constants.

Only business-invariant rules belong here.  Credentials, URLs, polling
intervals and operational retry limits remain configuration supplied by the
application layer.
"""

from __future__ import annotations

from dataclasses import dataclass


DISCOVERY_TYPES = ("new_creation", "near_completion")

LAUNCHPADS = (
    "Pump.fun",
    "Moonshot",
    "moonshot_app",
    "letsbonk",
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
    min_top_10_holder_rate: float = 0.125
    max_top_10_holder_rate: float = 0.275
    max_fresh_wallet_rate: float = 0.2
    max_rat_trader_amount_rate: float = 0.2
    min_holder_count_exclusive: int = 29
    max_holder_count_exclusive: int = 1_000
    min_marketcap: float = 5_000.0
    max_sell_tax: float = 0.025
    max_buy_tax: float = 0.025
    max_sniper_count: int = 10
    min_age_minutes: float = 1.0
    min_liquidity_per_holder: float = 50.0
    min_swaps_1h: int = 19
    min_volume_per_swap_1h: float = 30.0
    min_weighted_activity: float = 5_000.0
    min_top1_addr_type0_rate: float = 0.028
    max_top1_addr_type0_rate: float = 0.056


@dataclass(frozen=True, slots=True)
class LabelPolicy:
    """One-hour binary first-touch label: SL 0.9, TP 1.6, timeout is negative."""

    stop_loss_ratio: float = 0.9
    take_profit_ratio: float = 1.6
    window_seconds: int = 60 * 60
    history_seconds: int = 60 * 60
    label_version: str = "sl090_tp160_h1_binary_v4"


ALLOWED_QUOTE_SYMBOLS = frozenset({"SOL", "USDC", "USDT"})
EXCLUDED_TARGET_SYMBOLS = frozenset({"SOL", "USDT", "USDC", "PYUSD", "WBTC", "WETH"})

SOL_WRAPPED_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
PYUSD_MINT = "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"

ALLOWED_QUOTE_MINTS = frozenset({SOL_WRAPPED_MINT, USDC_MINT, USDT_MINT})
EXCLUDED_TARGET_MINTS = frozenset({SOL_WRAPPED_MINT, USDC_MINT, USDT_MINT, PYUSD_MINT})


TRENCH_PREFILTERS = {
    "max_rug_ratio": 0.2,
    "max_insider_ratio": 0.2,
    "max_bundler_rate": 0.2,
    "min_liquidity": 4_800,
    "min_top_holder_rate": 0.125,
    "max_top_holder_rate": 0.275,
    "max_fresh_wallet_rate": 0.2,
    "renounced_mint": 1,
    "renounced_freeze_account": 1,
    "min_holder_count": 30,
    "max_holder_count": 999,
    "min_marketcap": 5_000,
}

