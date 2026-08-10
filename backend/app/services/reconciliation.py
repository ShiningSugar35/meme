from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

from ..config import Settings, get_settings
from ..database import Database, utc_now_iso
from ..trading.live.errors import LiveTradeError
from ..trading.live.models import ExecutionResult, FailureKind, OrderStatus
from ..trading.live.protocols import AtomicSwapProvider, PreparedSwapProvider
from .live_trading import build_live_provider
from .order_journal import SqliteOrderJournal


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    reconciled_at: str
    pending_checked: int
    confirmed: int
    failed: int
    expired: int
    still_pending: int
    pre_submit_aborted: int
    unresolved_without_order_id: int
    query_errors: int
    remaining_unknown: int


class StartupReconciliationService:
    """Fail-closed reconciler for trades left non-terminal across restarts.

    The reconciler never submits a replacement order. Rows with a stable
    provider order id are queried in place. Rows without an external id remain
    unresolved because a crash may have happened after provider acceptance but
    before the local journal persisted the id.
    """

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        provider: AtomicSwapProvider | PreparedSwapProvider | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.journal = SqliteOrderJournal(database)
        self.provider = provider

    def _provider(self) -> AtomicSwapProvider | PreparedSwapProvider | None:
        if self.provider is not None:
            return self.provider
        if self.settings.trading_provider != "gmgn_cli":
            return None
        return build_live_provider(self.settings, allow_live_execution=False)

    async def reconcile_all_pending(self) -> ReconciliationReport:
        rows = self.database.fetch_all(
            """
            SELECT * FROM trades
            WHERE journal_state IN (
                    'intent_created','quoting','submission_started','reserved',
                    'submitted','submission_unknown','pending'
                  )
               OR status IN ('created','quoting','submitted','pending','processed')
            ORDER BY created_at ASC
            """
        )
        provider = self._provider() if rows else self.provider
        confirmed = failed = expired = still_pending = 0
        pre_submit_aborted = unresolved_without_order_id = query_errors = 0

        for row in rows:
            status = str(row.get("status") or "")
            journal_state = str(row.get("journal_state") or "")
            client_order_id = str(row["client_order_id"])
            order_id = str(row.get("provider_order_id") or "").strip()

            if status in {"confirmed", "failed", "expired", "cancelled"}:
                self.database.execute(
                    "UPDATE trades SET journal_state='terminal', updated_at=? WHERE client_order_id=?",
                    (utc_now_iso(), client_order_id),
                )
                if status == "confirmed":
                    confirmed += 1
                elif status == "expired":
                    expired += 1
                else:
                    failed += 1
                continue

            if not order_id:
                if journal_state in {"intent_created", "quoting"}:
                    # New journal versions only use these states before the
                    # provider submit call begins, so restart can safely abort
                    # them without risking a duplicate external order.
                    self.database.execute(
                        """
                        UPDATE trades
                        SET journal_state='terminal', status='cancelled',
                            failure_category='reconciled',
                            failure_code='PRE_SUBMIT_ABORTED', updated_at=?
                        WHERE client_order_id=?
                        """,
                        (utc_now_iso(), client_order_id),
                    )
                    pre_submit_aborted += 1
                    continue

                self.database.execute(
                    """
                    UPDATE trades
                    SET journal_state='submission_unknown', status='pending',
                        failure_category='pending',
                        failure_code=CASE
                            WHEN journal_state='reserved' THEN 'STARTUP_RESERVED_AMBIGUOUS'
                            WHEN journal_state='submission_started' THEN 'SUBMISSION_STARTED_AMBIGUOUS'
                            ELSE COALESCE(failure_code, 'SUBMISSION_UNKNOWN')
                        END,
                        updated_at=?
                    WHERE client_order_id=?
                    """,
                    (utc_now_iso(), client_order_id),
                )
                unresolved_without_order_id += 1
                continue

            if provider is None:
                query_errors += 1
                continue

            try:
                snapshot = await provider.get_order(order_id)
            except Exception as exc:
                if isinstance(exc, LiveTradeError):
                    error = exc
                else:
                    error = LiveTradeError(
                        "provider reconciliation query failed",
                        kind=FailureKind.API,
                        code="RECONCILIATION_QUERY_FAILED",
                    )
                self.database.audit(
                    category="reconciliation",
                    action="provider_query_failed",
                    severity="warning",
                    entity_type="client_order",
                    entity_id=client_order_id,
                    details={"kind": error.kind.value, "code": error.code},
                )
                query_errors += 1
                continue

            result = ExecutionResult(
                client_order_id=client_order_id,
                order_id=snapshot.order_id,
                status=snapshot.status,
                attempts=max(1, int(row.get("attempt_count") or 0)),
                tx_hash=snapshot.tx_hash,
                failure_kind=snapshot.failure_kind,
                error_code=snapshot.error_code,
                report=snapshot.report,
            )
            await self.journal.mark_result(client_order_id, result)
            if snapshot.status is OrderStatus.CONFIRMED:
                confirmed += 1
            elif snapshot.status is OrderStatus.EXPIRED:
                expired += 1
            elif snapshot.status is OrderStatus.FAILED:
                failed += 1
            else:
                still_pending += 1

        remaining_unknown = unresolved_without_order_id + query_errors + still_pending
        report = ReconciliationReport(
            reconciled_at=utc_now_iso(),
            pending_checked=len(rows),
            confirmed=confirmed,
            failed=failed,
            expired=expired,
            still_pending=still_pending,
            pre_submit_aborted=pre_submit_aborted,
            unresolved_without_order_id=unresolved_without_order_id,
            query_errors=query_errors,
            remaining_unknown=remaining_unknown,
        )
        self.database.set_runtime_state("reconciliation_status", asdict(report))
        self._apply_entry_gate(report)
        self.database.audit(
            category="reconciliation",
            action="startup_reconciliation_completed",
            severity="warning" if remaining_unknown else "info",
            details=asdict(report),
        )
        return report

    def _apply_entry_gate(self, report: ReconciliationReport) -> None:
        if report.remaining_unknown:
            self.database.set_runtime_state("new_entries_paused", True)
            self.database.set_runtime_state(
                "new_entries_pause_reason", "startup_reconciliation_required"
            )
            return
        if self.database.get_runtime_state("new_entries_pause_reason") == "startup_reconciliation_required":
            self.database.set_runtime_state("new_entries_paused", False)
            self.database.set_runtime_state("new_entries_pause_reason", None)


