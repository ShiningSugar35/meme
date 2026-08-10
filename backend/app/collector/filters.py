"""README-exact local safety filters."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .constants import FilterThresholds, LAUNCHPADS


def first(mapping: Mapping[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_timestamp(value: Any) -> int | None:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed // 1_000 if parsed > 10_000_000_000 else parsed


def launchpad_key(value: Any) -> str:
    compact = re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())
    return "pumpfun" if compact == "pump" else compact


ALLOWED_LAUNCHPAD_KEYS = {launchpad_key(item) for item in LAUNCHPADS}


def canonical_launchpad(value: Any) -> str:
    wanted = launchpad_key(value)
    for launchpad in LAUNCHPADS:
        if launchpad_key(launchpad) == wanted:
            return launchpad
    return str(value or "")


def _age_minutes(raw: Mapping[str, Any], now_ts: int | None = None) -> float | None:
    direct = to_float(raw.get("age"))
    if direct is not None:
        return direct
    created = parse_timestamp(
        first(
            raw,
            (
                "pool_created_at",
                "creation_time",
                "created_at",
                "open_time",
                "launch_time",
                "created_timestamp",
                "creation_timestamp",
            ),
        )
    )
    if created is None:
        return None
    return max(0, (now_ts or int(time.time())) - created) / 60.0


def _is_false(value: Any) -> bool:
    if value is False or value in (0, "0"):
        return True
    return isinstance(value, str) and value.strip().lower() in {
        "false",
        "no",
        "none",
        "null",
    }


def normalize_token(raw: Mapping[str, Any], token_type: str) -> dict[str, Any]:
    return {
        "address": first(raw, ("token_mint", "token_address", "address", "mint", "base_address"), ""),
        "type": token_type or first(raw, ("type", "trench_type", "category"), ""),
        "launchpad": first(
            raw,
            (
                "launchpad_platform",
                "launchpad",
                "platform",
                "source_platform",
                "pool_platform",
                "exchange",
                "migrated_pool_exchange",
            ),
            "",
        ),
        "price": to_float(first(raw, ("price", "price_usd", "usd_price"))),
        "liquidity": to_float(first(raw, ("liquidity", "liquidity_usd", "pool_liquidity_usd", "reserve_usd"))),
        "holder_count": to_float(first(raw, ("holder_count", "holders", "total_holders", "holder"))),
        "marketcap": to_float(first(raw, ("market_cap", "marketcap", "fdv", "fully_diluted_valuation", "usd_market_cap"))),
        "top_10_holder_rate": to_float(first(raw, ("top_10_holder_rate", "top10_holder_rate", "top10_holder_percent", "top_10_rate"))),
        "fresh_wallet_rate": to_float(first(raw, ("fresh_wallet_rate", "fresh_wallets_rate", "fresh_rate"))),
        "rug_ratio": to_float(first(raw, ("rug_ratio", "max_rug_ratio", "max_rugged_ratio", "top_rug_percentage"))),
        "insider_ratio": to_float(first(raw, (
            "max_insider_ratio", "max-insider-ratio", "insider_ratio", "insider_ratio_max",
            "max_insider_rate", "insider_rate", "suspected_insider_hold_rate",
            "suspected_insider_rate", "top_insider_percentage", "top_insider_trader_percentage",
            "insider_trader_amount_rate", "insider_amount_rate",
        ))),
        "bundler_rate": to_float(first(raw, ("bundler_rate", "bundler_trader_amount_rate", "max_bundler_rate", "top_bundler_trader_percentage"))),
        "burn_status": first(raw, ("burn_status", "lp_burn_status", "burnt_status")),
        "renounced_mint": first(raw, ("renounced_mint", "mint_renounced", "is_mint_renounced")),
        "renounced_freeze_account": first(raw, ("renounced_freeze_account", "freeze_renounced", "is_freeze_renounced", "freeze_authority_renounced")),
        "is_wash_trading": first(raw, ("is_wash_trading", "wash_trading", "wash_trading_detected", "is_wash")),
        "rat_trader_amount_rate": to_float(first(raw, ("rat_trader_amount_rate", "rat_trader_rate", "top_rat_trader_percentage"))),
        "sell_tax": to_float(first(raw, ("sell_tax", "sell_tax_rate", "sell_tax_percent"))),
        "buy_tax": to_float(first(raw, ("buy_tax", "buy_tax_rate", "buy_tax_percent"))),
        "sniper_count": to_float(first(raw, ("sniper_count", "snipers", "sniper_trader_count"))),
        "swaps_1h": to_float(first(raw, ("swaps_1h", "swaps1h", "trade_1h", "trades_1h"))),
        "volume_1h": to_float(first(raw, ("volume_1h", "volume1h", "volume_1h_usd", "volume_h1"))),
        "volume": to_float(first(raw, ("volume", "volume_usd", "volume_24h", "volume_h24"))),
        "smart_degen_count": to_float(first(raw, ("smart_degen_count", "smartDegenCount"))),
        "renowned_count": to_float(first(raw, ("renowned_count", "renownedCount"))),
        "age": _age_minutes(raw),
    }


@dataclass(frozen=True, slots=True)
class FilterDecision:
    accepted: bool
    reasons: tuple[str, ...]


class SafetyFilter:
    def __init__(self, thresholds: FilterThresholds | None = None) -> None:
        self.t = thresholds or FilterThresholds()

    def evaluate(self, token: Mapping[str, Any]) -> FilterDecision:
        t = self.t
        fail: list[str] = []

        def gt(name: str, value: float | None, limit: float) -> None:
            if value is None or not value > limit:
                fail.append(f"{name}>{limit:g}")

        def lt(name: str, value: float | None, limit: float) -> None:
            if value is None or not value < limit:
                fail.append(f"{name}<{limit:g}")

        platform = str(token.get("launchpad") or "")
        if platform and launchpad_key(platform) not in ALLOWED_LAUNCHPAD_KEYS:
            fail.append("launchpad")
        lt("rug_ratio", to_float(token.get("rug_ratio")), t.max_rug_ratio)
        lt("insider_ratio", to_float(token.get("insider_ratio")), t.max_insider_ratio)
        lt("bundler_rate", to_float(token.get("bundler_rate")), t.max_bundler_rate)
        gt("liquidity", to_float(token.get("liquidity")), t.min_liquidity)
        top10 = to_float(token.get("top_10_holder_rate"))
        if top10 is None or not t.min_top_10_holder_rate <= top10 <= t.max_top_10_holder_rate:
            fail.append("top_10_holder_rate")
        lt("fresh_wallet_rate", to_float(token.get("fresh_wallet_rate")), t.max_fresh_wallet_rate)
        if str(token.get("burn_status") or "").strip().lower() != "burn":
            fail.append("burn_status")
        if str(token.get("type") or "") != "completed":
            if token.get("renounced_mint") not in (1, True, "1", "true", "True"):
                fail.append("renounced_mint")
            if token.get("renounced_freeze_account") not in (1, True, "1", "true", "True"):
                fail.append("renounced_freeze_account")
        if not _is_false(token.get("is_wash_trading")):
            fail.append("is_wash_trading")
        lt("rat_trader_amount_rate", to_float(token.get("rat_trader_amount_rate")), t.max_rat_trader_amount_rate)
        holder_count = to_float(token.get("holder_count"))
        if holder_count is None or not t.min_holder_count_exclusive < holder_count < t.max_holder_count_exclusive:
            fail.append("holder_count")
        gt("marketcap", to_float(token.get("marketcap")), t.min_marketcap)
        lt("sell_tax", to_float(token.get("sell_tax")), t.max_sell_tax)
        lt("buy_tax", to_float(token.get("buy_tax")), t.max_buy_tax)
        lt("sniper_count", to_float(token.get("sniper_count")), t.max_sniper_count)
        gt("age", to_float(token.get("age")), t.min_age_minutes)
        liquidity = to_float(token.get("liquidity"))
        if not liquidity or not holder_count or liquidity / holder_count <= t.min_liquidity_per_holder:
            fail.append("liquidity/holder_count")
        swaps = to_float(token.get("swaps_1h"))
        volume_1h = to_float(token.get("volume_1h"))
        gt("swaps_1h", swaps, t.min_swaps_1h)
        if not swaps or not volume_1h or volume_1h / swaps <= t.min_volume_per_swap_1h:
            fail.append("volume_1h/swaps_1h")
        smart = to_float(token.get("smart_degen_count")) or 0.0
        renowned = to_float(token.get("renowned_count")) or 0.0
        volume = to_float(token.get("volume")) or volume_1h or 0.0
        if (0.5 + smart + renowned) * volume <= t.min_weighted_activity:
            fail.append("weighted_activity")
        return FilterDecision(not fail, tuple(fail))

    def evaluate_top_holders(self, holders: Sequence[Mapping[str, Any]]) -> FilterDecision:
        rate: float | None = None
        for holder in holders:
            raw_type = first(holder, ("addr_type", "address_type", "type"), 0)
            try:
                addr_type = int(raw_type)
            except (TypeError, ValueError):
                addr_type = 0
            if addr_type != 0:
                continue
            rate = to_float(first(holder, ("top1_holder_rate", "rate", "amount_percentage", "percentage", "hold_rate")))
            break
        accepted = (
            rate is not None
            and self.t.min_top1_addr_type0_rate < rate < self.t.max_top1_addr_type0_rate
        )
        reason = () if accepted else (f"top1_addr_type0={rate}",)
        return FilterDecision(accepted, reason)

