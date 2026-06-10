from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional

LAMPORTS_PER_SOL = 1_000_000_000


def to_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None or v == "":
            return default
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def to_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def lamports_to_sol(lamports: Any) -> float:
    return to_float(lamports, 0.0) / LAMPORTS_PER_SOL


def sol_to_lamports(sol: Any) -> int:
    return int(max(0.0, to_float(sol, 0.0) or 0.0) * LAMPORTS_PER_SOL)


def normalize_jito_tip_floor_to_lamports(v: Any) -> int:
    x = to_float(v, 0.0) or 0.0
    if x <= 0:
        return 1000
    if x < 1:
        return max(1000, int(x * LAMPORTS_PER_SOL))
    return max(1000, int(x))


def platform_fee_amount_raw(quote: Dict[str, Any]) -> Optional[str]:
    pf = quote.get("platformFee") if isinstance(quote, dict) else None
    if isinstance(pf, dict):
        return str(pf.get("amount")) if pf.get("amount") is not None else None
    return None


def quote_output_to_usd(
    *,
    amount_raw: Any,
    output_mint: str,
    output_decimals: int,
    sol_usd: float,
    token_usd: Optional[float] = None,
) -> Optional[float]:
    raw = to_float(amount_raw)
    if raw is None or raw <= 0:
        return None
    human = raw / (10 ** output_decimals)
    if output_mint == "So11111111111111111111111111111111111111112":
        return human * sol_usd
    if token_usd is not None and token_usd > 0:
        return human * token_usd
    return None


def compute_sim_sell_accounting(
    *,
    quote: Dict[str, Any],
    sol_usd: float,
    sell_tax: Optional[float],
    fee_upper_bound_usd: float = 0.0,
) -> Dict[str, Any]:
    expected_usd = quote_output_to_usd(
        amount_raw=quote.get("outAmount"),
        output_mint=quote.get("outputMint") or "So11111111111111111111111111111111111111112",
        output_decimals=9,
        sol_usd=sol_usd,
    ) or 0.0
    conservative_base = quote_output_to_usd(
        amount_raw=quote.get("otherAmountThreshold") or quote.get("outAmount"),
        output_mint=quote.get("outputMint") or "So11111111111111111111111111111111111111112",
        output_decimals=9,
        sol_usd=sol_usd,
    ) or expected_usd

    sell_tax_ratio = to_float(sell_tax)
    sell_tax_est_usd = None
    if sell_tax_ratio is not None and sell_tax_ratio > 0:
        sell_tax_est_usd = expected_usd * sell_tax_ratio
    else:
        sell_tax_est_usd = 0.0

    conservative_net = max(0.0, conservative_base - sell_tax_est_usd - fee_upper_bound_usd)

    return {
        "trade_value_usd_expected": expected_usd,
        "trade_value_usd_conservative": conservative_net,
        "trade_value_usd_net": conservative_net,
        "gross_value_usd": expected_usd,
        "fee_usd_est": sell_tax_est_usd + fee_upper_bound_usd,
        "fee_detail": {
            "accounting_mode": "SIM_CONSERVATIVE",
            "outAmount_usd": expected_usd,
            "otherAmountThreshold_usd": conservative_base,
            "sell_tax_ratio": sell_tax_ratio,
            "sell_tax_est_usd": sell_tax_est_usd,
            "fee_upper_bound_usd": fee_upper_bound_usd,
            "platformFee": quote.get("platformFee"),
            "platform_fee_note": "Jupiter outAmount is already after AMM/platform fees; not subtracted again.",
        },
        "accounting_source": "jupiter_quote_conservative",
        "accounting_status": "ESTIMATED",
    }


def compute_sim_buy_accounting(
    *,
    quote: Dict[str, Any],
    sol_usd: float,
    fee_upper_bound_usd: float = 0.0,
) -> Dict[str, Any]:
    in_sol = lamports_to_sol(quote.get("inAmount"))
    expected_cost_usd = in_sol * sol_usd
    conservative_cost_usd = expected_cost_usd + fee_upper_bound_usd
    return {
        "trade_value_usd_expected": -abs(expected_cost_usd),
        "trade_value_usd_conservative": -abs(conservative_cost_usd),
        "trade_value_usd_net": -abs(conservative_cost_usd),
        "gross_value_usd": expected_cost_usd,
        "fee_usd_est": fee_upper_bound_usd,
        "fee_detail": {
            "accounting_mode": "SIM_CONSERVATIVE",
            "input_sol": in_sol,
            "fee_upper_bound_usd": fee_upper_bound_usd,
            "platformFee": quote.get("platformFee"),
            "platform_fee_note": "Jupiter outAmount already reflects route/platform fee; buy cost is input SOL plus configured simulated fee upper bound.",
        },
        "accounting_source": "jupiter_quote_conservative",
        "accounting_status": "ESTIMATED",
    }


def compute_effective_price_usd(*, trade_value_usd_net: float, token_amount: float) -> Optional[float]:
    if not token_amount or token_amount <= 0:
        return None
    return abs(trade_value_usd_net) / token_amount


def extract_account_keys(result: Dict[str, Any]) -> list:
    msg = result.get("transaction", {}).get("message", {})
    keys = msg.get("accountKeys", [])
    out = []
    for k in keys:
        if isinstance(k, str):
            out.append(k)
        elif isinstance(k, dict):
            out.append(k.get("pubkey") or k.get("publicKey") or "")
    return out


def find_wallet_index(account_keys: list, wallet_pubkey: str) -> Optional[int]:
    for i, k in enumerate(account_keys):
        if k == wallet_pubkey:
            return i
    return None


def ui_amount_from_token_balance(b: Dict[str, Any]) -> float:
    amt = b.get("uiTokenAmount") or {}
    if amt.get("uiAmount") is not None:
        return float(amt.get("uiAmount"))
    s = amt.get("uiAmountString")
    return float(s) if s not in (None, "") else 0.0


def extract_token_delta_from_meta(
    meta: Dict[str, Any],
    wallet_pubkey: str,
    token_mint: str,
) -> Optional[float]:
    def collect(rows):
        total = 0.0
        for r in rows or []:
            if r.get("owner") == wallet_pubkey and r.get("mint") == token_mint:
                total += ui_amount_from_token_balance(r)
        return total

    pre = collect(meta.get("preTokenBalances"))
    post = collect(meta.get("postTokenBalances"))
    return post - pre


def summarize_tx_meta(result: Dict[str, Any]) -> Dict[str, Any]:
    meta = result.get("meta") or {}
    return {
        "fee": meta.get("fee"),
        "computeUnitsConsumed": meta.get("computeUnitsConsumed"),
        "status": result.get("transaction", {}).get("message", {}).get("recentBlockhash") not in (None, "") if isinstance(result.get("transaction", {}).get("message"), dict) else None,
        "blockTime": result.get("blockTime"),
        "slot": result.get("slot"),
    }