class ReconciliationWorker:
    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        provider: AtomicSwapProvider | PreparedSwapProvider | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.service = StartupReconciliationService(
            database, self.settings, provider=provider
        )
        self._stop = asyncio.Event()

    async def run_once(self) -> ReconciliationReport:
        return await self.service.reconcile_all_pending()

    async def run_forever(self) -> None:
        self.database.set_runtime_state(
            "reconciliation_worker_status",
            {"state": "running", "started_at": utc_now_iso()},
        )
        while not self._stop.is_set():
            try:
                report = await self.run_once()
                self.database.set_runtime_state(
                    "reconciliation_worker_status",
                    {
                        "state": "degraded" if report.remaining_unknown else "running",
                        "last_run_at": utc_now_iso(),
                        **asdict(report),
                    },
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                self.database.set_runtime_state("new_entries_paused", True)
                self.database.set_runtime_state(
                    "new_entries_pause_reason", "startup_reconciliation_failed"
                )
                self.database.set_runtime_state(
                    "reconciliation_worker_status",
                    {
                        "state": "degraded",
                        "last_error": message,
                        "last_run_at": utc_now_iso(),
                    },
                )
                self.database.audit(
                    category="reconciliation",
                    action="worker_failed",
                    severity="error",
                    details={"error": message},
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=float(self.settings.reconciliation_poll_seconds),
                )
            except TimeoutError:
                pass
        self.database.set_runtime_state(
            "reconciliation_worker_status",
            {"state": "stopped", "stopped_at": utc_now_iso()},
        )

    def stop(self) -> None:
        self._stop.set()
