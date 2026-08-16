from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..collector.constants import LabelPolicy
from ..config import Settings, get_settings
from ..database import Database, utc_now_iso
from ..ml.economics import UNIT_USD, WIN_UNITS, theoretical_profit_units
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
    models: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class ModelHealthService:
    """Monitor all three active models using recent fixed-payoff OOS results."""

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
        active = self.models.active_models()
        if len(active) != 3:
            return self._store(ModelHealthReport(
                state="active_top3_not_ready",
                model_id=active[0]["id"] if active else None,
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
                reason=f"{len(active)} active models; need 3",
                evaluated_at=moment.isoformat(),
            ))

        cutoff = int((moment - timedelta(days=self.settings.model_monitor_window_days)).timestamp())
        reports = [self._evaluate_model(model, cutoff, int(moment.timestamp())) for model in active]
        rank1 = reports[0]
        enough = [item for item in reports if item["mature_predictions"] >= self.settings.model_monitor_min_predictions]
        degraded_models = [item for item in enough if item["degraded"]]
        state = "healthy" if enough and not degraded_models else "insufficient_data" if not enough else "degraded"
        reason = (
            "all evaluated active models remain within fixed-payoff decay tolerance"
            if state == "healthy"
            else "not enough mature OOS predictions for active models"
            if state == "insufficient_data"
            else "degraded active models: " + ", ".join(item["strategy_key"] for item in degraded_models)
        )
        # Health classification no longer starts an out-of-band retrain. The
        # scheduler owns all automatic cadence: insufficient_data -> daily 17:00,
        # every other state (including degraded) -> weekly 17:00. This keeps the
        # 16:00 entry freeze / 17:00 training / flat-then-activate lifecycle
        # deterministic for every automatic update.
        run_id = None
        report = ModelHealthReport(
            state=state,
            model_id=active[0]["id"],
            window_days=self.settings.model_monitor_window_days,
            mature_predictions=int(rank1["mature_predictions"]),
            selected_trades=int(rank1["selected_trades"]),
            precision=rank1["precision"],
            utility_eligible=True,
            recent_pnl_usd=rank1["fixed_profit_usd"],
            recent_selected_capital_usd=(float(rank1["selected_trades"]) * 50.0),
            recent_roi=(float(rank1["fixed_profit_usd"]) / (float(rank1["selected_trades"]) * 50.0)) if rank1["selected_trades"] else None,
            baseline_roi=rank1["baseline_capture"],
            degraded_ratio=self.settings.model_degraded_ratio,
            degraded=bool(degraded_models),
            reason=reason,
            training_run_id=run_id,
            evaluated_at=moment.isoformat(),
            models=tuple(reports),
        )
        return self._store(report)

    def _evaluate_model(self, model: dict[str, Any], cutoff: int, end: int) -> dict[str, Any]:
        slot = int(model.get("active_slot") or 0)
        strategy_key = f"model_{slot}"
        try:
            activated_at = datetime.fromisoformat(str(model.get("active_selected_at") or ""))
            if activated_at.tzinfo is None:
                activated_at = activated_at.replace(tzinfo=timezone.utc)
            activation_epoch = int(activated_at.timestamp())
        except (TypeError, ValueError):
            activation_epoch = 0
        scope_start = max(cutoff, activation_epoch)
        rows = self.database.fetch_all(
            """
            SELECT p.selected,s.tag,s.entry_time
            FROM predictions p JOIN samples s ON s.id=p.sample_id
            WHERE p.model_id=? AND p.strategy_key=?
              AND s.label_status='mature' AND s.tag IN (0,1)
              AND s.token_type IN ('new_creation','near_completion')
              AND s.label_version=?
              AND s.entry_time>=? AND s.entry_time<=?
            ORDER BY s.entry_time,p.id
            """,
            (model["id"], strategy_key, LabelPolicy().label_version, scope_start, end),
        )
        selected = [row for row in rows if int(row.get("selected") or 0) == 1]
        tp = sum(int(int(row.get("tag") or 0) == 1) for row in selected)
        fp = len(selected) - tp
        positive_count = sum(int(int(row.get("tag") or 0) == 1) for row in rows)
        units = theoretical_profit_units(tp, fp)
        capture = units / (WIN_UNITS * positive_count) if positive_count else 0.0
        precision = tp / len(selected) if selected else None
        baseline = (
            model.get("metrics", {}).get("final_recent_window", {}).get("economic_capture")
        )
        baseline_capture = float(baseline) if baseline is not None else None
        enough = len(rows) >= self.settings.model_monitor_min_predictions
        degraded = bool(
            enough
            and baseline_capture is not None
            and baseline_capture > 0
            and capture < baseline_capture * self.settings.model_degraded_ratio
        )
        return {
            "strategy_key": strategy_key,
            "model_id": model["id"],
            "algorithm": model["algorithm"],
            "mature_predictions": len(rows),
            "selected_trades": len(selected),
            "true_positives": tp,
            "false_positives": fp,
            "precision": precision,
            "profit_units": float(units),
            "fixed_profit_usd": float(units * UNIT_USD),
            "economic_capture": float(capture),
            "baseline_capture": baseline_capture,
            "degraded": degraded,
        }

    def _queue_degraded_training(
        self,
        rank1: dict[str, Any],
        moment: datetime,
    ) -> tuple[str | None, str]:
        readiness = self.training.automatic_training_readiness()
        if not readiness.ready:
            return None, f"automatic retraining disabled: {readiness.reason}"
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
            feature_names=self.training.configured_feature_selection(),
        )
        self.database.set_runtime_state("last_degraded_training_requested_at", moment.isoformat())
        self.database.audit(
            category="model",
            action="degraded_retraining_queued",
            severity="warning",
            entity_type="training_run",
            entity_id=run_id,
            details={"rank1_model_id": rank1["id"]},
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
        return await asyncio.to_thread(self.service.evaluate)

    async def run_forever(self) -> None:
        self.database.set_runtime_state("model_health_worker_status", {"state": "running", "started_at": utc_now_iso()})
        while not self._stop.is_set():
            try:
                report = await self.run_once()
                self.database.set_runtime_state(
                    "model_health_worker_status",
                    {"state": "running", "last_cycle_at": utc_now_iso(), "last_report": asdict(report)},
                )
            except Exception as exc:
                self.database.set_runtime_state(
                    "model_health_worker_status",
                    {"state": "degraded", "last_cycle_at": utc_now_iso(), "error": f"{type(exc).__name__}: {exc}"[:500]},
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=300.0)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
