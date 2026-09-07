"""Frozen collector policy constants.

Only business-invariant rules belong here.  Credentials, URLs, polling
intervals and operational retry limits remain configuration supplied by the
application layer.
"""

from __future__ import annotations

from dataclasses import dataclass


DISCOVERY_TYPES = ("new_creation", "near_completion")

# Entry-feature generations are intentionally versioned. Historical samples
# collected before the event/regime upgrade remain auditable, but must never be
# silently backfilled with values that were not observed at their original PIT.
LEGACY_FEATURE_SCHEMA_VERSION = "legacy_pre_event_v1"
FEATURE_SCHEMA_VERSION = "event1m_regime_v7"

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

SOL_TRENCH_QUOTE_ADDRESS_TYPES = (4, 5, 3, 1, 13, 0)


@dataclass(frozen=True, slots=True)
class FilterThresholds:
    max_rug_ratio: float = 0.2
    max_insider_ratio: float = 0.2
    max_bundler_rate: float = 0.2
    min_liquidity: float = 5_000.0
    min_top_10_holder_rate: float = 0.14
    max_top_10_holder_rate: float = 0.25
    max_fresh_wallet_rate: float = 0.2
    max_rat_trader_amount_rate: float = 0.2
    min_holder_count_exclusive: int = 29
    max_holder_count_exclusive: int = 1_000
    min_marketcap: float = 5_000.0
    max_sell_tax: float = 0.025
    max_buy_tax: float = 0.025
    max_sniper_count: int = 10
    preliminary_min_age_minutes: float = 3.0
    min_age_minutes: float = 5.0
    max_age_minutes_exclusive: float = 300.0
    min_liquidity_per_holder: float = 50.0
    min_swaps_1h: int = 19
    max_buy_swap_ratio_1h: float = 0.95
    max_creator_launches_24h: int = 20
    min_volume_per_swap_1h: float = 31.0
    min_weighted_activity: float = 5_000.0
    min_top1_addr_type0_rate: float = 0.028
    max_top1_addr_type0_rate: float = 0.056


@dataclass(frozen=True, slots=True)
class LabelPolicy:
    """90-minute binary first-touch label: SL 0.9, TP 1.8, timeout is negative."""

    stop_loss_ratio: float = 0.9
    take_profit_ratio: float = 1.8
    window_seconds: int = 90 * 60
    history_seconds: int = 60 * 60
    label_version: str = "sl090_tp180_m90_binary_v5"


EXIT_POLICY_VERSION = "m90_tp180_sl090_v2"
LEGACY_EXIT_POLICY_VERSION = "h1_tp160_sl090_v1"


ALLOWED_QUOTE_SYMBOLS = frozenset({"SOL", "USDC", "USDT"})
EXCLUDED_TARGET_SYMBOLS = frozenset({"SOL", "USDT", "USDC", "PYUSD", "WBTC", "WETH"})

SOL_WRAPPED_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
PYUSD_MINT = "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"

ALLOWED_QUOTE_MINTS = frozenset({SOL_WRAPPED_MINT, USDC_MINT, USDT_MINT})
EXCLUDED_TARGET_MINTS = frozenset({SOL_WRAPPED_MINT, USDC_MINT, USDT_MINT, PYUSD_MINT})
