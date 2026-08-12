from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..config import Settings, get_settings
from ..database import Database, utc_now_iso
from .training import TrainingService


@dataclass(frozen=True, slots=True)
class TrainingRecoveryReport:
    recovered: int
    exhausted: int


class TrainingWorker:
    """Durable single-process executor for queued model training runs.

    Manual, weekly/catch-up and degraded retraining all create durable rows first.
    This worker is the only long-running executor in the application lifecycle.
    On process restart, an interrupted ``running`` row is either re-queued with a
    bounded retry count or marked failed when the retry budget is exhausted.
    """

    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.service = TrainingService(database, self.settings)
        self._stop = asyncio.Event()

    def recover_interrupted_runs(self) -> TrainingRecoveryReport:
        rows = self.database.fetch_all(
            "SELECT id,retry_count FROM training_runs WHERE status='running' ORDER BY requested_at,id"
        )
        recovered = exhausted = 0
        for row in rows:
            retry_count = int(row.get("retry_count") or 0)
            if retry_count < self.settings.training_max_retries:
                self.database.execute(
                    """
                    UPDATE training_runs
                    SET status='queued', retry_count=retry_count+1,
                        requested_at=?, started_at=NULL, completed_at=NULL,
                        error_message='interrupted by process restart; queued for retry'
                    WHERE id=? AND status='running'
                    """,
                    (utc_now_iso(), row["id"]),
                )
                recovered += 1
            else:
                self.database.execute(
                    """
                    UPDATE training_runs
                    SET status='failed', completed_at=?,
                        error_message='interrupted by process restart; retry limit exhausted'
                    WHERE id=? AND status='running'
                    """,
                    (utc_now_iso(), row["id"]),
                )
                exhausted += 1
        if recovered or exhausted:
            self.database.audit(
                category="model",
                action="training_runs_recovered",
                severity="warning" if exhausted else "info",
                details={"recovered": recovered, "exhausted": exhausted},
            )
        return TrainingRecoveryReport(recovered=recovered, exhausted=exhausted)

    async def run_once(self) -> str | None:
        row = self.database.fetch_one(
            """
            SELECT id FROM training_runs
            WHERE status='queued'
            ORDER BY requested_at,id
            LIMIT 1
            """
        )
        if row:
            run_id = str(row["id"])
            await asyncio.to_thread(self.service.run, run_id)
            await asyncio.to_thread(self.service.promote_pending_if_flat)
            return run_id
        # Candidate training may have finished while old positions were still open.
        # Keep polling activation separately so the new Top-3 switches on within
        # one worker interval of model_1/2/3 becoming fully flat.
        await asyncio.to_thread(self.service.promote_pending_if_flat)
        return None

    async def run_forever(
        self,
        *,
        recovery: TrainingRecoveryReport | None = None,
    ) -> None:
        recovery = recovery or self.recover_interrupted_runs()
        self.database.set_runtime_state(
            "training_worker_status",
            {
                "state": "running",
                "started_at": utc_now_iso(),
                "recovered_interrupted": recovery.recovered,
                "exhausted_interrupted": recovery.exhausted,
            },
        )
        while not self._stop.is_set():
            try:
                run_id = await self.run_once()
                self.database.set_runtime_state(
                    "training_worker_status",
                    {
                        "state": "running",
                        "last_run_id": run_id,
                        "last_poll_at": utc_now_iso(),
                    },
                )
                wait_seconds = 0.2 if run_id else float(self.settings.training_worker_poll_seconds)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                self.database.set_runtime_state(
                    "training_worker_status",
                    {"state": "degraded", "last_error": message, "last_poll_at": utc_now_iso()},
                )
                self.database.audit(
                    category="model",
                    action="training_worker_failed",
                    severity="error",
                    details={"error": message},
                )
                wait_seconds = float(self.settings.training_worker_poll_seconds)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_seconds)
            except TimeoutError:
                pass
        self.database.set_runtime_state(
            "training_worker_status", {"state": "stopped", "stopped_at": utc_now_iso()}
        )

    def stop(self) -> None:
        self._stop.set()
