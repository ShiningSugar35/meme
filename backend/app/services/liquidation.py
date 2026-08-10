from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from ..config import Settings, get_settings
from ..database import Database, utc_now_iso
from ..trading.live.errors import LiveTradeError
from ..trading.live.models import ExecutionResult, OrderStatus, SwapIntent, TradeSide
from .live_trading import LiveTradingService
from .paper_trading import PaperTradingService


class LiveExecutor(Protocol):
    async def execute(
        self,
        intent: SwapIntent,
        *,
        allow_authorized_exit: bool = False,
    ) -> ExecutionResult: ...


@dataclass(frozen=True, slots=True)
class LiquidationReport:
    job_id: str
    status: str
    total_positions: int
    closed_positions: int
    blocked_positions: int
    pending_positions: int
    remaining_positions: int
    finished_at: str | None = None


class LiquidationService:
    """Persistent, serial liquidation orchestrator.

    The runtime job freezes position ids before execution. Paper/shadow positions
    close only after a simulated SELL quote succeeds. Live positions close only
    after the durable live order journal reports CONFIRMED; pending or ambiguous
    submissions remain in CLOSING and are resumed with the same client_order_id.
    """

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        live_executor: LiveExecutor | None = None,
        paper_service: PaperTradingService | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.paper = paper_service or PaperTradingService(database, self.settings)
        self.live = live_executor or LiveTradingService(database, self.settings)

    async def process_active_job(self) -> LiquidationReport | None:
        job = self.database.get_runtime_state("liquidation_job")
        if not isinstance(job, dict) or job.get("status") not in {"queued", "running"}:
            return None

        job = self._ensure_job_snapshot(job)
        job["status"] = "running"
        job.setdefault("started_at", utc_now_iso())
        self.database.set_runtime_state("liquidation_job", job)

        results = job.setdefault("results", {})
        position_ids = [str(value) for value in job.get("position_ids") or []]
        for position_id in position_ids:
            previous = results.get(position_id) if isinstance(results, dict) else None
            if isinstance(previous, dict) and previous.get("state") == "closed":
                continue

            position = self.database.fetch_one("SELECT * FROM positions WHERE id=?", (position_id,))
            if not position:
                results[position_id] = self._result("blocked", "position_not_found")
                self._persist_progress(job)
                continue
            if position["status"] == "closed":
                results[position_id] = self._result("closed", "already_closed")
                self._persist_progress(job)
                continue
            if position["status"] in {"manual_intervention", "failed"}:
                results[position_id] = self._result("blocked", f"position_{position['status']}")
                self._persist_progress(job)
                continue

            account_kind = str(position["account_kind"])
            if account_kind == "live":
                outcome = await self._liquidate_live(job, position)
            else:
                outcome = self._liquidate_paper(position)
            results[position_id] = outcome
            self._persist_progress(job)

            # A live PENDING/UNKNOWN exit must be reconciled before another live
            # exit is broadcast. This keeps the emergency path serial and easy to
            # reason about across process restarts.
            if account_kind == "live" and outcome["state"] == "pending":
                break

        report = self._build_report(job)
        job.update(asdict(report))
        if report.finished_at is None:
            job.pop("finished_at", None)
        self.database.set_runtime_state("liquidation_job", job)
        if report.status in {"completed", "blocked"}:
            self.database.set_runtime_state("new_entries_paused", True)
            self.database.set_runtime_state(
                "new_entries_pause_reason",
                "liquidation_completed_manual_resume_required"
                if report.status == "completed"
                else "liquidation_blocked_manual_intervention",
            )
            self.database.audit(
                category="trading",
                action="liquidation_job_finished",
                severity="warning" if report.status == "blocked" else "info",
                entity_type="liquidation_job",
                entity_id=report.job_id,
                details=asdict(report),
            )
        return report

    def _ensure_job_snapshot(self, job: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(job)
        if not normalized.get("id"):
            normalized["id"] = f"liq_legacy_{normalized.get('requested_at') or 'unknown'}"
        if not normalized.get("position_ids"):
            rows = self.database.fetch_all(
                """
                SELECT id FROM positions
                WHERE status IN ('opening','open','closing')
                ORDER BY CASE account_kind WHEN 'live' THEN 0 ELSE 1 END, entry_time
                """
            )
            normalized["position_ids"] = [str(row["id"]) for row in rows]
        normalized["total_positions"] = len(normalized["position_ids"])
        if not isinstance(normalized.get("results"), dict):
            normalized["results"] = {}
        return normalized

    @staticmethod
    def _result(state: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {"state": state, "reason": reason, "updated_at": utc_now_iso(), **extra}

    def _persist_progress(self, job: dict[str, Any]) -> None:
        job["status"] = "running"
        job["updated_at"] = utc_now_iso()
        self.database.set_runtime_state("liquidation_job", job)

    def _liquidate_paper(self, position: dict[str, Any]) -> dict[str, Any]:
        result = self.paper.liquidate_position(str(position["id"]))
        return self._result("closed" if result.closed else "blocked", result.reason)

    async def _liquidate_live(
        self,
        job: dict[str, Any],
        position: dict[str, Any],
    ) -> dict[str, Any]:
        position_id = str(position["id"])
        if position["status"] == "opening":
            return self._result("blocked", "live_opening_requires_reconciliation")

        try:
            metadata = json.loads(position.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        input_amount_raw = metadata.get("token_amount_raw") or metadata.get("quantity_atomic")
        output_token = metadata.get("exit_output_token") or metadata.get("quote_token")
        if not self.settings.wallet_public_key:
            return self._result("blocked", "wallet_public_key_missing")
        if not input_amount_raw:
            return self._result("blocked", "live_token_amount_raw_missing")
        if not output_token:
            return self._result("blocked", "live_exit_output_token_missing")

        client_order_id = f"{job['id']}:{position_id}:sell"
        intent = SwapIntent(
            chain="sol",
            wallet_address=self.settings.wallet_public_key,
            input_token=str(position["token_address"]),
            output_token=str(output_token),
            input_amount_raw=str(input_amount_raw),
            side=TradeSide.SELL,
            client_order_id=client_order_id,
            metadata={
                "position_id": position_id,
                "account_kind": "live",
                "liquidation_job_id": str(job["id"]),
            },
        )
        self.database.execute(
            "UPDATE positions SET status='closing' WHERE id=? AND status IN ('open','closing')",
            (position_id,),
        )
        try:
            result = await self.live.execute(intent, allow_authorized_exit=True)
        except LiveTradeError as exc:
            if exc.submission_unknown or str(exc.code or "").upper() == "SUBMISSION_UNKNOWN":
                return self._result(
                    "pending",
                    "submission_unknown_reconciliation_required",
                    client_order_id=client_order_id,
                    error_code=exc.code,
                )
            self.database.execute(
                "UPDATE positions SET status='open' WHERE id=? AND status='closing'",
                (position_id,),
            )
            return self._result(
                "blocked",
                f"live_execution_{exc.kind.value}",
                client_order_id=client_order_id,
                error_code=exc.code,
            )

        if result.status is OrderStatus.CONFIRMED:
            self.database.execute(
                """
                UPDATE positions
                SET status='closed', exit_time=?, exit_reason='liquidate_all'
                WHERE id=? AND status='closing'
                """,
                (utc_now_iso(), position_id),
            )
            return self._result(
                "closed",
                "confirmed",
                client_order_id=client_order_id,
                provider_order_id=result.order_id,
                transaction_hash=result.tx_hash,
            )
        if result.status in {OrderStatus.PENDING, OrderStatus.PROCESSED, OrderStatus.UNKNOWN}:
            return self._result(
                "pending",
                result.status.value,
                client_order_id=client_order_id,
                provider_order_id=result.order_id,
            )

        self.database.execute(
            "UPDATE positions SET status='open' WHERE id=? AND status='closing'",
            (position_id,),
        )
        return self._result(
            "blocked",
            f"live_order_{result.status.value}",
            client_order_id=client_order_id,
            provider_order_id=result.order_id,
            error_code=result.error_code,
        )

    def _build_report(self, job: dict[str, Any]) -> LiquidationReport:
        position_ids = [str(value) for value in job.get("position_ids") or []]
        results = job.get("results") if isinstance(job.get("results"), dict) else {}
        states = [
            str((results.get(position_id) or {}).get("state") or "remaining")
            for position_id in position_ids
        ]
        closed = sum(state == "closed" for state in states)
        blocked = sum(state == "blocked" for state in states)
        pending = sum(state == "pending" for state in states)
        remaining = len(position_ids) - closed - blocked - pending
        if pending or remaining:
            status = "running"
            finished_at = None
        elif blocked:
            status = "blocked"
            finished_at = utc_now_iso()
        else:
            status = "completed"
            finished_at = utc_now_iso()
        return LiquidationReport(
            job_id=str(job["id"]),
            status=status,
            total_positions=len(position_ids),
            closed_positions=closed,
            blocked_positions=blocked,
            pending_positions=pending,
            remaining_positions=remaining,
            finished_at=finished_at,
        )


class LiquidationWorker:
    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        live_executor: LiveExecutor | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.service = LiquidationService(
            database, self.settings, live_executor=live_executor
        )
        self._stop = asyncio.Event()

    async def run_once(self) -> LiquidationReport | None:
        return await self.service.process_active_job()

    async def run_forever(self) -> None:
        self.database.set_runtime_state(
            "liquidation_worker_status",
            {"state": "running", "started_at": utc_now_iso()},
        )
        while not self._stop.is_set():
            try:
                report = await self.run_once()
                if report:
                    self.database.set_runtime_state(
                        "liquidation_worker_status",
                        {
                            "state": "degraded" if report.status == "blocked" else "running",
                            "last_job": asdict(report),
                            "updated_at": utc_now_iso(),
                        },
                    )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                self.database.set_runtime_state(
                    "liquidation_worker_status",
                    {"state": "degraded", "last_error": message, "updated_at": utc_now_iso()},
                )
                self.database.audit(
                    category="trading",
                    action="liquidation_worker_failed",
                    severity="error",
                    details={"error": message},
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=float(self.settings.liquidation_poll_seconds)
                )
            except TimeoutError:
                pass
        self.database.set_runtime_state(
            "liquidation_worker_status",
            {"state": "stopped", "stopped_at": utc_now_iso()},
        )

    def stop(self) -> None:
        self._stop.set()
