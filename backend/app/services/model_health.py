from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings, get_settings
from ..database import Database, utc_now_iso
from ..ml.features import DEFAULT_MODEL_TRAINING_FEATURES
from ..repositories.models import ModelRepository
from .training import TrainingService


@dataclass(frozen=True, slots=True)
class ModelHealthReport:
    state: str
    model_id: str | None
    window_days: int
    mature_predictions: int
    selected_trades: int
    precision: float | None
    utility_eligible: bool
    recent_pnl_usd: float | None
    recent_selected_capital_usd: float | None
    recent_roi: float | None
    baseline_roi: float | None
    degraded_ratio: float
    degraded: bool
    reason: str
    training_run_id: str | None = None
    evaluated_at: str = ""


class ModelHealthService:
    """Evaluate recent OOS Champion performance and queue safe retraining."""

    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.models = ModelRepository(database)
        self.training = TrainingService(database, self.settings)

    def evaluate(
        self,
        *,
        now: datetime | None = None,
        queue_retraining: bool = True,
    ) -> ModelHealthReport:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        champion = self.models.champion()
        if not champion:
            return self._store(
                ModelHealthReport(
                    state="no_champion",
                    model_id=None,
                    window_days=self.settings.model_monitor_window_days,
                    mature_predictions=0,
                    selected_trades=0,
                    precision=None,
                    utility_eligible=False,
                    recent_pnl_usd=None,
                    recent_selected_capital_usd=None,
                    recent_roi=None,
                    baseline_roi=None,
                    degraded_ratio=self.settings.model_degraded_ratio,
                    degraded=False,
                    reason="no Champion model is active",
                    evaluated_at=moment.isoformat(),
                )
            )

        cutoff = int((moment - timedelta(days=self.settings.model_monitor_window_days)).timestamp())
        rows = self.database.fetch_all(
            """
            SELECT p.probability, p.threshold, p.selected,
                   s.tag, s.liquidity, s.final_close_ratio,
                   s.terminal_return_estimated, s.utility_eligible, s.entry_time
            FROM predictions p
            JOIN samples s ON s.id=p.sample_id
            WHERE p.model_id=? AND p.profile='balanced'
              AND s.label_status='mature' AND s.entry_time>=? AND s.entry_time<=?
            ORDER BY s.entry_time, p.id
            """,
            (champion["id"], cutoff, int(moment.timestamp())),
        )
        selected = [row for row in rows if int(row.get("selected") or 0) == 1]
        positives = sum(int(row.get("tag") in {1, 2}) for row in selected)
        precision = positives / len(selected) if selected else None

        base_kwargs = dict(
            model_id=champion["id"],
            window_days=self.settings.model_monitor_window_days,
            mature_predictions=len(rows),
            selected_trades=len(selected),
            precision=precision,
            degraded_ratio=self.settings.model_degraded_ratio,
            training_run_id=None,
            evaluated_at=moment.isoformat(),
        )
        if len(rows) < self.settings.model_monitor_min_predictions:
            return self._store(ModelHealthReport(
                state="insufficient_data",
                utility_eligible=False,
                recent_pnl_usd=None,
                recent_selected_capital_usd=None,
                recent_roi=None,
                baseline_roi=None,
                degraded=False,
                reason=(
                    f"only {len(rows)} mature OOS predictions; "
                    f"need {self.settings.model_monitor_min_predictions}"
                ),
                **base_kwargs,
            ))
        if len(selected) < self.settings.model_monitor_min_selected:
            return self._store(ModelHealthReport(
                state="insufficient_trades",
                utility_eligible=False,
                recent_pnl_usd=None,
                recent_selected_capital_usd=None,
                recent_roi=None,
                baseline_roi=None,
                degraded=False,
                reason=(
                    f"only {len(selected)} selected trades; "
                    f"need {self.settings.model_monitor_min_selected}"
                ),
                **base_kwargs,
            ))

        utility_eligible = all(
            int(row.get("utility_eligible") or 0) == 1
            and float(row.get("liquidity") or 0) > 0
            and not (
                row.get("tag") == 2
                and (
                    int(row.get("terminal_return_estimated") or 0) == 1
                    or row.get("final_close_ratio") is None
                )
            )
            for row in rows
        )
        if not utility_eligible:
            return self._store(ModelHealthReport(
                state="insufficient_real_utility",
                utility_eligible=False,
                recent_pnl_usd=None,
                recent_selected_capital_usd=None,
                recent_roi=None,
                baseline_roi=None,
                degraded=False,
                reason="recent OOS window mixes legacy/estimated economics; automatic degradation decisions are blocked",
                **base_kwargs,
            ))

        pnl = 0.0
        selected_capital = 0.0
        for row in selected:
            capital = min(0.01 * float(row["liquidity"]), 50.0)
            tag = int(row["tag"])
            realized_return = (
                0.60
                if tag == 1
                else -0.10
                if tag == 0
                else float(row["final_close_ratio"]) - 1.0
            )
            pnl += capital * realized_return
            selected_capital += capital
        recent_roi = pnl / selected_capital if selected_capital > 0 else None

        final_metrics = champion.get("metrics", {}).get("final_recent_window", {})
        baseline_pnl = final_metrics.get("cumulative_pnl_usd")
        baseline_capital = final_metrics.get("selected_capital")
        baseline_unit = final_metrics.get("utility_unit")
        if (
            baseline_unit != "usd"
            or baseline_pnl is None
            or baseline_capital in (None, 0)
        ):
            return self._store(ModelHealthReport(
                state="baseline_not_comparable",
                utility_eligible=True,
                recent_pnl_usd=pnl,
                recent_selected_capital_usd=selected_capital,
                recent_roi=recent_roi,
                baseline_roi=None,
                degraded=False,
                reason="Champion baseline is not a real-USD evaluation window; weekly training remains active",
                **base_kwargs,
            ))

        baseline_roi = float(baseline_pnl) / float(baseline_capital)
        precision_breach = precision is not None and precision < self.settings.min_precision
        utility_breach = (
            recent_roi is not None
            and baseline_roi > 0
            and recent_roi < baseline_roi * self.settings.model_degraded_ratio
        )
        degraded = bool(precision_breach or utility_breach)
        if not degraded:
            return self._store(ModelHealthReport(
                state="healthy",
                utility_eligible=True,
                recent_pnl_usd=pnl,
                recent_selected_capital_usd=selected_capital,
                recent_roi=recent_roi,
                baseline_roi=baseline_roi,
                degraded=False,
                reason="recent OOS precision and normalized utility remain within configured gates",
                **base_kwargs,
            ))

        reason_parts: list[str] = []
        if precision_breach:
            reason_parts.append(
                f"precision {precision:.4f} below {self.settings.min_precision:.4f}"
            )
        if utility_breach:
            reason_parts.append(
                f"recent ROI {recent_roi:.6f} below {self.settings.model_degraded_ratio:.2%} of baseline {baseline_roi:.6f}"
            )
        report = ModelHealthReport(
            state="degraded",
            utility_eligible=True,
            recent_pnl_usd=pnl,
            recent_selected_capital_usd=selected_capital,
            recent_roi=recent_roi,
            baseline_roi=baseline_roi,
            degraded=True,
            reason="; ".join(reason_parts),
            **base_kwargs,
        )
        if queue_retraining:
            run_id, queue_reason = self._queue_degraded_training(champion, moment)
            report = replace(
                report,
                state="degraded_retraining_queued" if run_id else "degraded_waiting",
                training_run_id=run_id,
                reason=f"{report.reason}; {queue_reason}",
            )
        return self._store(report)

    def _queue_degraded_training(
        self,
        champion: dict[str, Any],
        moment: datetime,
    ) -> tuple[str | None, str]:
        active = self.database.fetch_one(
            "SELECT id FROM training_runs WHERE status IN ('queued','running') LIMIT 1"
        )
        if active:
            return None, f"training run {active['id']} is already active"

        last_raw = self.database.get_runtime_state("last_degraded_training_requested_at")
        if last_raw:
            try:
                last = datetime.fromisoformat(str(last_raw)).astimezone(timezone.utc)
            except ValueError:
                last = None
            if last is not None and moment - last < timedelta(hours=self.settings.model_monitor_cooldown_hours):
                return None, "degraded retraining cooldown is active"

        run_id = self.training.create_run(
            "degraded",
            feature_names=tuple(
                champion.get("feature_names") or DEFAULT_MODEL_TRAINING_FEATURES
            ),
        )
        self.database.set_runtime_state("last_degraded_training_requested_at", moment.isoformat())
        self.database.audit(
            category="model",
            action="degraded_retraining_queued",
            severity="warning",
            entity_type="training_run",
            entity_id=run_id,
            details={"champion_id": champion["id"]},
        )
        return run_id, "degraded retraining queued"

    def _store(self, report: ModelHealthReport) -> ModelHealthReport:
        self.database.set_runtime_state("model_health_status", asdict(report))
        return report


