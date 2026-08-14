from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from ..collector.enrichment import merge_sources
from ..collector.filters import first, normalize_token, to_float
from ..collector.models import Kline
from ..config import Settings, get_settings
from ..database import Database, utc_now_iso
from ..trading.simulator.jupiter_probe import JupiterReadOnlyQuoteProbe
from ..trading.simulator.quote import QuoteModelConfig
from ..trading.simulator.types import ExecutionQuote, FailureCategory
from .paper_trading import PaperTradingService


class KlineProvider(Protocol):
    async def klines(self, address: str, from_ts: int, to_ts: int) -> Sequence[Kline]: ...

    async def token_bundle(self, address: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PaperMonitorCycle:
    checked_positions: int
    market_requests: int
    closed_positions: int
    pending_positions: int
    blocked_positions: int
    open_positions: int
    skipped_liquidation_positions: int
    completed_at: str


class PaperPositionMonitor:
    """Restorable market-driven exit monitor for paper/shadow positions."""

    STRATEGY_KEYS = ("model_1", "model_2", "model_3", "rules_only")

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        paper_service: PaperTradingService | None = None,
        route_probe: JupiterReadOnlyQuoteProbe | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.paper = paper_service or PaperTradingService(database, self.settings)
        self.route_probe = route_probe or JupiterReadOnlyQuoteProbe(self.settings)

    async def run_cycle(
        self,
        provider: KlineProvider,
        *,
        now_ts: int | None = None,
    ) -> PaperMonitorCycle:
        current_ts = int(now_ts or time.time())
        if not self.settings.simulation_enabled or not self.settings.paper_market_monitor_enabled:
            report = PaperMonitorCycle(0, 0, 0, 0, 0, 0, 0, utc_now_iso())
            self.database.set_runtime_state("paper_monitor_status", {"state": "disabled", **asdict(report)})
            return report

        rows = self.database.fetch_all(
            """
            SELECT p.id,p.token_address,p.status,p.entry_time,p.expires_at,p.metadata_json,
                   p.token_amount,p.sample_id,s.raw_json
            FROM positions p
            LEFT JOIN samples s ON s.id=p.sample_id
            WHERE p.strategy_key IN ('model_1','model_2','model_3','rules_only')
              AND p.account_kind='simulation'
              AND p.status IN ('open','closing')
            ORDER BY p.entry_time,p.id
            """
        )
        liquidation = self.database.get_runtime_state("liquidation_job") or {}
        liquidation_ids = (
            {str(value) for value in liquidation.get("position_ids", [])}
            if liquidation.get("status") in {"queued", "running"}
            else set()
        )

        candidates = [row for row in rows if str(row["id"]) not in liquidation_ids]
        skipped = len(rows) - len(candidates)
        grouped: dict[str, list[dict]] = {}
        direct_retry: list[dict] = []
        for row in candidates:
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if row["status"] == "closing" and isinstance(metadata.get("paper_exit_pending"), dict):
                direct_retry.append(row)
            else:
                grouped.setdefault(str(row["token_address"]), []).append(row)

        closed = pending = blocked = open_count = 0
        checked = 0
        market_requests = 0

        for row in direct_retry:
            execution_quote = await self._prime_exit_route(row, now_ts=current_ts)
            result = self.paper.monitor_position(
                str(row["id"]), (), now_ts=current_ts, execution_quote=execution_quote
            )
            checked += 1
            closed += int(result.state == "closed")
            pending += int(result.state == "pending")
            blocked += int(result.state == "blocked")
            open_count += int(result.state == "open")

        for address, positions in grouped.items():
            from_ts = min(self._iso_epoch(str(row["entry_time"])) for row in positions)
            latest_needed = max(
                min(current_ts, self._iso_epoch(str(row["expires_at"])))
                for row in positions
            )
            to_ts = max(from_ts + 60, latest_needed)
            try:
                klines = await provider.klines(address, from_ts, to_ts)
                market_requests += 1
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:300]
                for row in positions:
                    blocked += 1
                    checked += 1
                self.database.audit(
                    category="simulation",
                    action="paper_market_data_failed",
                    severity="warning",
                    entity_type="token",
                    entity_id=address,
                    details={"positions": len(positions), "error": message},
                )
                continue

            try:
                # Exit execution must use current liquidity, not entry-time liquidity.
                await self._refresh_market_snapshot(provider, address, positions, klines)
            except Exception as exc:
                self.database.audit(
                    category="simulation",
                    action="paper_market_snapshot_failed",
                    severity="warning",
                    entity_type="token",
                    entity_id=address,
                    details={"positions": len(positions), "error": f"{type(exc).__name__}: {exc}"[:300]},
                )

            for row in positions:
                result = self.paper.monitor_position(
                    str(row["id"]), klines, now_ts=current_ts, defer_execution=True
                )
                if result.state == "pending" and result.reason == "execution_route_probe_required":
                    execution_quote = await self._prime_exit_route(row, now_ts=current_ts)
                    result = self.paper.monitor_position(
                        str(row["id"]), (), now_ts=current_ts, execution_quote=execution_quote
                    )
                checked += 1
                closed += int(result.state == "closed")
                pending += int(result.state == "pending")
                blocked += int(result.state == "blocked")
                open_count += int(result.state == "open")


        report = PaperMonitorCycle(
            checked_positions=checked,
            market_requests=market_requests,
            closed_positions=closed,
            pending_positions=pending,
            blocked_positions=blocked,
            open_positions=open_count,
            skipped_liquidation_positions=skipped,
            completed_at=utc_now_iso(),
        )
        self.database.set_runtime_state("paper_monitor_status", {"state": "running", **asdict(report)})
        return report

    async def _prime_exit_route(
        self,
        row: dict[str, Any],
        *,
        now_ts: int,
    ) -> ExecutionQuote | None:
        latest = self.database.fetch_one(
            """
            SELECT p.metadata_json,p.token_amount,p.token_address,s.raw_json
            FROM positions p LEFT JOIN samples s ON s.id=p.sample_id
            WHERE p.id=?
            """,
            (row["id"],),
        ) or {}
        try:
            metadata = json.loads(latest.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        pending_exit = metadata.get("paper_exit_pending")
        if not isinstance(pending_exit, dict):
            return None

        decimals = None
        snapshot = metadata.get("market_snapshot")
        if isinstance(snapshot, dict):
            decimals = snapshot.get("token_decimals")
        if decimals is None:
            try:
                raw = json.loads(latest.get("raw_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                raw = {}
            if isinstance(raw, dict):
                decimals = raw.get("decimals") or raw.get("decimal")
        try:
            decimals_int = int(decimals)
        except (TypeError, ValueError):
            # Never guess decimals: a wrong raw amount can manufacture a false no-route.
            return None
        decimals_int = min(18, max(0, decimals_int))
        quantity = float(latest.get("token_amount") or 0.0)
        amount_raw = max(0, int(quantity * (10**decimals_int)))
        slippage_bps = max(1, int(round(self.settings.trade_slippage_high * 10_000)))
        result = await self.route_probe.quote_sell(
            token_address=str(latest.get("token_address") or row.get("token_address") or ""),
            amount_raw=amount_raw,
            slippage_bps=slippage_bps,
        )
        route_fact = result.as_dict()
        route_fact["token_decimals"] = decimals_int
        route_fact["amount_raw"] = amount_raw
        route_fact["output_asset"] = "USDC"
        route_fact["output_decimals"] = 6
        route_fact["probed_at"] = utc_now_iso()
        pending_exit["route_probe"] = route_fact
        metadata["paper_exit_pending"] = pending_exit
        self.database.execute(
            "UPDATE positions SET metadata_json=? WHERE id=? AND status='closing'",
            (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), row["id"]),
        )

        if result.state == "no_route":
            self.database.audit(
                category="simulation",
                action="paper_read_only_route_probe",
                severity="error",
                entity_type="position",
                entity_id=str(row["id"]),
                details={"state": result.state, "source": result.source, "error_kind": result.error_kind},
            )
            return ExecutionQuote(
                success=False,
                fill_price=None,
                gross_usd=0.0,
                fee_usd=0.0,
                network_fee_sol=0.0,
                slippage_bps=0.0,
                latency_ms=0,
                failure_category=FailureCategory.NO_ROUTE,
                message=result.message or "Jupiter returned no sell route",
            )

        if result.state == "quoted" and result.out_amount_raw and quantity > 0:
            gross_usd = float(result.out_amount_raw) / 1_000_000.0
            if gross_usd > 0:
                # USDC uses six decimal places on Solana.
                fill_price = gross_usd / quantity
                reference_price = float(pending_exit.get("reference_price") or 0.0)
                slippage = (
                    max(0.0, (reference_price - fill_price) / reference_price * 10_000.0)
                    if reference_price > 0
                    else 0.0
                )
                return ExecutionQuote(
                    success=True,
                    fill_price=fill_price,
                    gross_usd=gross_usd,
                    fee_usd=gross_usd * QuoteModelConfig().platform_fee_rate,
                    network_fee_sol=QuoteModelConfig().network_fee_sol,
                    slippage_bps=slippage,
                    latency_ms=int(result.latency_ms or 0),
                    message="jupiter_read_only_quote",
                )

        if result.state == "unavailable":
            self.database.audit(
                category="simulation",
                action="paper_read_only_route_probe",
                severity="warning",
                entity_type="position",
                entity_id=str(row["id"]),
                details={"state": result.state, "source": result.source, "error_kind": result.error_kind},
            )
        # API/rate-limit/network failure is not evidence of a rug. Returning None
        # deliberately falls back to the local impact model using current liquidity.
        return None

    async def _refresh_market_snapshot(
        self,
        provider: KlineProvider,
        address: str,
        positions: list[dict],
        klines: Sequence[Kline],
    ) -> None:
        snapshot: dict[str, Any] = {"as_of": utc_now_iso()}
        completed = [item for item in klines if item.close is not None]
        if completed:
            latest = max(completed, key=lambda item: item.timestamp)
            snapshot["price"] = float(latest.close) if latest.close is not None else None

        token_bundle = getattr(provider, "token_bundle", None)
        if callable(token_bundle):
            try:
                bundle = await token_bundle(address)
                merged = merge_sources(bundle)
                normalized = normalize_token(merged, "")
                current_price = to_float(normalized.get("price"))
                if current_price is not None:
                    snapshot["price"] = current_price
                snapshot["liquidity_usd"] = normalized.get("liquidity")
                for risk_key in (
                    "rug_ratio",
                    "sell_tax",
                    "buy_tax",
                    "is_wash_trading",
                    "renounced_mint",
                    "renounced_freeze_account",
                ):
                    if normalized.get(risk_key) is not None:
                        snapshot[risk_key] = normalized.get(risk_key)
                decimals = to_float(first(merged, ("decimals", "decimal")))
                if decimals is not None and 0 <= decimals <= 18:
                    snapshot["token_decimals"] = int(decimals)

                market_cap = to_float(normalized.get("marketcap"))
                market_cap_source = "gmgn_direct" if market_cap is not None else None
                if market_cap is None and current_price is not None:
                    circulating_supply = to_float(first(merged, ("circulating_supply",)))
                    total_supply = to_float(first(merged, ("total_supply",)))
                    supply = circulating_supply if circulating_supply is not None else total_supply
                    if supply is not None and supply > 0:
                        market_cap = current_price * supply
                        market_cap_source = (
                            "gmgn_price_x_circulating_supply"
                            if circulating_supply is not None
                            else "gmgn_price_x_total_supply"
                        )
                snapshot["market_cap_usd"] = market_cap
                snapshot["market_cap_source"] = market_cap_source
            except Exception:
                # K-line monitoring remains authoritative for exits. A failed
                # optional token snapshot must never block or delay an exit.
                pass

        if len(snapshot) == 1:
            return
        for position in positions:
            latest = self.database.fetch_one(
                "SELECT metadata_json FROM positions WHERE id=?",
                (position["id"],),
            ) or {}
            try:
                metadata = json.loads(latest.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["market_snapshot"] = snapshot
            self.database.execute(
                "UPDATE positions SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position["id"]),
            )

    @staticmethod
    def _iso_epoch(value: str) -> int:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp())
