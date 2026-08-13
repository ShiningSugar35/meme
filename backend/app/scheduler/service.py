from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..config import Settings, get_settings
from ..database import Database, utc_now_iso
from ..services.training import TrainingService


class TrainingScheduler:
    """Health-aware 17:00 BJT training scheduler with staged model rollover.

    Normal cadence is weekly on ``training_weekday``. While model health is
    ``insufficient_data`` the cadence accelerates to every day. On every due
    cycle model strategies stop opening new positions at 16:00, training is
    queued at 17:00, and the resulting Top-3 is activated only after all three
    model strategy ledgers are flat. Missed scheduled runs are durably caught up
    on process restart.
    """

    ENTRY_FREEZE_LEAD = timedelta(hours=1)

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
            now = datetime.now(ZoneInfo(self.settings.training_timezone))
            self.refresh_entry_gate(now=now)
            next_due = self.next_scheduled_at(now)
            next_freeze = next_due - self.ENTRY_FREEZE_LEAD
            self._store_status(next_due=next_due, next_freeze=next_freeze)
            boundary = next_freeze if next_freeze > now else next_due
            seconds_to_boundary = max(1.0, (boundary - now).total_seconds())
            # Poll at most once per minute so health-state cadence changes and a
            # newly activated candidate set are reflected promptly, while still
            # waking exactly for the 16:00/17:00 boundaries when they are nearer.
            seconds = min(60.0, seconds_to_boundary)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            except TimeoutError:
                await self._schedule_if_due(startup=False)
        self.database.set_runtime_state(
            "scheduler_status", {"state": "stopped", "stopped_at": utc_now_iso()}
        )

    def stop(self) -> None:
        self._stop.set()

    def schedule_mode(self) -> str:
        health = self.database.get_runtime_state("model_health_status", {})
        state = str(health.get("state") or "") if isinstance(health, dict) else ""
        return "daily" if state == "insufficient_data" else "weekly"

    def next_scheduled_at(self, now: datetime | None = None) -> datetime:
        tz = ZoneInfo(self.settings.training_timezone)
        current = (now or datetime.now(tz)).astimezone(tz)
        if self.schedule_mode() == "daily":
            candidate = current.replace(
                hour=self.settings.training_hour,
                minute=self.settings.training_minute,
                second=0,
                microsecond=0,
            )
            if candidate <= current:
                candidate += timedelta(days=1)
            return candidate
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
        if self.schedule_mode() == "daily":
            candidate = current.replace(
                hour=self.settings.training_hour,
                minute=self.settings.training_minute,
                second=0,
                microsecond=0,
            )
            if candidate > current:
                candidate -= timedelta(days=1)
            return candidate
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

    def refresh_entry_gate(self, *, now: datetime | None = None) -> bool:
        """Persist and return whether model_1/2/3 entries must be paused."""
        tz = ZoneInfo(self.settings.training_timezone)
        current = (now or datetime.now(tz)).astimezone(tz)
        rollover = self.database.get_runtime_state("model_rollover_status", {})
        if isinstance(rollover, dict) and rollover.get("state") == "waiting_for_flat":
            target_raw = self.database.get_runtime_state("model_entry_rollover_gate", {})
            target = target_raw.get("scheduled_for") if isinstance(target_raw, dict) else None
            self._set_entry_gate(
                paused=True,
                scheduled_for=str(target or ""),
                mode=self.schedule_mode(),
                reason="candidate_models_waiting_for_all_model_positions_to_close",
            )
            return True

        existing = self.database.get_runtime_state("model_entry_rollover_gate", {})
        if isinstance(existing, dict) and existing.get("paused") and existing.get("scheduled_for"):
            try:
                committed = datetime.fromisoformat(str(existing["scheduled_for"]))
                if committed.tzinfo is None:
                    committed = committed.replace(tzinfo=timezone.utc)
                committed = committed.astimezone(tz)
            except ValueError:
                committed = None
            if committed is not None and not self._activation_satisfies(committed):
                self._set_entry_gate(
                    paused=True,
                    scheduled_for=committed.astimezone(timezone.utc).isoformat(),
                    mode=str(existing.get("schedule_mode") or self.schedule_mode()),
                    reason=str(existing.get("reason") or "scheduled_model_rollover_in_progress"),
                )
                return True

        next_due = self.next_scheduled_at(current)
        recent_due = self.most_recent_scheduled_at(current)
        target = (
            next_due
            if timedelta(0) <= (next_due - current) <= self.ENTRY_FREEZE_LEAD
            else recent_due
        )
        freeze_at = target - self.ENTRY_FREEZE_LEAD
        paused = current >= freeze_at and not self._activation_satisfies(target)
        self._set_entry_gate(
            paused=paused,
            scheduled_for=target.astimezone(timezone.utc).isoformat() if paused else "",
            mode=self.schedule_mode(),
            reason=(
                "scheduled_model_rollover_window"
                if paused
                else "outside_model_rollover_window"
            ),
        )
        return paused

    def _activation_satisfies(self, scheduled: datetime) -> bool:
        values: list[str] = []
        last = self.database.get_runtime_state("last_model_activation_at")
        if isinstance(last, str) and last:
            values.append(last)
        active = self.database.fetch_one("SELECT MAX(selected_at) AS selected_at FROM active_model_slots")
        if active and active.get("selected_at"):
            values.append(str(active["selected_at"]))
        scheduled_utc = scheduled.astimezone(timezone.utc)
        for raw in values:
            try:
                value = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            if value.astimezone(timezone.utc) >= scheduled_utc:
                return True
        return False

    def _set_entry_gate(
        self,
        *,
        paused: bool,
        scheduled_for: str,
        mode: str,
        reason: str,
    ) -> None:
        payload = {
            "paused": bool(paused),
            "scheduled_for": scheduled_for or None,
            "schedule_mode": mode,
            "reason": reason,
            "updated_at": utc_now_iso(),
        }
        self.database.set_runtime_state("model_entries_paused_for_rollover", bool(paused))
        self.database.set_runtime_state("model_entry_rollover_gate", payload)

    def _store_status(self, *, next_due: datetime, next_freeze: datetime) -> None:
        gate = self.database.get_runtime_state("model_entry_rollover_gate", {})
        rollover = self.database.get_runtime_state("model_rollover_status", {})
        self.database.set_runtime_state(
            "scheduler_status",
            {
                "state": "running",
                "schedule_mode": self.schedule_mode(),
                "next_training_at": next_due.astimezone(timezone.utc).isoformat(),
                "next_entry_freeze_at": next_freeze.astimezone(timezone.utc).isoformat(),
                "model_entries_paused": bool(gate.get("paused")) if isinstance(gate, dict) else False,
                "pending_activation_run_id": (
                    rollover.get("run_id")
                    if isinstance(rollover, dict) and rollover.get("state") == "waiting_for_flat"
                    else None
                ),
            },
        )

    def _scheduled_features(self) -> tuple[str, ...]:
        return TrainingService(self.database, self.settings).configured_feature_selection()

    async def _schedule_if_due(
        self,
        *,
        startup: bool,
        now: datetime | None = None,
    ) -> str | None:
        self.refresh_entry_gate(now=now)
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
            if status in {"completed", "queued", "running"}:
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
                return str(existing["id"])
            self.database.set_runtime_state(
                "scheduler_status",
                {
                    "state": "degraded",
                    "schedule_mode": self.schedule_mode(),
                    "scheduled_for": scheduled_key,
                    "failed_run_id": existing["id"],
                    "retry_count": retry_count,
                    "retry_limit": self.settings.training_max_retries,
                    "reason": "scheduled training retry limit exhausted; model entries remain paused",
                    "updated_at": utc_now_iso(),
                },
            )
            return str(existing["id"])

        if active:
            return None

        mode = self.schedule_mode()
        trigger = "startup_catchup" if startup else ("daily" if mode == "daily" else "weekly")
        service = TrainingService(self.database, self.settings)
        return service.create_run(
            trigger,
            feature_names=self._scheduled_features(),
            scheduled_for=scheduled_key,
        )
