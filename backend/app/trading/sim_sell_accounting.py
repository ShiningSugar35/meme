"""Shared SIM sell accounting module.

Both TradingPipeline._execute_sim_paper_sell() and
PositionExitService._exit_sim() delegate to prepare_sim_sell_accounting()
so that their accounting formulas stay identical.

Centralises: price fetching, Jupiter quote, token decimals, sell tax,
conservative accounting (quote success / fallback / raw-rounds-to-zero),
and execution detail for trade_event + audit payloads.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..config import settings
from .accounting import (
    compute_sim_sell_accounting,
    compute_effective_price_usd,
)

WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_TOKEN_DECIMALS = 9


# ---------------------------------------------------------------------------
# Standalone helpers (deliberately NOT methods so both callers can use them)
# ---------------------------------------------------------------------------

def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(obj), ensure_ascii=False)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_price_usd(data: Dict[str, Any]) -> float:
    for key in ("price_usd", "latest_price_usd", "usd_price", "priceUsd"):
        v = _to_float(data.get(key))
        if v is not None and v > 0:
            return v
    v = _to_float(data.get("price"))
    return v if v is not None and v > 0 else 0.0


def _get_price_sol(data: Dict[str, Any]) -> float:
    for key in ("price_sol", "latest_price_sol", "sol_price", "priceSol"):
        v = _to_float(data.get(key))
        if v is not None and v > 0:
            return v
    return 0.0


def _derive_sol_usd_price(snapshot: Dict[str, Any], latest: Dict[str, Any]) -> Optional[float]:
    latest = latest or {}
    snapshot = snapshot or {}
    price_usd = _to_float(latest.get("price_usd") or snapshot.get("price_usd") or latest.get("latest_price_usd"))
    price_sol = _to_float(latest.get("price_sol") or snapshot.get("price_sol") or latest.get("latest_price_sol"))
    if price_usd and price_usd > 0 and price_sol and price_sol > 0:
        sol_usd = price_usd / price_sol
        if sol_usd > 0:
            return sol_usd
    liq_usd = _to_float(snapshot.get("liquidity_usd") or latest.get("liquidity_usd") or snapshot.get("liquidity"))
    sol_side_liq = _to_float(snapshot.get("sol_side_liquidity") or latest.get("sol_side_liquidity"))
    if liq_usd and liq_usd > 0 and sol_side_liq and sol_side_liq > 0:
        return liq_usd / sol_side_liq
    return None


def _extract_token_decimals(
    token_mint: str,
    quote: Optional[Dict[str, Any]] = None,
    latest: Optional[Dict[str, Any]] = None,
    strategy: Optional[Dict[str, Any]] = None,
) -> int:
    """Best-effort token decimal extraction with safe fallback (mirrors TradingPipeline)"""
    candidates: list = []
    if strategy:
        for k in ("token_decimals", "output_decimals", "decimals"):
            candidates.append(strategy.get(k))
    if latest:
        for k in ("token_decimals", "decimals", "base_decimals", "baseTokenDecimals"):
            candidates.append(latest.get(k))
    if quote:
        for k in ("outputDecimals", "outputTokenDecimals", "outDecimals",
                  "inputDecimals", "inputTokenDecimals", "decimals"):
            candidates.append(quote.get(k))
        output_mint = quote.get("outputMint")
        input_mint = quote.get("inputMint")
        for container_key in ("outputToken", "outToken", "outputMintInfo", "outMintInfo"):
            info = quote.get(container_key)
            if isinstance(info, dict):
                candidates.append(info.get("decimals"))
        for container_key in ("tokens", "mintInfos", "tokenInfos"):
            info = quote.get(container_key)
            if isinstance(info, dict):
                for mint in (output_mint, input_mint, token_mint):
                    sub = info.get(mint)
                    if isinstance(sub, dict):
                        candidates.append(sub.get("decimals"))
    for c in candidates:
        try:
            d = int(float(c))
            if 0 <= d <= 18:
                return d
        except (TypeError, ValueError):
            pass
    if latest and isinstance(latest.get("type"), str) and "pump" in latest["type"].lower():
        return 6
    return DEFAULT_TOKEN_DECIMALS


def _human_to_raw_amount(amount: float, decimals: int) -> int:
    if amount <= 0:
        return 0
    return max(0, int(math.floor(amount * (10 ** decimals))))


def _price_impact_fraction(quote: Dict[str, Any]) -> float:
    v = _to_float(quote.get("priceImpactPct"), 0.0)
    return max(0.0, v or 0.0)


def _price_impact_cap_fraction(strategy: Optional[Dict[str, Any]] = None) -> float:
    raw = None
    if strategy:
        raw = strategy.get("price_impact_hard_cap_pct")
    if raw is None:
        raw = getattr(settings, "PRICE_IMPACT_HARD_CAP_PCT", 10.0)
    cap_pct = _to_float(raw, 10.0) or 10.0
    return max(0.0, cap_pct / 100.0)


def _position_strategy_id(position: Dict[str, Any]) -> Optional[int]:
    if position.get("live_strategy_id"):
        return int(position["live_strategy_id"])
    locked = position.get("locked_strategy_config_json")
    if locked:
        try:
            cfg = json.loads(locked)
            return int(cfg.get("id") or cfg.get("strategy_id") or 0) or None
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return None


# ---------------------------------------------------------------------------
# Sell tax loader (moved from executor.py — single source of truth)
# ---------------------------------------------------------------------------

async def _load_sell_tax_for_position(repo, position, gmgn) -> Optional[float]:
    try:
        pos_id = int(position["id"])
        audits = await repo.get_position_audits(pos_id, audit_type="ENTRY")
        if audits:
            entry_json = audits[0].get("audit_json") or {}
            if isinstance(entry_json, str):
                entry_json = json.loads(entry_json)
            if isinstance(entry_json, dict) and entry_json.get("sell_tax") is not None:
                return float(entry_json["sell_tax"])
    except Exception:
        pass
    try:
        token_mint = position["token_mint"]
        snap = await repo.get_latest_token_metric_snapshot(token_mint)
        if snap and snap.get("sell_tax") is not None:
            return float(snap["sell_tax"])
    except Exception:
        pass
    try:
        token_mint = position["token_mint"]
        sec = await gmgn.fetch_token_security(token_mint) if gmgn else None
        if isinstance(sec, dict) and sec.get("sell_tax") is not None:
            return float(sec["sell_tax"])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Main helper — prepare all data needed for SIM sell trade_event + audit
# ---------------------------------------------------------------------------

async def prepare_sim_sell_accounting(
    *,
    repo,
    gmgn,
    jupiter,
    position: dict,
    exit_pct: float,
    reason_code: str = "EXIT",
    current_price_usd_override: Optional[float] = None,
) -> dict:
    """Compute SIM sell accounting values matching old TradingPipeline._execute_sim_paper_sell().

    Returns a dict with everything needed to write a trade_event, build an
    exit audit, and update / close the position — so both TradingPipeline
    and PositionExitService produce identical accounting data.
    """
    pos_id = int(position["id"])
    token_mint = position["token_mint"]
    remaining_token = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0
    pct = max(0.0, min(1.0, _to_float(exit_pct, 1.0) or 1.0))

    # ---- fetch latest price ----
    has_override = current_price_usd_override is not None and current_price_usd_override > 0
    if gmgn is not None and not has_override:
        try:
            latest = await gmgn.fetch_latest_price(token_mint)
        except Exception:
            latest = {}
    else:
        latest = {}

    current_price_usd = current_price_usd_override if has_override else None
    if current_price_usd is None:
        current_price_usd = (
            _get_price_usd(latest)
            or _to_float(position.get("last_fill_price_usd"), 0.0)
            or _to_float(position.get("entry_price_usd"), 0.0)
            or 0.0
        )
    current_price_sol = (
        _get_price_sol(latest)
        or _to_float(position.get("entry_price_sol"), 0.0)
        or 0.0
    )

    # ---- basic sell amounts ----
    sell_amount_human = remaining_token * pct
    new_remaining = max(0.0, remaining_token - sell_amount_human)
    gross_value_usd = sell_amount_human * current_price_usd  # initial fallback

    # ---- locked strategy config ----
    locked_cfg: Dict[str, Any] = {}
    locked = position.get("locked_strategy_config_json")
    if locked:
        try:
            locked_cfg = json.loads(locked)
        except (json.JSONDecodeError, TypeError):
            locked_cfg = {}

    # ---- token decimals ----
    token_decimals = _extract_token_decimals(
        token_mint, quote=None, latest=latest, strategy=locked_cfg
    )
    sell_amount_raw = _human_to_raw_amount(sell_amount_human, token_decimals)

    # ---- Jupiter quote & accounting ----
    quote: Dict[str, Any] = {}
    price_impact = None
    quote_json = None
    route_plan_json = None
    quote_ok = False
    acct: Dict[str, Any]
    fee_detail_json: str

    if sell_amount_raw > 0 and jupiter is not None:
        # Build slippage cap
        sell_slippage_cap_bps = int(
            locked_cfg.get(
                "sell_slippage_cap_bps",
                getattr(settings, "SELL_SLIPPAGE_CAP_BPS", 100),
            )
        )
        try:
            raw_quote = await jupiter.quote_exact_in(
                token_mint, WRAPPED_SOL_MINT, int(sell_amount_raw), int(sell_slippage_cap_bps)
            )
        except Exception:
            raw_quote = None

        if raw_quote and isinstance(raw_quote, dict) and not raw_quote.get("error"):
            # Validate price impact cap
            pi_frac = _price_impact_fraction(raw_quote)
            pi_cap_frac = _price_impact_cap_fraction(locked_cfg)
            if pi_frac > pi_cap_frac:
                quote = {"error": "PRICE_IMPACT_HARD_CAP", "priceImpactPct": raw_quote.get("priceImpactPct")}
            else:
                quote = raw_quote

            if not quote.get("error"):
                # ── quote SUCCESS path ──
                price_impact = _price_impact_fraction(quote)
                quote_json = _safe_json(quote)
                route_plan_json = _safe_json((quote.get("routePlan") or [])[:3])
                out_sol = (_to_float(quote.get("outAmount"), 0.0) or 0.0) / LAMPORTS_PER_SOL
                sol_usd = _derive_sol_usd_price({}, latest) or 200.0
                gross_value_usd = out_sol * sol_usd

                fee_upper_bound_usd = float(getattr(settings, "SIM_SELL_FEE_UPPER_BOUND_USD", 0.0))
                sell_tax = await _load_sell_tax_for_position(repo, position, gmgn)
                acct = compute_sim_sell_accounting(
                    quote=quote,
                    sol_usd=sol_usd,
                    sell_tax=sell_tax,
                    fee_upper_bound_usd=fee_upper_bound_usd,
                )
                fee_detail_json = json.dumps(acct["fee_detail"], ensure_ascii=False)
                quote_ok = True
            else:
                # quote hit price impact cap → fallback
                fee_upper_bound_usd = float(getattr(settings, "SIM_SELL_FEE_UPPER_BOUND_USD", 0.0))
                fallback_net = +abs(gross_value_usd - fee_upper_bound_usd)
                acct = {
                    "trade_value_usd_expected": gross_value_usd,
                    "trade_value_usd_conservative": fallback_net,
                    "trade_value_usd_net": fallback_net,
                    "gross_value_usd": gross_value_usd,
                    "fee_usd_est": fee_upper_bound_usd,
                    "fee_detail": {"fallback": True, "reason": "no_quote_or_sell_amount_raw_rounds_to_zero"},
                    "accounting_source": "gmgn_price_fallback",
                    "accounting_status": "ESTIMATED",
                }
                fee_detail_json = json.dumps(acct["fee_detail"], ensure_ascii=False)
                quote_json = None
                route_plan_json = None
                price_impact = None
        else:
            # ── quote FAILED or jupiter returned error ──
            quote = raw_quote if isinstance(raw_quote, dict) else {"error": "NO_QUOTE"}
            fee_upper_bound_usd = float(getattr(settings, "SIM_SELL_FEE_UPPER_BOUND_USD", 0.0))
            fallback_net = +abs(gross_value_usd - fee_upper_bound_usd)
            acct = {
                "trade_value_usd_expected": gross_value_usd,
                "trade_value_usd_conservative": fallback_net,
                "trade_value_usd_net": fallback_net,
                "gross_value_usd": gross_value_usd,
                "fee_usd_est": fee_upper_bound_usd,
                "fee_detail": {"fallback": True, "reason": "no_quote_or_sell_amount_raw_rounds_to_zero"},
                "accounting_source": "gmgn_price_fallback",
                "accounting_status": "ESTIMATED",
            }
            fee_detail_json = json.dumps(acct["fee_detail"], ensure_ascii=False)
    else:
        # ── sell_amount_raw <= 0 or no jupiter ──
        gross_value_usd = sell_amount_human * current_price_usd
        acct = {
            "trade_value_usd_expected": gross_value_usd,
            "trade_value_usd_conservative": gross_value_usd,
            "trade_value_usd_net": gross_value_usd,
            "gross_value_usd": gross_value_usd,
            "fee_usd_est": 0.0,
            "fee_detail": {"fallback": True, "reason": "sell_amount_raw_rounds_to_zero"},
            "accounting_source": "gmgn_price_fallback",
            "accounting_status": "ESTIMATED",
        }
        fee_detail_json = json.dumps(acct["fee_detail"], ensure_ascii=False)

    # ---- sell_price_usd_effective ----
    sell_price_effective = compute_effective_price_usd(
        trade_value_usd_net=acct["trade_value_usd_net"],
        token_amount=sell_amount_human,
    )

    # ---- execution_detail for PositionExitService + pipeline compatibility ----
    execution_detail = {
        "service": "prepare_sim_sell_accounting",
        "reason_code": reason_code,
        "pct": pct,
        "current_price_usd": current_price_usd,
        "current_price_sol": current_price_sol,
        "remaining_token_before": remaining_token,
        "sell_token_amount": sell_amount_human,
        "remaining_token_after": new_remaining,
        "sell_amount_raw": sell_amount_raw,
        "token_decimals": token_decimals,
        "quote_ok": quote_ok,
        "quote_error": quote.get("error") if isinstance(quote, dict) else None,
        "accounting_source": acct["accounting_source"],
        "accounting_status": acct["accounting_status"],
        "gross_value_usd": acct["gross_value_usd"],
        "trade_value_usd_net": acct["trade_value_usd_net"],
    }

    return {
        "pos_id": pos_id,
        "token_mint": token_mint,
        "remaining_token": remaining_token,
        "pct": pct,
        "latest": latest,
        "current_price_usd": current_price_usd,
        "current_price_sol": current_price_sol,
        "sell_amount_human": sell_amount_human,
        "new_remaining": new_remaining,
        "token_decimals": token_decimals,
        "sell_amount_raw": sell_amount_raw,
        "quote": quote,
        "quote_ok": quote_ok,
        "quote_json": quote_json,
        "route_plan_json": route_plan_json,
        "price_impact": price_impact,             # fraction (0.02 = 2 %)
        "price_impact_pct": (price_impact * 100.0) if price_impact else None,
        "gross_value_usd": gross_value_usd,
        "acct": acct,
        "fee_detail_json": fee_detail_json,
        "sell_price_effective": sell_price_effective,
        "execution_detail": execution_detail,
    }
