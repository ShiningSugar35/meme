from __future__ import annotations

from typing import List, Dict, Any, Optional, Tuple
import json
import math
from datetime import datetime, timezone

from ..db.repositories import Repositories
from ..strategy.sizing import compute_entry_size_usd
from ..strategy.slippage import (
    compute_slippage_bps,
    BUY_SLIPPAGE_CAP_BPS,
    SELL_SLIPPAGE_CAP_BPS,
    EMERGENCY_SLIPPAGE_CAP_BPS,
)
from ..logging_config import logger
from ..providers.base import SwapProvider, ExecutionProvider, RpcProvider, MarketDataProvider
from ..config import settings, ProviderMode
from ..strategy.thresholds import requires_smart_degen_for_x
from .audit_builder import build_entry_audit_payload, build_exit_audit_payload
from .entry_data_gate import check_entry_data_completeness, retry_fetch_complete_snapshot
from .accounting import (
    compute_sim_buy_accounting,
    compute_sim_sell_accounting,
    compute_effective_price_usd,
    extract_account_keys,
    find_wallet_index,
    extract_token_delta_from_meta,
    summarize_tx_meta,
)


WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_TOKEN_DECIMALS = 9

EXIT_REASON_LABELS: Dict[str, str] = {
    "HARD_TP_160": "硬止盈：价格超过 1.6x，撤仓50%",
    "HARD_TP_210": "硬止盈：价格超过 2.1x，全部撤仓",
    "HARD_SL_70": "硬止损：价格低于 0.7x，撤仓50%",
    "HARD_SL_45": "硬止损：价格低于 0.45x，全部撤仓",
    "COMPLETED": "池子 type 变为 completed，全部撤仓",
    "SMART_MONEY_SELL": "聪明钱卖出触发",
    "TOP3_SMART_DEGEN_DUMP": "TOP3聪明钱减仓超过25%",
    "RISK_RECHECK_FAILED": "持仓风控复查失败",
    "DUST_FORCE_EXIT": "尘埃仓强制清仓",
    "PRICE_API_UNAVAILABLE_EXIT_PENDING": "价格接口异常，等待重试撤仓",
    "RISK_DATA_UNAVAILABLE_EXIT": "风控数据连续异常，撤仓",
}

def validate_and_select_sim_token_amount(
    *,
    size_usd: float,
    gmgn_price_usd: float,
    quote: dict | None,
    token_decimals: int | None,
    max_ratio: float = 1.1,
    min_ratio: float = 0.9,
) -> tuple[float, dict]:
    """
    Returns: (token_amount, diagnostics)

    Rules:
    1. GMGN price must be > 0, otherwise block buy (return 0.0).
    2. fallback_amount = size_usd / gmgn_price_usd.
    3. If Jupiter quote does not exist, use fallback_amount.
    4. If quote exists:
       - outAmount must be positive integer;
       - token_decimals must have a clear source (not None, > 0);
       - quote_amount = outAmount / 10^token_decimals;
       - implied_price = size_usd / quote_amount;
       - ratio = implied_price / gmgn_price_usd;
       - Only allow quote_amount if ratio in [0.9, 1.1];
       - Otherwise use fallback_amount and write diagnostics.
    """
    diag = {
        "token_amount_source": None,
        "quote_implied_price_usd": None,
        "quote_vs_gmgn_price_ratio": None,
        "token_decimals": token_decimals,
        "token_decimals_source": None,
        "quantity_validation_status": None,
    }

    if not gmgn_price_usd or gmgn_price_usd <= 0:
        diag["quantity_validation_status"] = "BLOCKED_NO_GMGN_PRICE"
        diag["token_amount_source"] = "blocked_invalid_gmgn_price"
        diag["buy_allowed"] = False
        return 0.0, diag

    fallback_amount = size_usd / gmgn_price_usd

    if not quote or not isinstance(quote, dict) or quote.get("error"):
        diag["token_amount_source"] = "no_quote"
        diag["quantity_validation_status"] = "FALLBACK_NO_QUOTE"
        return fallback_amount, diag

    out_amount_raw = quote.get("outAmount")
    out_raw: int | None = None
    if out_amount_raw is not None:
        try:
            out_raw = int(str(out_amount_raw))
        except (TypeError, ValueError):
            out_raw = None

    if out_raw is None or out_raw <= 0:
        diag["token_amount_source"] = "gmgn_spot_fallback"
        diag["quantity_validation_status"] = "FALLBACK_ZERO_OUT_AMOUNT"
        return fallback_amount, diag

    if token_decimals is None or token_decimals <= 0:
        diag["token_decimals_source"] = "missing"
        diag["token_amount_source"] = "jupiter_quote_missing_decimals"
        diag["quantity_validation_status"] = "FALLBACK_MISSING_DECIMALS"
        return fallback_amount, diag

    quote_amount = out_raw / (10 ** token_decimals)
    implied_price = size_usd / quote_amount if quote_amount > 0 else 0.0
    ratio = implied_price / gmgn_price_usd if gmgn_price_usd > 0 else float('inf')

    diag["quote_implied_price_usd"] = implied_price
    diag["quote_vs_gmgn_price_ratio"] = ratio

    if min_ratio <= ratio <= max_ratio:
        diag["token_amount_source"] = "jupiter_quote_validated"
        diag["quantity_validation_status"] = "VALIDATED"
        return quote_amount, diag
    else:
        diag["token_amount_source"] = "jupiter_quote_rejected_ratio"
        diag["quantity_validation_status"] = "FALLBACK_RATIO_OUT_OF_RANGE"
        return fallback_amount, diag


def _finite_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _derive_sol_usd_price(snapshot: Dict[str, Any], latest: Dict[str, Any]) -> Optional[float]:
    """Derive SOL/USD from GMGN token price fields or liquidity fields.

    Preferred relation: token_price_usd / token_price_sol.  If that is absent,
    fall back to liquidity_usd / sol_side_liquidity, which is less exact but
    sufficient for simulation sizing.
    """
    latest = latest or {}
    snapshot = snapshot or {}
    price_usd = _finite_float(latest.get('price_usd') or snapshot.get('price_usd') or latest.get('latest_price_usd'))
    price_sol = _finite_float(latest.get('price_sol') or snapshot.get('price_sol') or latest.get('latest_price_sol'))
    if price_usd and price_usd > 0 and price_sol and price_sol > 0:
        sol_usd = price_usd / price_sol
        if sol_usd > 0:
            return sol_usd

    liq_usd = _finite_float(snapshot.get('liquidity_usd') or latest.get('liquidity_usd') or snapshot.get('liquidity'))
    sol_side_liq = _finite_float(snapshot.get('sol_side_liquidity') or latest.get('sol_side_liquidity'))
    if liq_usd and liq_usd > 0 and sol_side_liq and sol_side_liq > 0:
        return liq_usd / sol_side_liq
    return None


def _usd_to_sol_amount(size_usd: float, snapshot: Dict[str, Any], latest: Dict[str, Any]) -> float:
    sol_usd = _derive_sol_usd_price(snapshot, latest)
    if not sol_usd or sol_usd <= 0:
        return 0.0
    return math.floor((size_usd / sol_usd) * 1_000_000_000) / 1_000_000_000


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


async def backfill_trade_event_from_solana_tx_meta(
    repo,
    rpc,
    trade_event_id: int,
    signature: str,
    wallet_pubkey: str,
    token_mint: str,
    side: str,
    sol_usd: float,
):
    tx = await rpc.get_transaction(signature)
    result = tx.get("result") if isinstance(tx, dict) else tx
    if isinstance(tx, dict) and tx.get("mode") == "MOCK":
        return {"ok": False, "error": "MOCK_NO_TX_META"}
    if not result or not result.get("meta"):
        return {"ok": False, "error": "TX_META_NOT_FOUND"}

    meta = result["meta"]
    account_keys = extract_account_keys(result)
    wallet_index = find_wallet_index(account_keys, wallet_pubkey)
    if wallet_index is None:
        return {"ok": False, "error": "WALLET_NOT_IN_TX"}

    pre = meta.get("preBalances", [])
    post = meta.get("postBalances", [])
    wallet_delta_lamports = int(post[wallet_index]) - int(pre[wallet_index])
    actual_usd = (wallet_delta_lamports / 1_000_000_000) * sol_usd

    token_delta = extract_token_delta_from_meta(meta, wallet_pubkey, token_mint)

    from .accounting import platform_fee_amount_raw as _pfar
    fee_detail = {
        "accounting_mode": "LIVE_TX_META_ACTUAL",
        "wallet_delta_lamports": wallet_delta_lamports,
        "gas_fee_lamports": meta.get("fee"),
        "computeUnitsConsumed": meta.get("computeUnitsConsumed"),
        "wallet_delta_includes_rent_or_ata": True,
        "token_delta": token_delta,
    }

    effective_price = None
    if token_delta and abs(token_delta) > 0:
        effective_price = abs(actual_usd) / abs(token_delta)

    await repo.update_trade_event_accounting(
        trade_event_id,
        trade_value_usd_actual=actual_usd,
        trade_value_usd_net=actual_usd,
        gas_fee_lamports=meta.get("fee"),
        fee_detail_json=json.dumps(fee_detail, ensure_ascii=False),
        execution_detail_json=json.dumps({"tx_meta": summarize_tx_meta(result)}, ensure_ascii=False),
        accounting_source="solana_tx_meta_actual",
        accounting_status="FINAL",
        sell_price_usd_effective=effective_price if side == "SELL" else None,
        buy_price_usd_effective=effective_price if side == "BUY" else None,
        executed_token_amount=abs(token_delta) if token_delta is not None else None,
    )
    return {"ok": True}


