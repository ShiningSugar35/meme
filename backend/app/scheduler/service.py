from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..config import Settings, get_settings
from ..database import Database, utc_now_iso
from ..services.training import TrainingService


class TrainingScheduler:
    """Weekly Sunday 03:00 scheduler with durable catch-up and bounded retries."""

    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        self.database.set_runtime_state(
            "scheduler_status", {"state": "running", "started_at": utc_now_iso()}
        )
        await self._schedule_if_due(startup=True)
        while not self._stop.is_set():
            next_due = self.next_scheduled_at()
            self.database.set_runtime_state(
                "scheduler_status",
                {
                    "state": "running",
                    "next_training_at": next_due.astimezone(timezone.utc).isoformat(),
                },
            )
            seconds = max(
                1.0,
                min(300.0, (next_due - datetime.now(next_due.tzinfo)).total_seconds()),
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            except TimeoutError:
                await self._schedule_if_due(startup=False)
        self.database.set_runtime_state(
            "scheduler_status", {"state": "stopped", "stopped_at": utc_now_iso()}
        )

    def stop(self) -> None:
        self._stop.set()

    def next_scheduled_at(self, now: datetime | None = None) -> datetime:
        tz = ZoneInfo(self.settings.training_timezone)
        current = (now or datetime.now(tz)).astimezone(tz)
        days = (self.settings.training_weekday - current.weekday()) % 7
        candidate = (current + timedelta(days=days)).replace(
            hour=self.settings.training_hour,
            minute=self.settings.training_minute,
            second=0,
            microsecond=0,
        )
        if candidate <= current:
            candidate += timedelta(days=7)
        return candidate

    def most_recent_scheduled_at(self, now: datetime | None = None) -> datetime:
        tz = ZoneInfo(self.settings.training_timezone)
        current = (now or datetime.now(tz)).astimezone(tz)
        days_since = (current.weekday() - self.settings.training_weekday) % 7
        candidate = (current - timedelta(days=days_since)).replace(
            hour=self.settings.training_hour,
            minute=self.settings.training_minute,
            second=0,
            microsecond=0,
        )
        if candidate > current:
            candidate -= timedelta(days=7)
        return candidate

    def _scheduled_features(self) -> tuple[str, ...] | None:
        champion = TrainingService(self.database, self.settings).models.champion()
        if champion and champion.get("feature_names"):
            return tuple(str(name) for name in champion["feature_names"])
        return None

    async def _schedule_if_due(
        self,
        *,
        startup: bool,
        now: datetime | None = None,
    ) -> str | None:
        scheduled = self.most_recent_scheduled_at(now)
        scheduled_utc = scheduled.astimezone(timezone.utc)
        scheduled_key = scheduled_utc.isoformat()
        last_raw = self.database.get_runtime_state("last_training_completed_at")
        try:
            last = datetime.fromisoformat(last_raw).astimezone(timezone.utc) if last_raw else None
        except (TypeError, ValueError):
            last = None
        if last is not None and last >= scheduled_utc:
            return None

        active = self.database.fetch_one(
            "SELECT id FROM training_runs WHERE status IN ('queued','running') LIMIT 1"
        )
        existing = self.database.fetch_one(
            "SELECT * FROM training_runs WHERE scheduled_for=? LIMIT 1",
            (scheduled_key,),
        )
        if existing:
            status = str(existing["status"])
            if status == "completed":
                return str(existing["id"])
            if status in {"queued", "running"}:
                return str(existing["id"])
            if active and str(active["id"]) != str(existing["id"]):
                return None
            retry_count = int(existing.get("retry_count") or 0)
            if status in {"failed", "skipped"} and retry_count < self.settings.training_max_retries:
                self.database.execute(
                    """
                    UPDATE training_runs
                    SET status='queued', retry_count=retry_count+1, requested_at=?,
                        started_at=NULL, completed_at=NULL, error_message=NULL
                    WHERE id=? AND status IN ('failed','skipped')
                    """,
                    (utc_now_iso(), existing["id"]),
                )
                # The durable TrainingWorker will execute the re-queued row.
                return str(existing["id"])
            self.database.set_runtime_state(
                "scheduler_status",
                {
                    "state": "degraded",
                    "scheduled_for": scheduled_key,
                    "failed_run_id": existing["id"],
                    "retry_count": retry_count,
                    "retry_limit": self.settings.training_max_retries,
                    "reason": "scheduled training retry limit exhausted",
                    "updated_at": utc_now_iso(),
                },
            )
            return str(existing["id"])

        if active:
            return None

        service = TrainingService(self.database, self.settings)
        return service.create_run(
            "startup_catchup" if startup else "weekly",
            feature_names=self._scheduled_features(),
            scheduled_for=scheduled_key,
        )