class ModelHealthWorker:
    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.service = ModelHealthService(database, self.settings)
        self._stop = asyncio.Event()

    async def run_once(self) -> ModelHealthReport:
        # Degradation evaluation only enqueues a durable retraining request. The
        # TrainingWorker is the sole executor, preserving crash recovery and
        # serialization across manual/weekly/degraded training sources.
        return await asyncio.to_thread(self.service.evaluate)

    async def run_forever(self) -> None:
        self.database.set_runtime_state(
            "model_health_worker_status",
            {"state": "running", "started_at": utc_now_iso()},
        )
        while not self._stop.is_set():
            try:
                report = await self.run_once()
                self.database.set_runtime_state(
                    "model_health_worker_status",
                    {"state": "running", "last_run_at": utc_now_iso(), **asdict(report)},
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                self.database.set_runtime_state(
                    "model_health_worker_status",
                    {"state": "degraded", "last_run_at": utc_now_iso(), "error": message},
                )
                self.database.audit(
                    category="model",
                    action="health_monitor_failed",
                    severity="error",
                    details={"error": message},
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=float(self.settings.model_monitor_poll_seconds)
                )
            except TimeoutError:
                pass
        self.database.set_runtime_state(
            "model_health_worker_status",
            {"state": "stopped", "stopped_at": utc_now_iso()},
        )

    def stop(self) -> None:
        self._stop.set()
