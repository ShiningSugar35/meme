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


WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_TOKEN_DECIMALS = 9




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
    ):
        if not passed_strategies:
            return {"status": "NO_PASSED_STRATEGY", "token_mint": token_mint}

        if discovery_event_id is None:
            discovery_event_id, _ = await self.repo.create_discovery_event_idempotent(
                token_mint=token_mint,
                snapshot_id=snapshot_id,
            )

        # Capture TOP3 smart degen snapshot BEFORE creating positions
        # so it can be embedded in locked_strategy_config_json at creation time.
        top3_snapshot = None
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
            pos = await self._create_sim_position(
                token_mint, strategy, snapshot_id, discovery_event_id,
                top3_smart_degen_snapshot=top3_snapshot,
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
            res = await self._execute_buy(token_mint, live_strategies[0], snapshot_id, discovery_event_id, top3_smart_degen_snapshot=top3_snapshot)
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

        sol_side_liquidity = self._get_sol_side_liquidity(latest)
        liquidity_usd = self._get_liquidity_usd(latest)
        price_usd = self._get_price_usd(latest)
        price_sol = self._get_price_sol(latest)
        size_usd = await compute_entry_size_usd(liquidity_usd)
        size_sol = _usd_to_sol_amount(size_usd, latest, latest)
        if size_usd <= 0 or size_sol <= 0:
            await self.repo.append_system_event(
                "WARN",
                "TRADE",
                "SIM entry skipped: invalid liquidity/size",
                self._safe_json({"token": token_mint, "liquidity_usd": liquidity_usd, "size_usd": size_usd, "size_sol": size_sol}),
                account_type="SIM",
            )
            return None

        token_decimals = self._extract_token_decimals(token_mint, latest=latest, strategy=strategy)

        # Try Jupiter quote for better token-amount estimation
        token_amount = 0.0
        jupiter_price_impact = None
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
                token_decimals = self._extract_token_decimals(token_mint, quote=quote, latest=latest, strategy=strategy)
                out_raw = self._to_float(quote.get("outAmount"), 0.0) or 0.0
                token_amount = self._raw_to_human_amount(out_raw, token_decimals)
                jupiter_price_impact = self._price_impact_fraction(quote)
        except Exception:
            token_amount = (size_usd / price_usd) if price_usd and price_usd > 0 else (size_sol / price_sol if price_sol and price_sol > 0 else 0.0)

        if token_amount <= 0:
            token_amount = (size_usd / price_usd) if price_usd and price_usd > 0 else (size_sol / price_sol if price_sol and price_sol > 0 else 0.0)

        remaining_value_usd = token_amount * price_usd if token_amount > 0 and price_usd and price_usd > 0 else size_usd

        opened_at = datetime.now(timezone.utc).isoformat()
        idempotency_key = self._build_idempotency_key(
            "SIM_BUY",
            token_mint,
            strategy,
            snapshot_id,
            extra=f":d{discovery_event_id or 0}",
        )
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
        )

        pos_id = await self.repo.create_position(
            token_mint,
            False,
            self._locked_strategy_for_position(
                strategy,
                token_decimals=token_decimals,
                discovery_event_id=discovery_event_id,
                entry_size_usd=size_usd,
                top3_smart_degen_snapshot=top3_smart_degen_snapshot,
            ),
            "POSITION_OPEN",
            price_usd,
            price_sol,
            token_amount,
            token_amount,
            remaining_value_usd,
            opened_at,
            live_strategy_id=None,
            strategy_config_version=strategy.get("config_version", 1),
            open_trade_event_id=te.get("id"),
            last_fill_at=opened_at,
            last_fill_price_usd=price_usd,
            discovery_event_id=discovery_event_id,
            account_type="SIM",
            legacy_config_status="VALID",
        )

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

        return {"account_type": "SIM", "position_id": pos_id, "trade_event_id": te.get("id")}

    async def _execute_buy(
        self,
        token_mint: str,
        strategy: Dict[str, Any],
        snapshot_id: Optional[int] = None,
        discovery_event_id: Optional[int] = None,
        top3_smart_degen_snapshot: Optional[List[Dict[str, Any]]] = None,
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

        sol_side_liquidity = self._get_sol_side_liquidity(latest)
        liquidity_usd = self._get_liquidity_usd(latest)
        price_usd = self._get_price_usd(latest)
        price_sol = self._get_price_sol(latest)
        wallet_balance_usd = await self._get_wallet_balance_usd(latest, latest)
        size_usd = await compute_entry_size_usd(
            liquidity_usd,
            wallet_balance_usd=wallet_balance_usd,
            is_live=True,
        )
        size_sol = _usd_to_sol_amount(size_usd, latest, latest)
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
            jito_tip_lamports=bundle.get("tip_lamports"),
            priority_fee_lamports=bundle.get("priority_fee_lamports"),
            tx_signature=bundle.get("signature"),
            bundle_id=bundle.get("bundle_id"),
            error_code=bundle.get("error_code"),
            error_message=bundle.get("error_message"),
            provider="JITO",
        )

        if not bundle.get("ok", True):
            await self.repo.append_system_event(
                "ERROR",
                "TRADE",
                "Buy execution failed",
                self._safe_json({"token": token_mint, "bundle": bundle}),
                account_type="LIVE",
            )
            return {"ok": False, "error": bundle.get("error_code") or "BUNDLE_FAILED", "trade_event_id": te_confirmed.get("id")}

        opened_at = datetime.now(timezone.utc).isoformat()
        pos_id = await self.repo.create_position(
            token_mint,
            True,
            self._locked_strategy_for_position(
                strategy,
                token_decimals=token_decimals,
                discovery_event_id=discovery_event_id,
                entry_size_usd=size_usd,
                top3_smart_degen_snapshot=top3_smart_degen_snapshot,
            ),
            "POSITION_OPEN",
            price_usd,
            price_sol,
            token_amount,
            token_amount,
            remaining_value_usd,
            opened_at,
            live_strategy_id=sid,
            strategy_config_version=strategy.get("config_version", 1),
            open_trade_event_id=te_confirmed.get("id"),
            last_fill_at=opened_at,
            last_fill_price_usd=price_usd,
            discovery_event_id=discovery_event_id,
            account_type="LIVE",
            legacy_config_status="VALID",
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
        return {"ok": True, "account_type": "LIVE", "position_id": pos_id, "trade_event_id": te_confirmed.get("id")}

    # ------------------------------------------------------------------
    # Exit execution
    # ------------------------------------------------------------------
    async def execute_sell(
        self,
        position: Dict[str, Any],
        exit_pct: float = 1.0,
        exit_reason: str = "EXIT",
    ) -> Optional[Dict[str, Any]]:
        gate = self._safety_gate()
        account_type = position.get("account_type", "LIVE" if position.get("is_live") else "SIM")
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
        bucket = self._round_timestamp_bucket()
        idem_pending = f"SELL:{pos_id}:{exit_reason}:{bucket}:PENDING"
        te_pending = await self.repo.append_trade_event(
            idem_pending,
            position_id=pos_id,
            token_mint=token_mint,
            strategy_id=position.get("live_strategy_id"),
            side="SELL",
            event_type="SELL_PENDING",
            status="PENDING",
            is_live=1 if account_type == "LIVE" else 0,
            account_type=account_type,
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
        )

        wallet_pubkey = settings.WALLET_PUBLIC_KEY or "MOCK_WALLET"
        instructions = await self.jupiter.build_swap_instructions(quote, wallet_pubkey, extra={})
        bundle = await self.jito.send(instructions)

        idem_confirmed = f"SELL:{pos_id}:{exit_reason}:{bucket}:CONFIRMED"
        te_confirmed = await self.repo.append_trade_event(
            idem_confirmed,
            position_id=pos_id,
            token_mint=token_mint,
            strategy_id=position.get("live_strategy_id"),
            side="SELL",
            event_type="SELL_CONFIRMED",
            status="CONFIRMED" if bundle.get("ok", True) else "FAILED",
            is_live=1 if account_type == "LIVE" else 0,
            account_type=account_type,
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
            jito_tip_lamports=bundle.get("tip_lamports"),
            priority_fee_lamports=bundle.get("priority_fee_lamports"),
            tx_signature=bundle.get("signature"),
            bundle_id=bundle.get("bundle_id"),
            error_code=bundle.get("error_code"),
            error_message=bundle.get("error_message"),
            provider="JITO",
        )

        if not bundle.get("ok", True):
            await self.repo.append_system_event(
                "ERROR",
                "TRADE",
                "Sell execution failed",
                self._safe_json({"position_id": pos_id, "bundle": bundle}),
                account_type=account_type,
            )
            return {"ok": False, "error": bundle.get("error_code") or "BUNDLE_FAILED", "trade_event_id": te_confirmed.get("id")}

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
            account_type=account_type,
        )
        return {"ok": True, "trade_event_id": te_confirmed.get("id"), "executed_sol_amount": out_sol}

    # ------------------------------------------------------------------
    # Quote validation
    # ------------------------------------------------------------------
    async def _get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
        *,
        token_mint: Optional[str] = None,
        strategy: Optional[Dict[str, Any]] = None,
        context_id: Optional[int] = None,
    ) -> Dict[str, Any]:
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
