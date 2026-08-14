"""README-exact local safety filters."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .constants import ALLOWED_QUOTE_SYMBOLS, EXCLUDED_TARGET_SYMBOLS, FilterThresholds, LAUNCHPADS


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
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _nonnegative_float(value: Any) -> float | None:
    parsed = to_float(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _ratio_float(value: Any) -> float | None:
    parsed = _nonnegative_float(value)
    return parsed if parsed is not None and parsed <= 1 else None


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
    }


def _known_bool(value: Any) -> bool | None:
    if value is True or value in (1, "1"):
        return True
    if isinstance(value, str) and value.strip().lower() == "true":
        return True
    if _is_false(value):
        return False
    return None


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
        "symbol": str(first(raw, ("symbol", "token_symbol", "base_symbol"), "") or "").upper(),
        "quote_symbol": str(first(raw, ("quote_symbol", "quote_token_symbol"), "") or "").upper(),
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
        "sniper_count": to_float(first(raw, ("sniper_count", "sniper_wallets", "snipers", "sniper_trader_count"))),
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

        def gt(name: str, value: Any, limit: float) -> None:
            parsed = _nonnegative_float(value)
            if parsed is None or not parsed > limit:
                fail.append(f"{name}>{limit:g}")

        def lt(name: str, value: Any, limit: float) -> None:
            parsed = _nonnegative_float(value)
            if parsed is None or not parsed < limit:
                fail.append(f"{name}<{limit:g}")

        platform = str(token.get("launchpad") or "")
        if not platform or launchpad_key(platform) not in ALLOWED_LAUNCHPAD_KEYS:
            fail.append("launchpad")
        target_symbol = str(token.get("symbol") or "").strip().upper()
        quote_symbol = str(token.get("quote_symbol") or "").strip().upper()
        if target_symbol in EXCLUDED_TARGET_SYMBOLS:
            fail.append("target_asset_excluded")
        if not quote_symbol or quote_symbol not in ALLOWED_QUOTE_SYMBOLS:
            fail.append("quote_asset_not_allowed")
        lt("rug_ratio", token.get("rug_ratio"), t.max_rug_ratio)
        lt("insider_ratio", token.get("insider_ratio"), t.max_insider_ratio)
        lt("bundler_rate", token.get("bundler_rate"), t.max_bundler_rate)
        gt("liquidity", token.get("liquidity"), t.min_liquidity)
        top10 = _ratio_float(token.get("top_10_holder_rate"))
        if top10 is None or not t.min_top_10_holder_rate <= top10 <= t.max_top_10_holder_rate:
            fail.append("top_10_holder_rate")
        lt("fresh_wallet_rate", token.get("fresh_wallet_rate"), t.max_fresh_wallet_rate)
        if str(token.get("burn_status") or "").strip().lower() != "burn":
            fail.append("burn_status")
        if _known_bool(token.get("renounced_mint")) is not True:
            fail.append("renounced_mint")
        if _known_bool(token.get("renounced_freeze_account")) is not True:
            fail.append("renounced_freeze_account")
        if _known_bool(token.get("is_wash_trading")) is not False:
            fail.append("is_wash_trading")
        lt("rat_trader_amount_rate", token.get("rat_trader_amount_rate"), t.max_rat_trader_amount_rate)
        holder_count = _nonnegative_float(token.get("holder_count"))
        if holder_count is None or not t.min_holder_count_exclusive < holder_count < t.max_holder_count_exclusive:
            fail.append("holder_count")
        gt("marketcap", token.get("marketcap"), t.min_marketcap)
        lt("sell_tax", token.get("sell_tax"), t.max_sell_tax)
        lt("buy_tax", token.get("buy_tax"), t.max_buy_tax)
        lt("sniper_count", token.get("sniper_count"), t.max_sniper_count)
        gt("age", token.get("age"), t.min_age_minutes)
        lt("age", token.get("age"), t.max_age_minutes_exclusive)
        liquidity = _nonnegative_float(token.get("liquidity"))
        if not liquidity or not holder_count or liquidity / holder_count <= t.min_liquidity_per_holder:
            fail.append("liquidity/holder_count")
        swaps = _nonnegative_float(token.get("swaps_1h"))
        volume_1h = _nonnegative_float(token.get("volume_1h"))
        gt("swaps_1h", swaps, t.min_swaps_1h)
        if not swaps or not volume_1h or volume_1h / swaps <= t.min_volume_per_swap_1h:
            fail.append("volume_1h/swaps_1h")
        smart = _nonnegative_float(token.get("smart_degen_count"))
        renowned = _nonnegative_float(token.get("renowned_count"))
        volume = _nonnegative_float(token.get("volume"))
        if volume is None:
            volume = volume_1h
        if smart is None or renowned is None or volume is None:
            fail.append("weighted_activity_data")
        elif (0.5 + smart + renowned) * volume <= t.min_weighted_activity:
            fail.append("weighted_activity")
        return FilterDecision(not fail, tuple(fail))

    def evaluate_required_facts(self, token: Mapping[str, Any]) -> FilterDecision:
        """Validate filter inputs without assuming that unknown means safe."""
        invalid: list[str] = []

        def require_text(name: str) -> None:
            if not str(token.get(name) or "").strip():
                invalid.append(name)

        def require_nonnegative(name: str) -> None:
            if _nonnegative_float(token.get(name)) is None:
                invalid.append(name)

        def require_ratio(name: str) -> None:
            if _ratio_float(token.get(name)) is None:
                invalid.append(name)

        for name in ("address", "launchpad", "symbol", "quote_symbol", "burn_status"):
            require_text(name)
        for name in (
            "rug_ratio",
            "insider_ratio",
            "bundler_rate",
            "top_10_holder_rate",
            "fresh_wallet_rate",
            "rat_trader_amount_rate",
            "sell_tax",
            "buy_tax",
        ):
            require_ratio(name)
        for name in (
            "price",
            "liquidity",
            "holder_count",
            "marketcap",
            "sniper_count",
            "age",
            "swaps_1h",
            "volume_1h",
            "smart_degen_count",
            "renowned_count",
        ):
            require_nonnegative(name)
        if _nonnegative_float(token.get("volume")) is None and _nonnegative_float(token.get("volume_1h")) is None:
            invalid.append("volume")
        for name in ("renounced_mint", "renounced_freeze_account", "is_wash_trading"):
            if _known_bool(token.get(name)) is None:
                invalid.append(name)
        reasons = tuple(f"missing_or_invalid:{name}" for name in invalid)
        return FilterDecision(not invalid, reasons)

    @staticmethod
    def top1_addr_type0_rate(holders: Sequence[Mapping[str, Any]]) -> float | None:
        for holder in holders:
            raw_type = first(holder, ("addr_type", "address_type", "type"))
            if raw_type in (None, ""):
                continue
            try:
                addr_type = int(raw_type)
            except (TypeError, ValueError):
                continue
            if addr_type != 0:
                continue
            return _ratio_float(
                first(holder, ("top1_holder_rate", "rate", "amount_percentage", "percentage", "hold_rate"))
            )
        return None

    def evaluate_top_holders(self, holders: Sequence[Mapping[str, Any]]) -> FilterDecision:
        rate = self.top1_addr_type0_rate(holders)
        accepted = (
            rate is not None
            and self.t.min_top1_addr_type0_rate < rate < self.t.max_top1_addr_type0_rate
        )
        reason = () if accepted else (f"top1_addr_type0={rate}",)
        return FilterDecision(accepted, reason)