class TradingPipeline:
    def __init__(
        self,
        repo: Repositories,
        gmgn: MarketDataProvider,
        jupiter: SwapProvider,
        jito: ExecutionProvider,
        rpc: RpcProvider,
    ):
        self.repo = repo
        self.gmgn = gmgn
        self.jupiter = jupiter
        self.jito = jito
        self.rpc = rpc

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------
    def _safety_gate(self) -> Optional[Dict[str, Any]]:
        mode = settings.get_provider_mode()
        if mode == ProviderMode.MOCK:
            return None
        if settings.DRY_RUN:
            return {"ok": False, "error": "DRY_RUN", "message": "DRY_RUN=true blocks real trade broadcasts"}
        if not settings.JITO_ENABLED:
            return {"ok": False, "error": "JITO_DISABLED", "message": "Jito is disabled, no RPC fallback allowed"}
        if not settings.WALLET_PUBLIC_KEY:
            return {"ok": False, "error": "NO_WALLET_PUBKEY", "message": "WALLET_PUBLIC_KEY not configured"}
        if not settings.WALLET_PRIVATE_KEY_BASE58:
            return {"ok": False, "error": "NO_WALLET_PRIVKEY", "message": "WALLET_PRIVATE_KEY_BASE58 not configured"}
        return None

    def _build_idempotency_key(
        self,
        side: str,
        token_mint: str,
        strategy: Dict[str, Any],
        snapshot_id: Optional[int],
        extra: str = "",
    ) -> str:
        sid = strategy.get("id", 0)
        ver = strategy.get("config_version", 1)
        sn = snapshot_id or 0
        return f"{side}:{token_mint}:{sid}:{ver}:{sn}{extra}"

    def _round_timestamp_bucket(self, bucket_seconds: int = 30) -> str:
        ts = int(datetime.now(timezone.utc).timestamp())
        return str((ts // bucket_seconds) * bucket_seconds)

    def _safe_json(self, obj: Any) -> str:
        try:
            return json.dumps(obj, ensure_ascii=False, default=str)
        except Exception:
            return json.dumps(str(obj), ensure_ascii=False)

    def _to_float(self, value: Any, default: Optional[float] = None) -> Optional[float]:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        return v if math.isfinite(v) else default

    def _to_int(self, value: Any, default: Optional[int] = None) -> Optional[int]:
        try:
            v = int(float(value))
        except (TypeError, ValueError):
            return default
        return v if v >= 0 else default

    def _price_impact_fraction(self, quote: Dict[str, Any]) -> float:
        """Jupiter quote priceImpactPct is normally a numeric string fraction."""
        v = self._to_float(quote.get("priceImpactPct"), 0.0)
        return max(0.0, v or 0.0)

    def _price_impact_cap_fraction(self, strategy: Optional[Dict[str, Any]] = None) -> float:
        raw = None
        if strategy:
            raw = strategy.get("price_impact_hard_cap_pct")
        if raw is None:
            raw = getattr(settings, "PRICE_IMPACT_HARD_CAP_PCT", 10.0)
        cap_pct = self._to_float(raw, 10.0) or 10.0
        return max(0.0, cap_pct / 100.0)

    def _get_price_usd(self, data: Dict[str, Any]) -> float:
        for key in ("price_usd", "latest_price_usd", "usd_price", "priceUsd"):
            v = self._to_float(data.get(key))
            if v is not None and v > 0:
                return v
        # Some existing mock providers only expose `price`.
        v = self._to_float(data.get("price"))
        return v if v is not None and v > 0 else 0.0

    def _get_price_sol(self, data: Dict[str, Any]) -> float:
        for key in ("price_sol", "latest_price_sol", "sol_price", "priceSol"):
            v = self._to_float(data.get(key))
            if v is not None and v > 0:
                return v
        return 0.0

    def _get_sol_side_liquidity(self, data: Dict[str, Any]) -> float:
        for key in ("sol_side_liquidity", "latest_sol_side_liquidity", "sol_liquidity", "solLiquidity"):
            v = self._to_float(data.get(key))
            if v is not None and v > 0:
                return v
        return 0.0

    def _get_liquidity_usd(self, data: Dict[str, Any]) -> float:
        for key in ("liquidity_usd", "latest_liquidity_usd", "liquidity", "usd_liquidity", "liquidityUsd"):
            v = self._to_float(data.get(key))
            if v is not None and v > 0:
                return v
        return 0.0

    async def _build_entry_market_context(
        self,
        token_mint: str,
        latest: Dict[str, Any],
        snapshot_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Merge latest + snapshot columns + snapshot raw_json + tokens table.

        Priority (first positive wins):
          1. latest (GMGN latest-price result)
          2. token_metric_snapshot columns (e.g. liquidity_usd, price_usd)
          3. token_metric_snapshot.raw_json
          4. tokens table (latest_liquidity_usd, latest_price_usd, …)
        """
        NEEDED_KEYS = ("liquidity_usd", "price_usd", "price_sol", "sol_side_liquidity")

        def _field_ok(ctx, key):
            return (self._to_float(ctx.get(key)) or 0) > 0

        ctx = dict(latest)

        if not all(_field_ok(ctx, k) for k in NEEDED_KEYS) and snapshot_id:
            try:
                snapshot = await self.repo.get_token_metric_snapshot(snapshot_id)
            except Exception:
                snapshot = None
            if snapshot:
                # 2. Snapshot columns (e.g. liquidity_usd stored directly)
                for key in NEEDED_KEYS:
                    if not _field_ok(ctx, key):
                        val = self._to_float(snapshot.get(key))
                        if val and val > 0:
                            ctx[key] = val

                # 3. Snapshot raw_json fallback
                if not all(_field_ok(ctx, k) for k in NEEDED_KEYS):
                    raw_json = snapshot.get("raw_json")
                    raw = {}
                    if isinstance(raw_json, str):
                        try:
                            raw = json.loads(raw_json) if raw_json.strip() else {}
                        except (json.JSONDecodeError, TypeError, ValueError):
                            raw = {}
                    elif isinstance(raw_json, dict):
                        raw = raw_json
                    for key in NEEDED_KEYS:
                        if not _field_ok(ctx, key):
                            val = self._to_float(raw.get(key))
                            if val and val > 0:
                                ctx[key] = val

        # 4. Tokens table fallback
        if not all(_field_ok(ctx, k) for k in NEEDED_KEYS):
            try:
                token_row = await self.repo.get_token(token_mint)
            except Exception:
                token_row = None
            if token_row:
                for key, col in (("liquidity_usd", "latest_liquidity_usd"),
                                 ("price_usd", "latest_price_usd"),
                                 ("price_sol", "latest_price_sol")):
                    if not _field_ok(ctx, key):
                        val = self._to_float(token_row.get(col))
                        if val and val > 0:
                            ctx[key] = val

        return ctx

    def _extract_token_decimals_with_source(
        self,
        token_mint: str,
        quote: Optional[Dict[str, Any]] = None,
        latest: Optional[Dict[str, Any]] = None,
        strategy: Optional[Dict[str, Any]] = None,
    ) -> tuple[Optional[int], str]:
        """Extract decimals with a source label.  Returns (decimals, source).

        Returns (None, "missing") when no reliable decimal source is found.
        Unlike _extract_token_decimals, this does NOT fall back to DEFAULT_TOKEN_DECIMALS.
        """
        candidates: List[tuple[Any, str]] = []
        if strategy:
            candidates.extend([
                (strategy.get("token_decimals"), "strategy_config"),
                (strategy.get("output_decimals"), "strategy_config"),
                (strategy.get("decimals"), "strategy_config"),
            ])
        if latest:
            candidates.extend([
                (latest.get("token_decimals"), "gmgn_latest"),
                (latest.get("decimals"), "gmgn_latest"),
                (latest.get("base_decimals"), "gmgn_latest"),
                (latest.get("baseTokenDecimals"), "gmgn_latest"),
            ])
        if quote:
            candidates.extend([
                (quote.get("outputDecimals"), "jupiter_quote"),
                (quote.get("outputTokenDecimals"), "jupiter_quote"),
                (quote.get("outDecimals"), "jupiter_quote"),
                (quote.get("inputDecimals"), "jupiter_quote"),
                (quote.get("inputTokenDecimals"), "jupiter_quote"),
                (quote.get("decimals"), "jupiter_quote"),
            ])
            output_mint = quote.get("outputMint")
            input_mint = quote.get("inputMint")
            for container_key in ("outputToken", "outToken", "outputMintInfo", "outMintInfo"):
                info = quote.get(container_key)
                if isinstance(info, dict):
                    candidates.append((info.get("decimals"), "jupiter_quote"))
            for container_key in ("tokens", "mintInfos", "tokenInfos"):
                info = quote.get(container_key)
                if isinstance(info, dict):
                    for mint in (output_mint, input_mint, token_mint):
                        sub = info.get(mint)
                        if isinstance(sub, dict):
                            candidates.append((sub.get("decimals"), "jupiter_quote"))

        for val, src in candidates:
            d = self._to_int(val)
            if d is not None and 0 <= d <= 18:
                return d, src
        return None, "missing"

    def _extract_token_decimals(
        self,
        token_mint: str,
        quote: Optional[Dict[str, Any]] = None,
        latest: Optional[Dict[str, Any]] = None,
        strategy: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Best-effort token decimal extraction with safe fallback.

        Jupiter quote amounts are raw integer amounts before decimals. The
        position table stores human token amount, so buy converts quote outAmount
        by output decimals; sell converts human amount back by input decimals.
        """
        candidates: List[Any] = []
        if strategy:
            candidates.extend([
                strategy.get("token_decimals"),
                strategy.get("output_decimals"),
                strategy.get("decimals"),
            ])
        if latest:
            candidates.extend([
                latest.get("token_decimals"),
                latest.get("decimals"),
                latest.get("base_decimals"),
                latest.get("baseTokenDecimals"),
            ])
        if quote:
            candidates.extend([
                quote.get("outputDecimals"),
                quote.get("outputTokenDecimals"),
                quote.get("outDecimals"),
                quote.get("inputDecimals"),
                quote.get("inputTokenDecimals"),
                quote.get("decimals"),
            ])
            output_mint = quote.get("outputMint")
            input_mint = quote.get("inputMint")
            for container_key in ("outputToken", "outToken", "outputMintInfo", "outMintInfo"):
                info = quote.get(container_key)
                if isinstance(info, dict):
                    candidates.append(info.get("decimals"))
            # Some APIs expose token infos keyed by mint.
            for container_key in ("tokens", "mintInfos", "tokenInfos"):
                info = quote.get(container_key)
                if isinstance(info, dict):
                    for mint in (output_mint, input_mint, token_mint):
                        sub = info.get(mint)
                        if isinstance(sub, dict):
                            candidates.append(sub.get("decimals"))

        for c in candidates:
            d = self._to_int(c)
            if d is not None and 0 <= d <= 18:
                return d
        return DEFAULT_TOKEN_DECIMALS

    def _human_to_raw_amount(self, amount: float, decimals: int) -> int:
        if amount <= 0:
            return 0
        return max(0, int(math.floor(amount * (10 ** decimals))))

    def _raw_to_human_amount(self, amount_raw: Any, decimals: int) -> float:
        raw = self._to_float(amount_raw, 0.0) or 0.0
        if raw <= 0:
            return 0.0
        return raw / float(10 ** decimals)

    def _locked_strategy_for_position(
        self,
        strategy: Dict[str, Any],
        *,
        token_decimals: int,
        discovery_event_id: Optional[int],
        entry_size_usd: Optional[float] = None,
        top3_smart_degen_snapshot: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        locked = dict(strategy)
        locked["token_decimals"] = token_decimals
        if entry_size_usd is not None:
            locked["entry_size_usd"] = entry_size_usd
        locked["discovery_event_id"] = discovery_event_id
        locked.setdefault("buy_slippage_cap_bps", getattr(settings, "BUY_SLIPPAGE_CAP_BPS", BUY_SLIPPAGE_CAP_BPS))
        locked.setdefault("sell_slippage_cap_bps", getattr(settings, "SELL_SLIPPAGE_CAP_BPS", SELL_SLIPPAGE_CAP_BPS))
        locked.setdefault("emergency_slippage_cap_bps", getattr(settings, "EMERGENCY_SLIPPAGE_CAP_BPS", EMERGENCY_SLIPPAGE_CAP_BPS))
        locked.setdefault("price_impact_hard_cap_pct", getattr(settings, "PRICE_IMPACT_HARD_CAP_PCT", 10.0))
        if top3_smart_degen_snapshot:
            locked["top3_smart_degen_snapshot"] = top3_smart_degen_snapshot
        return self._safe_json(locked)

    async def _fetch_top3_smart_degen_snapshot(self, token_mint: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch TOP3 smart degen holders at entry time for position locking."""
        try:
            holders = await self.gmgn.fetch_smart_degen_holders(token_mint, limit=5)
        except Exception:
            return None
        if not holders:
            return None
        sorted_h = sorted(holders, key=lambda h: float(h.get("amount_percentage") or 0), reverse=True)
        top3 = []
        for h in sorted_h[:3]:
            top3.append({
                "address": h.get("address", ""),
                "amount_percentage": float(h.get("amount_percentage") or 0),
                "usd_value": float(h.get("usd_value") or 0),
            })
        return top3 if top3 else None

    def _is_emergency_exit(self, exit_reason: str) -> bool:
        r = (exit_reason or "").upper()
        return any(key in r for key in ("RISK", "SL", "STOP", "DUST", "COMPLETED", "EMERGENCY"))

    async def _get_open_live_position_by_token_cycle(
        self, token_mint: str, discovery_event_id: Optional[int]
    ) -> Optional[Dict[str, Any]]:
        if hasattr(self.repo, "get_open_live_position_by_token_and_cycle"):
            return await self.repo.get_open_live_position_by_token_and_cycle(token_mint, discovery_event_id)
        return await self.repo.get_open_live_position_by_token(token_mint)

    async def _get_wallet_balance_usd(self, snapshot: Dict[str, Any], latest: Dict[str, Any]) -> Optional[float]:
        wallet = settings.WALLET_PUBLIC_KEY
        if not wallet:
            return None
        try:
            balance = await self.rpc.get_balance(wallet)
        except Exception as e:
            await self.repo.append_system_event(
                "ERROR",
                "TRADE",
                "Live buy blocked: wallet balance fetch failed",
                self._safe_json({"error": str(e)}),
                account_type="LIVE",
            )
            return None
        sol_balance = self._to_float(balance.get("sol_balance") or balance.get("balance_sol") or balance.get("balance"), None)
        if sol_balance is None or sol_balance <= 0:
            return 0.0
        sol_usd = _derive_sol_usd_price(snapshot, latest)
        if sol_usd is None or sol_usd <= 0:
            await self.repo.append_system_event(
                "WARN",
                "TRADE",
                "Live buy blocked: cannot derive SOL/USD for wallet cap",
                self._safe_json({"wallet_balance": balance, "latest": latest}),
                account_type="LIVE",
            )
            return None
        return max(sol_balance * sol_usd, 0.0)

    # ------------------------------------------------------------------
    # Entry orchestration
    # ------------------------------------------------------------------
    async def handle_token_second_filter_result(
        self,
        token_mint: str,
        passed_strategies: List[Dict[str, Any]],
        snapshot_id: Optional[int] = None,
        discovery_event_id: Optional[int] = None,
        discovery_event_ids_by_strategy: Optional[Dict[int, int]] = None,
    ):
        if not passed_strategies:
            return {"status": "NO_PASSED_STRATEGY", "token_mint": token_mint}

        discovery_created = True
        if discovery_event_id is None and not discovery_event_ids_by_strategy:
            discovery_event_id, discovery_created = await self.repo.create_discovery_event_idempotent(
                token_mint=token_mint,
                snapshot_id=snapshot_id,
            )
            if snapshot_id is not None and not discovery_created:
                await self.repo.append_system_event(
                    "INFO",
                    "TRADE",
                    "Duplicate snapshot skipped",
                    self._safe_json({"token": token_mint, "snapshot_id": snapshot_id, "discovery_event_id": discovery_event_id}),
                    account_type="SIM",
                )
                return {"status": "SKIPPED_DUPLICATE_SNAPSHOT", "created": [], "discovery_event_id": discovery_event_id}

        # Capture TOP3 smart degen snapshot BEFORE creating positions
        # so it can be embedded in locked_strategy_config_json at creation time.
        # 仅当至少一个策略要求聪明钱时才调用 smart_degen API
        top3_snapshot = None
        requires_any_smart_degen = any(
            requires_smart_degen_for_x(
                s.get("x") if s.get("x") is not None else settings.STRATEGY_DEFAULT_X
            )
            for s in passed_strategies
        )
        if requires_any_smart_degen:
            try:
                top3_snapshot = await self._fetch_top3_smart_degen_snapshot(token_mint)
            except Exception:
                pass

        live_strategies = [s for s in passed_strategies if bool(s.get("is_live"))]
        sim_strategies = [s for s in passed_strategies if not bool(s.get("is_live"))]

        created: List[Dict[str, Any]] = []

        # Sim positions are paper tracking only. Keep one SIM position per passed
        # strategy/cycle so the bandit data is not lost.
        for strategy in sim_strategies:
            sg_id = int(strategy.get('id') or 0)
            de_id = discovery_event_id
            if discovery_event_ids_by_strategy:
                de_id = discovery_event_ids_by_strategy.get(sg_id, discovery_event_id)
            pos = await self._create_sim_position(
                token_mint, strategy, snapshot_id, de_id,
                top3_smart_degen_snapshot=top3_snapshot,
                smart_degen_required=requires_any_smart_degen,
            )
            if pos:
                created.append(pos)

        if live_strategies:
            live_enabled = (await self.repo.get_runtime_setting("live_entries_enabled")) == "true"
            if not live_enabled:
                await self.repo.append_system_event(
                    "WARN",
                    "TRADE",
                    "Live strategy passed but live trading disabled",
                    self._safe_json({
                        "token": token_mint,
                        "strategy_ids": [s.get("id") for s in live_strategies],
                        "discovery_event_id": discovery_event_id,
                    }),
                    account_type="LIVE",
                )
                return {"status": "LIVE_DISABLED", "created": created, "discovery_event_id": discovery_event_id}

            existing_positions = await self.repo.list_positions_by_token_and_is_live(token_mint, True)
            existing_open = next((p for p in existing_positions if p.get("status") != "CLOSED"), None)
            if existing_open:
                await self.repo.append_system_event(
                    "INFO",
                    "TRADE",
                    "Open live position already exists for this token",
                    self._safe_json({
                        "token": token_mint,
                        "position_id": existing_open.get("id"),
                        "discovery_event_id": discovery_event_id,
                    }),
                    account_type="LIVE",
                )
                return {"status": "SKIPPED_EXISTING_LIVE_POSITION", "created": created, "position_id": existing_open.get("id")}

            existing = await self._get_open_live_position_by_token_cycle(token_mint, discovery_event_id)
            if existing:
                await self.repo.append_system_event(
                    "INFO",
                    "TRADE",
                    "Open live position already exists for this token/cycle",
                    self._safe_json({
                        "token": token_mint,
                        "position_id": existing.get("id"),
                        "discovery_event_id": discovery_event_id,
                    }),
                    account_type="LIVE",
                )
                return {"status": "SKIPPED_EXISTING_LIVE_POSITION", "created": created, "position_id": existing.get("id")}

            # Only one live strategy is allowed by Control Center validation.
            res = await self._execute_buy(
                token_mint, live_strategies[0], snapshot_id, discovery_event_id,
                top3_smart_degen_snapshot=top3_snapshot,
                smart_degen_required=requires_any_smart_degen,
            )
            if res:
                created.append(res)

        return {"status": "OK", "created": created, "discovery_event_id": discovery_event_id}

    async def _create_sim_position(
        self,
        token_mint: str,
        strategy: Dict[str, Any],
        snapshot_id: Optional[int],
        discovery_event_id: Optional[int],
        top3_smart_degen_snapshot: Optional[List[Dict[str, Any]]] = None,
        smart_degen_required: bool = True,
    ) -> Optional[Dict[str, Any]]:
        # Per-strategy dedup: if an open SIM position already exists for this strategy+token, skip
        try:
            existing_positions = await self.repo.list_positions_by_token(token_mint)
            for ep in existing_positions:
                if ep.get("account_type") == "SIM" and ep.get("status") not in ("CLOSED",):
                    locked = ep.get("locked_strategy_config_json")
                    if locked:
                        try:
                            cfg = json.loads(locked)
                            if int(cfg.get("id", 0)) == int(strategy.get("id", 0)):
                                await self.repo.append_system_event(
                                    "INFO",
                                    "TRADE",
                                    "SIM entry skipped: open position already exists for this strategy+token",
                                    self._safe_json({
                                        "token": token_mint,
                                        "strategy_id": strategy.get("id"),
                                        "existing_position_id": ep.get("id"),
                                    }),
                                    account_type="SIM",
                                )
                                return None
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass
        except Exception:
            pass

        latest: Dict[str, Any] = {}
        try:
            latest = await self.gmgn.fetch_latest_price(token_mint)
        except Exception:
            latest = {}

        ctx = await self._build_entry_market_context(token_mint, latest, snapshot_id)
        sol_side_liquidity = self._get_sol_side_liquidity(ctx)
        liquidity_usd = self._get_liquidity_usd(ctx)
        price_usd = self._get_price_usd(ctx)
        price_sol = self._get_price_sol(ctx)
        size_usd = await compute_entry_size_usd(liquidity_usd)
        size_sol = _usd_to_sol_amount(size_usd, ctx, ctx)
        if size_usd <= 0 or size_sol <= 0:
            await self.repo.append_system_event(
                "WARN",
                "TRADE",
                "SIM entry skipped: invalid liquidity/size",
                self._safe_json({"token": token_mint, "liquidity_usd": liquidity_usd, "size_usd": size_usd, "size_sol": size_sol}),
                account_type="SIM",
            )
            return None

        token_decimals, token_decimals_source = self._extract_token_decimals_with_source(
            token_mint, latest=latest, strategy=strategy,
        )

        # Try Jupiter quote for better token-amount estimation
        token_amount = 0.0
        jupiter_price_impact = None
        quote: Dict[str, Any] = {}
        quantity_diag: Dict[str, Any] = {}
        try:
            amount_lamports = int(size_sol * LAMPORTS_PER_SOL)
            quote = await self._get_quote(
                WRAPPED_SOL_MINT,
                token_mint,
                amount_lamports,
                int(strategy.get("buy_slippage_cap_bps", getattr(settings, "BUY_SLIPPAGE_CAP_BPS", 1500))),
                token_mint=token_mint,
                strategy=strategy,
                context_id=discovery_event_id,
            )
            if quote and not quote.get("error"):
                token_decimals, token_decimals_source = self._extract_token_decimals_with_source(
                    token_mint, quote=quote, latest=latest, strategy=strategy,
                )
                jupiter_price_impact = self._price_impact_fraction(quote)
        except Exception:
            quote = {}

        token_amount, quantity_diag = validate_and_select_sim_token_amount(
            size_usd=size_usd,
            gmgn_price_usd=price_usd,
            quote=quote if quote and not quote.get("error") else None,
            token_decimals=token_decimals if token_decimals else None,
        )
        quantity_diag["token_decimals_source"] = token_decimals_source

        if quantity_diag.get("buy_allowed") is False:
            logger.warning(
                "sim_buy_blocked_invalid_gmgn_price",
                token_mint=token_mint,
                gmgn_price_usd=price_usd,
                details=quantity_diag,
            )
            if strategy:
                discovery_event_id = strategy.get("discovery_event_id") or strategy.get("event_id")
                await self.repo.insert_token_strategy_match(
                    token_mint=token_mint,
                    strategy_group_id=strategy.get("group_id", 0),
                    stage="quantity_validation",
                    x_value=(strategy.get("x") or 0.2),
                    passed=0,
                    pass_fail_detail_json=self._safe_json(quantity_diag),
                    strategy_config_json=self._safe_json(strategy),
                    event_id=discovery_event_id,
                )
            return None

        remaining_value_usd = token_amount * price_usd if token_amount > 0 and price_usd and price_usd > 0 else size_usd

        opened_at = datetime.now(timezone.utc).isoformat()
        idempotency_key = self._build_idempotency_key(
            "SIM_BUY",
            token_mint,
            strategy,
            snapshot_id,
            extra=f":d{discovery_event_id or 0}",
        )

        fee_upper_bound_usd = float(getattr(settings, "SIM_BUY_FEE_UPPER_BOUND_USD", 0.0))
        sol_usd = _derive_sol_usd_price(ctx, ctx) or 200.0
        if isinstance(quote, dict) and not quote.get("error") and quote.get("inAmount"):
            acct = compute_sim_buy_accounting(
                quote=quote,
                sol_usd=sol_usd,
                fee_upper_bound_usd=fee_upper_bound_usd,
            )
        else:
            acct = {
                "trade_value_usd_expected": -abs(size_usd),
                "trade_value_usd_conservative": -abs(size_usd + fee_upper_bound_usd),
                "trade_value_usd_net": -abs(size_usd + fee_upper_bound_usd),
                "gross_value_usd": size_usd,
                "fee_usd_est": fee_upper_bound_usd,
                "fee_detail": {"accounting_mode": "SIM_CONSERVATIVE_NO_QUOTE", "size_usd": size_usd, "fee_upper_bound_usd": fee_upper_bound_usd},
                "accounting_source": "size_usd_conservative",
                "accounting_status": "ESTIMATED",
            }

        buy_price_effective = compute_effective_price_usd(
            trade_value_usd_net=acct["trade_value_usd_net"],
            token_amount=token_amount,
        )

        # ---- Entry data gate: block buy if required fields missing ----
        gmgn_mode = getattr(self.gmgn, "mode", None)
        if gmgn_mode != ProviderMode.MOCK:
            complete_snap, gate_report = await retry_fetch_complete_snapshot(self.gmgn, token_mint)
            if gate_report.blocked:
                logger.warning(
                    "entry_data_gate_blocked",
                    token_mint=token_mint,
                    missing=gate_report.missing_fields,
                    abnormal=gate_report.abnormal_fields,
                )
                if strategy:
                    await self.repo.insert_token_strategy_match(
                        token_mint=token_mint,
                        strategy_group_id=strategy.get("group_id", 0),
                        stage="entry_data_gate",
                        x_value=(strategy.get("x") or 0.2),
                        passed=0,
                        pass_fail_detail_json=self._safe_json(gate_report.__dict__),
                        strategy_config_json=self._safe_json(strategy),
                        event_id=strategy.get("discovery_event_id") or strategy.get("event_id"),
                    )
                return None
            # Merge complete snapshot fields into ctx (snapshot overrides ctx)
            if complete_snap:
                ctx = {**ctx, **complete_snap}
                # Recompute values from enriched ctx
                price_usd = self._get_price_usd(ctx)
                price_sol = self._get_price_sol(ctx)
                sol_side_liquidity = self._get_sol_side_liquidity(ctx)

        te = await self.repo.append_trade_event(
            idempotency_key,
            token_mint=token_mint,
            strategy_id=strategy.get("id"),
            side="BUY",
            event_type="SIM_BUY",
            status="CONFIRMED",
            is_live=0,
            account_type="SIM",
            requested_sol_amount=size_sol,
            executed_sol_amount=size_sol,
            executed_token_amount=token_amount,
            price_usd=price_usd,
            price_sol=price_sol,
            price_impact_pct=(jupiter_price_impact * 100.0) if jupiter_price_impact else None,
            trade_value_usd_net=acct["trade_value_usd_net"],
            trade_value_usd_expected=acct["trade_value_usd_expected"],
            trade_value_usd_conservative=acct["trade_value_usd_conservative"],
            gross_value_usd=acct["gross_value_usd"],
            fee_usd_est=acct["fee_usd_est"],
            fee_detail_json=json.dumps(acct["fee_detail"], ensure_ascii=False),
            accounting_source=acct["accounting_source"],
            accounting_status=acct["accounting_status"],
            buy_price_usd_effective=buy_price_effective,
            platform_fee_amount=acct["fee_detail"].get("platformFee_amount"),
            quote_json=self._safe_json(quote) if quote else None,
            route_plan_json=self._safe_json((quote.get("routePlan") or [])[:3]) if quote else None,
            input_amount_raw=str(int(size_sol * LAMPORTS_PER_SOL)),
            output_amount_raw=quote.get("outAmount") if isinstance(quote, dict) and quote.get("outAmount") else None,
            quote_out_amount_raw=quote.get("outAmount") if isinstance(quote, dict) else None,
            quote_price_impact_pct=quote.get("priceImpactPct") if isinstance(quote, dict) else None,
            input_mint=WRAPPED_SOL_MINT,
            output_mint=token_mint,
            execution_detail_json=self._safe_json(quantity_diag),
            token_amount_source=quantity_diag.get("token_amount_source"),
            quote_implied_price_usd=quantity_diag.get("quote_implied_price_usd"),
            quote_vs_gmgn_price_ratio=quantity_diag.get("quote_vs_gmgn_price_ratio"),
            token_decimals=token_decimals,
            token_decimals_source=quantity_diag.get("token_decimals_source"),
            quantity_validation_status=quantity_diag.get("quantity_validation_status"),
        )

        locked_json = self._locked_strategy_for_position(
            strategy,
            token_decimals=token_decimals,
            discovery_event_id=discovery_event_id,
            entry_size_usd=size_usd,
            top3_smart_degen_snapshot=top3_smart_degen_snapshot,
        )
        pos_id = await self.repo.create_position(
            token_mint=token_mint,
            is_live=False,
            locked_strategy_config_json=locked_json,
            status="POSITION_OPEN",
            entry_price_usd=price_usd,
            entry_token_amount=token_amount,
            remaining_token_amount=token_amount,
            remaining_value_usd=remaining_value_usd,
            opened_at=opened_at,
            live_strategy_id=None,
            strategy_config_version=strategy.get("config_version", 1),
            open_trade_event_id=te.get("id"),
            last_fill_at=opened_at,
            last_fill_price_usd=price_usd,
            discovery_event_id=discovery_event_id,
            account_type="SIM",
            legacy_config_status="VALID",
        )

        te_id = te.get("id")
        if te_id is not None:
            await self.repo.attach_trade_event_to_position(te_id, pos_id)

        await self.repo.insert_bandit_observation(
            token_mint,
            strategy.get("id", 0),
            False,
            self._safe_json({"entry_price_usd": price_usd, "size_usd": size_usd, "jupiter_impact": jupiter_price_impact}),
            self._safe_json(strategy),
            position_id=pos_id,
            discovery_event_id=discovery_event_id,
        )
        await self.repo.insert_strategy_match(
            token_mint,
            strategy.get("id", 0),
            strategy.get("config_version", 1),
            snapshot_id,
            "sim_executed",
            True,
            "{}",
            "{}",
            discovery_event_id=discovery_event_id,
        )
        if discovery_event_id:
            await self.repo.update_discovery_event_status(discovery_event_id, "SIM_POSITION_OPEN")

        entry_audit = await build_entry_audit_payload(
            repo=self.repo,
            gmgn=self.gmgn,
            token_mint=token_mint,
            position_id=pos_id,
            account_type="SIM",
            strategy=strategy,
            discovery_event_id=discovery_event_id,
            snapshot_id=snapshot_id,
            buy_trade_event=te,
            quote=quote,
            token_amount=token_amount,
            price_usd=price_usd,
            price_sol=price_sol,
            size_usd=size_usd,
            liquidity_usd=liquidity_usd,
            sol_side_liquidity=sol_side_liquidity,
            smart_degen_required=smart_degen_required,
        )
        entry_audit["quantity_diag"] = quantity_diag
        entry_audit["quantity_validation_status"] = quantity_diag.get("quantity_validation_status")
        await self.repo.insert_position_audit(
            position_id=pos_id,
            token_mint=token_mint,
            account_type="SIM",
            strategy_id=strategy.get("id"),
            discovery_event_id=discovery_event_id,
            snapshot_id=snapshot_id,
            audit_type="ENTRY",
            audit_json=entry_audit,
        )

        return {"account_type": "SIM", "position_id": pos_id, "trade_event_id": te.get("id")}

    async def _execute_buy(
        self,
        token_mint: str,
        strategy: Dict[str, Any],
        snapshot_id: Optional[int] = None,
        discovery_event_id: Optional[int] = None,
        top3_smart_degen_snapshot: Optional[List[Dict[str, Any]]] = None,
        smart_degen_required: bool = True,
    ) -> Optional[Dict[str, Any]]:
        gate = self._safety_gate()
        if gate:
            await self.repo.append_system_event(
                "WARN",
                "TRADE",
                "Buy blocked by safety gate",
                self._safe_json({"token": token_mint, "reason": gate["error"]}),
                account_type="LIVE",
            )
            return gate

        try:
            latest = await self.gmgn.fetch_latest_price(token_mint)
        except Exception as e:
            await self.repo.append_system_event(
                "ERROR",
                "TRADE",
                "Buy blocked: latest price fetch failed",
                self._safe_json({"token": token_mint, "error": str(e)}),
                account_type="LIVE",
            )
            return {"ok": False, "error": "LATEST_PRICE_FAILED"}

        ctx = await self._build_entry_market_context(token_mint, latest, snapshot_id)
        sol_side_liquidity = self._get_sol_side_liquidity(ctx)
        liquidity_usd = self._get_liquidity_usd(ctx)
        price_usd = self._get_price_usd(ctx)
        price_sol = self._get_price_sol(ctx)
        wallet_balance_usd = await self._get_wallet_balance_usd(ctx, ctx)
        size_usd = await compute_entry_size_usd(
            liquidity_usd,
            wallet_balance_usd=wallet_balance_usd,
            is_live=True,
        )
        size_sol = _usd_to_sol_amount(size_usd, ctx, ctx)
        if size_usd <= 0 or size_sol <= 0:
            await self.repo.append_system_event(
                "WARN",
                "TRADE",
                "Buy skipped: live computed size is below minimum or invalid",
                self._safe_json({
                    "token": token_mint,
                    "liquidity_usd": liquidity_usd,
                    "wallet_balance_usd": wallet_balance_usd,
                    "size_usd": size_usd,
                    "size_sol": size_sol,
                    "live_min_entry_usd": 10,
                }),
                account_type="LIVE",
            )
            return {"ok": False, "error": "INVALID_ENTRY_SIZE_OR_BELOW_MIN"}

        # ---- Entry data gate: block live buy if required fields missing ----
        gmgn_mode = getattr(self.gmgn, "mode", None)
        if gmgn_mode != ProviderMode.MOCK:
            complete_snap, gate_report = await retry_fetch_complete_snapshot(self.gmgn, token_mint)
            if gate_report.blocked:
                logger.warning(
                    "live_entry_data_gate_blocked",
                    token_mint=token_mint,
                    missing=gate_report.missing_fields,
                    abnormal=gate_report.abnormal_fields,
                )
                await self.repo.append_system_event(
                    "WARN", "TRADE",
                    "Live buy blocked by entry data gate",
                    self._safe_json({"token": token_mint, "missing": gate_report.missing_fields,
                                     "abnormal": gate_report.abnormal_fields}),
                    account_type="LIVE",
                )
                return {"ok": False, "error": "ENTRY_DATA_GATE_BLOCKED"}
            if complete_snap:
                ctx = {**ctx, **complete_snap}
                price_usd = self._get_price_usd(ctx)
                price_sol = self._get_price_sol(ctx)

        buy_cap = int(strategy.get("buy_slippage_cap_bps", getattr(settings, "BUY_SLIPPAGE_CAP_BPS", BUY_SLIPPAGE_CAP_BPS)))
        slippage_bps = await compute_slippage_bps(
            size_sol,
            sol_side_liquidity,
            buy_cap,
            side="BUY",
            recent_volatility_pct=latest.get("volatility_pct") or latest.get("volatility_60s_pct"),
        )

        amount_lamports = int(size_sol * LAMPORTS_PER_SOL)
        quote = await self._get_quote(
            WRAPPED_SOL_MINT,
            token_mint,
            amount_lamports,
            slippage_bps,
            token_mint=token_mint,
            strategy=strategy,
            context_id=discovery_event_id,
        )
        if quote.get("error"):
            await self.repo.append_system_event(
                "ERROR",
                "TRADE",
                "Buy quote blocked",
                self._safe_json({"token": token_mint, "error": quote.get("error")}),
                account_type="LIVE",
            )
            return {"ok": False, "error": quote.get("error")}

        token_decimals = self._extract_token_decimals(token_mint, quote=quote, latest=latest, strategy=strategy)
        out_amount_raw = self._to_float(quote.get("outAmount"), 0.0) or 0.0
        token_amount = self._raw_to_human_amount(out_amount_raw, token_decimals)
        remaining_value_usd = token_amount * price_usd if token_amount > 0 and price_usd > 0 else 0.0

        sid = strategy.get("id", 0)
        idem = self._build_idempotency_key(
            "BUY",
            token_mint,
            strategy,
            snapshot_id,
            extra=f":d{discovery_event_id or 0}",
        )
        te_pending = await self.repo.append_trade_event(
            idem + ":PENDING",
            token_mint=token_mint,
            strategy_id=sid,
            side="BUY",
            event_type="BUY_PENDING",
            status="PENDING",
            is_live=1,
            account_type="LIVE",
            requested_sol_amount=size_sol,
            requested_token_amount=token_amount,
            price_usd=price_usd,
            price_sol=price_sol,
            slippage_bps=slippage_bps,
            price_impact_pct=self._price_impact_fraction(quote) * 100.0,
            quote_json=self._safe_json(quote),
            route_plan_json=self._safe_json((quote.get("routePlan") or [])[:3]),
            provider="JUPITER",
        )

        wallet_pubkey = settings.WALLET_PUBLIC_KEY or "MOCK_WALLET"
        instructions = await self.jupiter.build_swap_instructions(quote, wallet_pubkey, extra={})
        bundle = await self.jito.send(instructions)

        sol_usd = _derive_sol_usd_price(ctx, ctx) or 200.0
        priority_fee = instructions.get("prioritizationFeeLamports")
        te_confirmed = await self.repo.append_trade_event(
            idem + ":CONFIRMED",
            position_id=None,
            token_mint=token_mint,
            strategy_id=sid,
            side="BUY",
            event_type="BUY_CONFIRMED",
            status="CONFIRMED" if bundle.get("ok", True) else "FAILED",
            is_live=1,
            account_type="LIVE",
            requested_sol_amount=size_sol,
            requested_token_amount=token_amount,
            executed_sol_amount=size_sol if bundle.get("ok", True) else None,
            executed_token_amount=token_amount if bundle.get("ok", True) else None,
            price_usd=price_usd,
            price_sol=price_sol,
            slippage_bps=slippage_bps,
            price_impact_pct=self._price_impact_fraction(quote) * 100.0,
            quote_json=self._safe_json(quote),
            route_plan_json=self._safe_json((quote.get("routePlan") or [])[:3]),
            jito_tip_lamports=bundle.get("jito_tip_lamports"),
            priority_fee_lamports=priority_fee if priority_fee is not None else bundle.get("priority_fee_lamports"),
            tx_signature=bundle.get("signature"),
            bundle_id=bundle.get("bundle_id"),
            error_code=bundle.get("error_code"),
            error_message=bundle.get("error_message"),
            provider="JITO",
            trade_value_usd_net=-abs(size_usd),
            trade_value_usd_expected=-abs(size_usd),
            accounting_source="jupiter_quote_expected",
            accounting_status="PENDING_RPC_BACKFILL",
            fee_detail_json=json.dumps({"accounting_mode": "LIVE_PENDING", "note": "Awaiting Solana tx meta backfill"}, ensure_ascii=False),
        )

        te_confirmed_id = te_confirmed.get("id")
        signature = bundle.get("signature")

        if not bundle.get("ok", True):
            await self.repo.append_system_event(
                "ERROR",
                "TRADE",
                "Buy execution failed",
                self._safe_json({"token": token_mint, "bundle": bundle}),
                account_type="LIVE",
            )
            return {"ok": False, "error": bundle.get("error_code") or "BUNDLE_FAILED", "trade_event_id": te_confirmed_id}

        opened_at = datetime.now(timezone.utc).isoformat()
        locked_json = self._locked_strategy_for_position(
            strategy,
            token_decimals=token_decimals,
            discovery_event_id=discovery_event_id,
            entry_size_usd=size_usd,
            top3_smart_degen_snapshot=top3_smart_degen_snapshot,
        )
        pos_id = await self.repo.create_position(
            token_mint=token_mint,
            is_live=True,
            locked_strategy_config_json=locked_json,
            status="POSITION_OPEN",
            entry_price_usd=price_usd,
            entry_token_amount=token_amount,
            remaining_token_amount=token_amount,
            remaining_value_usd=remaining_value_usd,
            opened_at=opened_at,
            live_strategy_id=sid,
            strategy_config_version=strategy.get("config_version", 1),
            open_trade_event_id=te_confirmed.get("id"),
            last_fill_at=opened_at,
            last_fill_price_usd=price_usd,
            discovery_event_id=discovery_event_id,
            account_type="LIVE",
            legacy_config_status="VALID",
        )

        if te_confirmed_id is not None:
            await self.repo.attach_trade_event_to_position(te_confirmed_id, pos_id)

        if signature and te_confirmed_id:
            try:
                backfill_result = await backfill_trade_event_from_solana_tx_meta(
                    repo=self.repo,
                    rpc=self.rpc,
                    trade_event_id=te_confirmed_id,
                    signature=signature,
                    wallet_pubkey=wallet_pubkey,
                    token_mint=token_mint,
                    side="BUY",
                    sol_usd=sol_usd,
                )
                if not backfill_result.get("ok"):
                    await self.repo.append_system_event(
                        "WARN", "TRADE",
                        "Backfill failed for live buy",
                        json.dumps({"te_id": te_confirmed_id, "sig": signature, "err": backfill_result.get("error")}),
                        account_type="LIVE",
                    )
            except Exception as e:
                await self.repo.append_system_event(
                    "WARN", "TRADE",
                    "Backfill error for live buy",
                    json.dumps({"te_id": te_confirmed_id, "error": str(e)}),
                    account_type="LIVE",
                )

        await self.repo.insert_bandit_observation(
            token_mint,
            sid,
            True,
            self._safe_json({"entry_price_usd": price_usd, "size_usd": size_usd}),
            self._safe_json(strategy),
            position_id=pos_id,
            discovery_event_id=discovery_event_id,
        )
        await self.repo.insert_strategy_match(
            token_mint,
            sid,
            strategy.get("config_version", 1),
            snapshot_id,
            "live_executed",
            True,
            "{}",
            "{}",
            discovery_event_id=discovery_event_id,
        )
        if discovery_event_id:
            await self.repo.update_discovery_event_status(discovery_event_id, "LIVE_POSITION_OPEN")

        await self.repo.append_system_event(
            "INFO",
            "TRADE",
            "Buy executed",
            self._safe_json({"token": token_mint, "position_id": pos_id, "trade_event_id": te_confirmed.get("id")}),
            account_type="LIVE",
        )

        entry_audit = await build_entry_audit_payload(
            repo=self.repo,
            gmgn=self.gmgn,
            token_mint=token_mint,
            position_id=pos_id,
            account_type="LIVE",
            strategy=strategy,
            discovery_event_id=discovery_event_id,
            snapshot_id=snapshot_id,
            buy_trade_event=te_confirmed,
            quote=quote,
            token_amount=token_amount,
            price_usd=price_usd,
            price_sol=price_sol,
            size_usd=size_usd,
            liquidity_usd=liquidity_usd,
            sol_side_liquidity=sol_side_liquidity,
            smart_degen_required=smart_degen_required,
        )
        await self.repo.insert_position_audit(
            position_id=pos_id,
            token_mint=token_mint,
            account_type="LIVE",
            strategy_id=sid,
            discovery_event_id=discovery_event_id,
            snapshot_id=snapshot_id,
            audit_type="ENTRY",
            audit_json=entry_audit,
        )

        return {"ok": True, "account_type": "LIVE", "position_id": pos_id, "trade_event_id": te_confirmed.get("id")}

    # ------------------------------------------------------------------
    # Exit execution
    # ------------------------------------------------------------------
    @staticmethod
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

    async def _execute_sim_paper_sell(
        self,
        position: Dict[str, Any],
        exit_pct: float,
        exit_reason: str,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Paper-sell a SIM position — try Jupiter quote, no Jito send."""
        pos_id = int(position["id"])
        token_mint = position["token_mint"]
        remaining_token = self._to_float(position.get("remaining_token_amount"), 0.0) or 0.0
        pct = max(0.0, min(1.0, self._to_float(exit_pct, 1.0) or 1.0))

        if remaining_token <= 0:
            return {"ok": False, "error": "ZERO_REMAINING"}
        if pct <= 0:
            return {"ok": False, "error": "ZERO_EXIT_PCT"}

        try:
            latest = await self.gmgn.fetch_latest_price(token_mint)
        except Exception:
            latest = {}
        current_price_usd = self._get_price_usd(latest) or self._to_float(position.get("last_fill_price_usd"), 0.0) or 0.0
        current_price_sol = self._get_price_sol(latest) or self._to_float(position.get("entry_price_sol"), 0.0) or 0.0

        sell_amount_human = remaining_token * pct
        gross_value_usd = sell_amount_human * current_price_usd

        locked_cfg: Dict[str, Any] = {}
        locked = position.get("locked_strategy_config_json")
        if locked:
            try:
                locked_cfg = json.loads(locked)
            except (json.JSONDecodeError, TypeError):
                locked_cfg = {}

        token_decimals = self._extract_token_decimals(token_mint, latest=latest, strategy=locked_cfg)
        sell_amount_raw = self._human_to_raw_amount(sell_amount_human, token_decimals)

        quote: Dict[str, Any] = {}
        price_impact = None
        quote_json = None
        route_plan_json = None
        fee_detail = None
        if sell_amount_raw > 0:
            try:
                sell_cap = int(locked_cfg.get("sell_slippage_cap_bps", getattr(settings, "SELL_SLIPPAGE_CAP_BPS", SELL_SLIPPAGE_CAP_BPS)))
                quote = await self._get_quote(
                    token_mint,
                    WRAPPED_SOL_MINT,
                    sell_amount_raw,
                    sell_cap,
                    token_mint=token_mint,
                    strategy=locked_cfg,
                    context_id=pos_id,
                )
            except Exception:
                quote = {}
            if quote and not quote.get("error"):
                price_impact = self._price_impact_fraction(quote)
                quote_json = self._safe_json(quote)
                route_plan_json = self._safe_json((quote.get("routePlan") or [])[:3])
                out_sol = (self._to_float(quote.get("outAmount"), 0.0) or 0.0) / LAMPORTS_PER_SOL
                sol_usd = _derive_sol_usd_price({}, latest) or 200.0
                gross_value_usd = out_sol * sol_usd

                fee_upper_bound_usd = float(getattr(settings, "SIM_SELL_FEE_UPPER_BOUND_USD", 0.0))
                sell_tax = await _load_sell_tax_for_position(self.repo, position, self.gmgn)
                acct = compute_sim_sell_accounting(
                    quote=quote,
                    sol_usd=sol_usd,
                    sell_tax=sell_tax,
                    fee_upper_bound_usd=fee_upper_bound_usd,
                )
                fee_detail = json.dumps(acct["fee_detail"], ensure_ascii=False)
            else:
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
                fee_detail = json.dumps(acct["fee_detail"], ensure_ascii=False)
        else:
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
            fee_detail = json.dumps(acct["fee_detail"], ensure_ascii=False)

        sell_price_effective = compute_effective_price_usd(
            trade_value_usd_net=acct["trade_value_usd_net"],
            token_amount=sell_amount_human,
        )

        te = await self.repo.append_trade_event(
            f"SIM_SELL:{pos_id}:{exit_reason}",
            position_id=pos_id,
            token_mint=token_mint,
            strategy_id=self._position_strategy_id(position),
            side="SELL",
            event_type="SIM_SELL",
            status="CONFIRMED",
            is_live=0,
            account_type="SIM",
            requested_pct=pct,
            requested_token_amount=sell_amount_human,
            executed_token_amount=sell_amount_human,
            price_usd=current_price_usd,
            exit_reason=exit_reason,
            exit_reason_label=EXIT_REASON_LABELS.get(exit_reason, exit_reason),
            gross_value_usd=acct["gross_value_usd"],
            trade_value_usd_net=acct["trade_value_usd_net"],
            trade_value_usd_expected=acct["trade_value_usd_expected"],
            trade_value_usd_conservative=acct["trade_value_usd_conservative"],
            fee_usd_est=acct["fee_usd_est"],
            fee_detail_json=fee_detail,
            accounting_source=acct["accounting_source"],
            accounting_status=acct["accounting_status"],
            sell_price_usd_effective=sell_price_effective,
            platform_fee_amount=acct.get("fee_detail", {}).get("platformFee_amount"),
            provider="PIPELINE_SIM",
            price_impact_pct=(price_impact * 100.0) if price_impact else None,
            quote_json=quote_json,
            route_plan_json=route_plan_json,
        )

        exit_audit = await build_exit_audit_payload(
            repo=self.repo,
            position=position,
            sell_trade_event=te,
            exit_reason=exit_reason,
            exit_pct=pct,
            sell_amount_human=sell_amount_human,
            gross_value_usd=gross_value_usd,
            current_price_usd=current_price_usd,
            current_price_sol=current_price_sol,
            quote=quote,
            **(audit_context or {}),
        )
        await self.repo.insert_position_audit(
            position_id=pos_id,
            token_mint=token_mint,
            account_type="SIM",
            strategy_id=self._position_strategy_id(position),
            discovery_event_id=position.get("discovery_event_id"),
            snapshot_id=None,
            audit_type="EXIT",
            audit_json=exit_audit,
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        if pct >= 0.999999:
            await self.repo.close_position(
                pos_id,
                closed_at=now_iso,
                close_reason=exit_reason,
            )
        else:
            new_remaining = max(0.0, remaining_token - sell_amount_human)
            remaining_value_usd = new_remaining * current_price_usd if current_price_usd > 0 else 0.0
            await self.repo.update_position_remaining(
                pos_id,
                new_remaining,
                remaining_value_usd,
                last_fill_at=now_iso,
                last_fill_price_usd=current_price_usd,
            )

        await self.repo.append_system_event(
            "INFO", "TRADE",
            "SIM sell executed (paper)",
            self._safe_json({
                "position_id": pos_id,
                "exit_pct": pct,
                "exit_reason": exit_reason,
                "trade_event_id": te.get("id"),
                "jupiter_quote_ok": bool(quote and not quote.get("error")),
            }),
            account_type="SIM",
        )
        return {"ok": True, "trade_event_id": te.get("id"), "executed_sol_amount": 0.0}

    async def execute_sell(
        self,
        position: Dict[str, Any],
        exit_pct: float = 1.0,
        exit_reason: str = "EXIT",
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        # ---- SIM/LIVE hard protection ----
        account_type = position.get("account_type", "LIVE" if position.get("is_live") else "SIM")
        is_live = bool(position.get("is_live"))

        if not is_live or account_type != "LIVE":
            return await self._execute_sim_paper_sell(position, exit_pct, exit_reason, audit_context=audit_context)

        # ---- LIVE path: safety gate then real execution ----
        gate = self._safety_gate()
        if gate:
            await self.repo.append_system_event(
                "WARN",
                "TRADE",
                "Sell blocked by safety gate",
                self._safe_json({"position_id": position.get("id"), "reason": gate["error"]}),
                account_type=account_type,
            )
            return gate

        pos_id = position["id"]
        token_mint = position["token_mint"]
        remaining_token = self._to_float(position.get("remaining_token_amount"), 0.0) or 0.0
        if remaining_token <= 0:
            await self.repo.append_system_event(
                "WARN",
                "TRADE",
                "Sell blocked: zero remaining",
                self._safe_json({"position_id": pos_id}),
                account_type=account_type,
            )
            return {"ok": False, "error": "ZERO_REMAINING"}

        pct = max(0.0, min(1.0, self._to_float(exit_pct, 1.0) or 1.0))
        if pct <= 0:
            return {"ok": False, "error": "ZERO_EXIT_PCT"}

        locked_cfg: Dict[str, Any] = {}
        locked = position.get("locked_strategy_config_json")
        if locked:
            try:
                locked_cfg = json.loads(locked)
            except (json.JSONDecodeError, TypeError):
                locked_cfg = {}

        try:
            latest = await self.gmgn.fetch_latest_price(token_mint)
        except Exception:
            latest = {}

        current_price_usd = self._get_price_usd(latest) or self._to_float(position.get("last_fill_price_usd"), 0.0) or 0.0
        current_price_sol = self._get_price_sol(latest) or self._to_float(position.get("entry_price_sol"), 0.0) or 0.0
        sol_side_liquidity = self._get_sol_side_liquidity(latest)

        remaining_value_usd = remaining_token * current_price_usd if current_price_usd > 0 else 0.0
        if remaining_value_usd > 0 and remaining_value_usd < getattr(settings, "DUST_FORCE_EXIT_USD", 12.5):
            pct = 1.0
            exit_reason = "DUST_FORCE_EXIT"

        sell_amount_human = remaining_token * pct
        gross_value_usd = sell_amount_human * current_price_usd
        token_decimals = self._extract_token_decimals(token_mint, latest=latest, strategy=locked_cfg)
        sell_amount_raw = self._human_to_raw_amount(sell_amount_human, token_decimals)
        if sell_amount_raw <= 0:
            return {"ok": False, "error": "SELL_AMOUNT_ROUNDS_TO_ZERO", "token_decimals": token_decimals}

        sell_cap = int(locked_cfg.get("sell_slippage_cap_bps", getattr(settings, "SELL_SLIPPAGE_CAP_BPS", SELL_SLIPPAGE_CAP_BPS)))
        emergency_cap = int(locked_cfg.get("emergency_slippage_cap_bps", getattr(settings, "EMERGENCY_SLIPPAGE_CAP_BPS", EMERGENCY_SLIPPAGE_CAP_BPS)))
        emergency = self._is_emergency_exit(exit_reason) or pct >= 1.0
        cap = emergency_cap if emergency else sell_cap
        order_size_sol = sell_amount_human * current_price_sol if current_price_sol > 0 else 0.0
        slippage_bps = await compute_slippage_bps(
            order_size_sol,
            sol_side_liquidity,
            cap,
            side="SELL",
            emergency=emergency,
            recent_volatility_pct=latest.get("volatility_pct") or latest.get("volatility_60s_pct"),
        )

        quote = await self._get_quote(
            token_mint,
            WRAPPED_SOL_MINT,
            sell_amount_raw,
            slippage_bps,
            token_mint=token_mint,
            strategy=locked_cfg,
            context_id=pos_id,
        )
        if quote.get("error"):
            await self.repo.append_system_event(
                "ERROR",
                "TRADE",
                "Sell quote blocked",
                self._safe_json({"position_id": pos_id, "error": quote.get("error")}),
                account_type=account_type,
            )
            return {"ok": False, "error": quote.get("error")}

        out_sol = (self._to_float(quote.get("outAmount"), 0.0) or 0.0) / LAMPORTS_PER_SOL
        sol_usd = _derive_sol_usd_price({}, latest) or 200.0
        gross_value_usd = out_sol * sol_usd
        bucket = self._round_timestamp_bucket()
        idem_pending = f"SELL:{pos_id}:{exit_reason}:{bucket}:PENDING"
        te_pending = await self.repo.append_trade_event(
            idem_pending,
            position_id=pos_id,
            token_mint=token_mint,
            strategy_id=self._position_strategy_id(position),
            side="SELL",
            event_type="SELL_PENDING",
            status="PENDING",
            is_live=1,
            account_type="LIVE",
            requested_pct=pct,
            requested_token_amount=sell_amount_human,
            requested_sol_amount=out_sol,
            price_usd=current_price_usd,
            price_sol=current_price_sol,
            slippage_bps=slippage_bps,
            price_impact_pct=self._price_impact_fraction(quote) * 100.0,
            quote_json=self._safe_json(quote),
            route_plan_json=self._safe_json((quote.get("routePlan") or [])[:3]),
            provider="JUPITER",
            exit_reason=exit_reason,
            exit_reason_label=EXIT_REASON_LABELS.get(exit_reason, exit_reason),
        )

        wallet_pubkey = settings.WALLET_PUBLIC_KEY or "MOCK_WALLET"
        instructions = await self.jupiter.build_swap_instructions(quote, wallet_pubkey, extra={})
        bundle = await self.jito.send(instructions)

        sol_usd_val = _derive_sol_usd_price({}, latest) or 200.0
        priority_fee = instructions.get("prioritizationFeeLamports")

        idem_confirmed = f"SELL:{pos_id}:{exit_reason}:{bucket}:CONFIRMED"
        te_confirmed = await self.repo.append_trade_event(
            idem_confirmed,
            position_id=pos_id,
            token_mint=token_mint,
            strategy_id=self._position_strategy_id(position),
            side="SELL",
            event_type="SELL_CONFIRMED",
            status="CONFIRMED" if bundle.get("ok", True) else "FAILED",
            is_live=1,
            account_type="LIVE",
            requested_pct=pct,
            requested_token_amount=sell_amount_human,
            requested_sol_amount=out_sol,
            executed_token_amount=sell_amount_human if bundle.get("ok", True) else None,
            executed_sol_amount=out_sol if bundle.get("ok", True) else None,
            price_usd=current_price_usd,
            price_sol=current_price_sol,
            slippage_bps=slippage_bps,
            price_impact_pct=self._price_impact_fraction(quote) * 100.0,
            quote_json=self._safe_json(quote),
            route_plan_json=self._safe_json((quote.get("routePlan") or [])[:3]),
            jito_tip_lamports=bundle.get("jito_tip_lamports"),
            priority_fee_lamports=priority_fee if priority_fee is not None else bundle.get("priority_fee_lamports"),
            tx_signature=bundle.get("signature"),
            bundle_id=bundle.get("bundle_id"),
            error_code=bundle.get("error_code"),
            error_message=bundle.get("error_message"),
            provider="JITO",
            exit_reason=exit_reason,
            exit_reason_label=EXIT_REASON_LABELS.get(exit_reason, exit_reason),
            gross_value_usd=gross_value_usd,
            trade_value_usd_net=+abs(gross_value_usd),
            trade_value_usd_expected=+abs(gross_value_usd),
            accounting_source="jupiter_quote_expected",
            accounting_status="PENDING_RPC_BACKFILL",
            fee_detail_json=json.dumps({"accounting_mode": "LIVE_PENDING", "note": "Awaiting Solana tx meta backfill"}, ensure_ascii=False),
        )

        if not bundle.get("ok", True):
            await self.repo.append_system_event(
                "ERROR",
                "TRADE",
                "Sell execution failed",
                self._safe_json({"position_id": pos_id, "bundle": bundle}),
                account_type="LIVE",
            )
            return {"ok": False, "error": bundle.get("error_code") or "BUNDLE_FAILED", "trade_event_id": te_confirmed.get("id")}

        te_confirmed_id = te_confirmed.get("id")
        sig = bundle.get("signature")
        if sig and te_confirmed_id:
            try:
                backfill_result = await backfill_trade_event_from_solana_tx_meta(
                    repo=self.repo,
                    rpc=self.rpc,
                    trade_event_id=te_confirmed_id,
                    signature=sig,
                    wallet_pubkey=wallet_pubkey,
                    token_mint=token_mint,
                    side="SELL",
                    sol_usd=sol_usd_val,
                )
                if not backfill_result.get("ok"):
                    await self.repo.append_system_event(
                        "WARN", "TRADE",
                        "Backfill failed for live sell",
                        json.dumps({"te_id": te_confirmed_id, "sig": sig, "err": backfill_result.get("error")}),
                        account_type="LIVE",
                    )
            except Exception as e:
                await self.repo.append_system_event(
                    "WARN", "TRADE",
                    "Backfill error for live sell",
                    json.dumps({"te_id": te_confirmed_id, "error": str(e)}),
                    account_type="LIVE",
                )

        exit_audit = await build_exit_audit_payload(
            repo=self.repo,
            position=position,
            sell_trade_event=te_confirmed,
            exit_reason=exit_reason,
            exit_pct=pct,
            sell_amount_human=sell_amount_human,
            gross_value_usd=gross_value_usd,
            current_price_usd=current_price_usd,
            current_price_sol=current_price_sol,
            quote=quote,
            **(audit_context or {}),
        )
        await self.repo.insert_position_audit(
            position_id=pos_id,
            token_mint=token_mint,
            account_type=account_type,
            strategy_id=self._position_strategy_id(position),
            discovery_event_id=position.get("discovery_event_id"),
            snapshot_id=None,
            audit_type="EXIT",
            audit_json=exit_audit,
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        if pct >= 0.999999:
            await self.repo.close_position(
                pos_id,
                closed_at=now_iso,
                close_reason=exit_reason,
            )
        else:
            new_remaining = max(0.0, remaining_token - sell_amount_human)
            remaining_value_usd = new_remaining * current_price_usd if current_price_usd > 0 else 0.0
            await self.repo.update_position_remaining(
                pos_id,
                new_remaining,
                remaining_value_usd,
                last_fill_at=now_iso,
                last_fill_price_usd=current_price_usd,
            )

        await self.repo.append_system_event(
            "INFO",
            "TRADE",
            "Sell executed",
            self._safe_json({
                "position_id": pos_id,
                "exit_pct": pct,
                "exit_reason": exit_reason,
                "trade_event_id": te_confirmed.get("id"),
            }),
            account_type="LIVE",
        )
        return {"ok": True, "trade_event_id": te_confirmed.get("id"), "executed_sol_amount": out_sol}

    # ------------------------------------------------------------------
    # Quote validation
    # ------------------------------------------------------------------
    async def _get_quote(
        self,
        input_mint: str,
        output_mint: Any,
        amount_raw: Optional[int] = None,
        slippage_bps: Optional[int] = None,
        *legacy_args,
        token_mint: Optional[str] = None,
        strategy: Optional[Dict[str, Any]] = None,
        context_id: Optional[int] = None,
        is_sell: Optional[bool] = None,
    ) -> Dict[str, Any]:
        if is_sell is not None and isinstance(output_mint, (int, float)):
            legacy_token = input_mint
            legacy_amount = int(output_mint)
            legacy_slippage = int(amount_raw or slippage_bps or 0)
            input_mint = legacy_token if is_sell else WRAPPED_SOL_MINT
            output_mint = WRAPPED_SOL_MINT if is_sell else legacy_token
            amount_raw = legacy_amount
            slippage_bps = legacy_slippage

        amount_raw = int(amount_raw or 0)
        slippage_bps = int(slippage_bps or 0)
        if amount_raw <= 0:
            return {"error": "INVALID_QUOTE_AMOUNT", "amount_raw": amount_raw}

        try:
            quote = await self.jupiter.quote_exact_in(input_mint, output_mint, int(amount_raw), int(slippage_bps))
        except Exception as e:
            await self.repo.append_system_event(
                "ERROR",
                "TRADE",
                "Jupiter quote failed",
                self._safe_json({
                    "token": token_mint or output_mint,
                    "input_mint": input_mint,
                    "output_mint": output_mint,
                    "amount_raw": amount_raw,
                    "error": str(e),
                    "context_id": context_id,
                }),
                account_type="LIVE",
            )
            return {"error": "QUOTE_EXCEPTION", "message": str(e)}

        if not quote or quote.get("error"):
            return {"error": quote.get("error") if isinstance(quote, dict) else "NO_QUOTE", "quote": quote}

        price_impact_fraction = self._price_impact_fraction(quote)
        cap_fraction = self._price_impact_cap_fraction(strategy)
        if price_impact_fraction > cap_fraction:
            return {
                "error": "PRICE_IMPACT_HARD_CAP",
                "priceImpactPct": quote.get("priceImpactPct"),
                "price_impact_fraction": price_impact_fraction,
                "cap_fraction": cap_fraction,
                "quote": quote,
            }

        return quote
