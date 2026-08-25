from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..collector.constants import FEATURE_SCHEMA_VERSION, LabelPolicy
from ..config import PROJECT_ROOT, Settings, get_settings
from ..database import Database, utc_now_iso
from ..ml.decision_policy import (
    DECISION_POLICY_VERSION,
    RISK_CEILING_CAUTION,
    RISK_CEILING_NORMAL,
    age_gate,
    clamp_adaptive_threshold,
)
from ..ml.economics import ECONOMIC_OBJECTIVE_VERSION
from ..ml.features import materialize_entry_feature
from ..ml.registry import ModelRegistry
from ..repositories.models import ModelRepository
from ..strategy import MODEL_STRATEGIES, RULES_ONLY, model_strategy
from .adaptive_policy import AdaptivePolicyService
from .drift import DriftGateService
from .modeling_gate import persist_modeling_readiness
from .paper_trading import PaperTradingService


@dataclass(frozen=True, slots=True)
class PredictionCycleResult:
    model_ids: tuple[str, ...] = ()
    samples_scored: int = 0
    predictions_written: int = 0
    signals_selected: int = 0
    model_positions_opened: int = 0
    rule_positions_opened: int = 0
    paper_positions_settled: int = 0
    stale_signals: int = 0
    blocked_signals: int = 0
    reason: str = "ok"


class PredictionService:
    """Score new admitted samples with all three active models plus rules-only baseline."""

    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.models = ModelRepository(database)
        self.paper = PaperTradingService(database, self.settings)
        self.adaptive = AdaptivePolicyService(database, self.settings)
        self.drift = DriftGateService(database)

    def run_cycle(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> PredictionCycleResult:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        readiness = persist_modeling_readiness(self.database, self.settings)
        active = self.models.active_models()
        active_contract_current = len(active) == 3 and all(
            str((item.get("parameters") or {}).get("label_version") or "") == LabelPolicy().label_version
            and str((item.get("parameters") or {}).get("economic_objective_version") or "") == ECONOMIC_OBJECTIVE_VERSION
            and str((item.get("parameters") or {}).get("decision_policy_version") or "") == DECISION_POLICY_VERSION
            for item in active
        )
        if not readiness.ready or not active_contract_current:
            rule_opened, rule_stale, rule_blocked = self._reconcile_rule_only(
                moment=moment,
                limit=limit,
                ignore_model_rollover_gate=True,
            )
            settled = 0 if self.settings.paper_market_monitor_enabled else self.paper.settle_mature_positions()
            if not readiness.ready:
                blocked_reason = readiness.reason
            elif len(active) != 3:
                blocked_reason = "active_top3_not_ready"
            else:
                blocked_reason = "active_top3_phase16_contract_stale"
            result = PredictionCycleResult(
                model_ids=tuple(item.get("id") for item in active) if active_contract_current else (),
                rule_positions_opened=rule_opened,
                paper_positions_settled=settled,
                stale_signals=rule_stale,
                blocked_signals=rule_blocked,
                reason=blocked_reason,
            )
            self.database.set_runtime_state("prediction_worker_last_cycle", asdict(result))
            return result

        predictions_written = selected = scored = 0
        active_ids: list[str] = []
        adaptive_decision = self.adaptive.decision(now_ts=int(moment.timestamp()))
        for slot, model in enumerate(active, start=1):
            bundle = self._load_bundle(model)
            active_ids.append(str(model["id"]))
            try:
                activated_at = datetime.fromisoformat(str(model.get("active_selected_at") or ""))
                if activated_at.tzinfo is None:
                    activated_at = activated_at.replace(tzinfo=timezone.utc)
                activation_epoch = int(activated_at.timestamp())
            except ValueError:
                activation_epoch = 0
            entry_cutoff = max(int(model.get("training_window_end") or 0), activation_epoch)
            rows = self.database.fetch_all(
                """
                SELECT s.*
                FROM samples s
                WHERE s.entry_time >= ?
                  AND s.feature_schema_version=?
                  AND s.token_type IN ('new_creation','near_completion')
                  AND NOT EXISTS(
                      SELECT 1 FROM predictions p
                      WHERE p.sample_id=s.id AND p.model_id=?
                  )
                ORDER BY s.entry_time,s.id
                LIMIT ?
                """,
                (entry_cutoff, FEATURE_SCHEMA_VERSION, model["id"], limit),
            )
            strategy = model_strategy(slot)
            if not rows:
                continue
            frame = pd.concat(
                [self._prediction_frame(row, bundle.feature_names) for row in rows],
                ignore_index=True,
            )
            if (
                bundle.calibrator is None
                or bundle.sparse_budget is None
                or bundle.decision_policy_version != DECISION_POLICY_VERSION
                or bundle.label_version != LabelPolicy().label_version
                or bundle.economic_objective_version != ECONOMIC_OBJECTIVE_VERSION
            ):
                raise RuntimeError("active model artifact does not satisfy the Phase16 decision contract")
            raw_probabilities = np.clip(bundle.predict_raw_probabilities(frame), 0.0, 1.0)
            probabilities = np.clip(bundle.predict_probabilities(frame), 0.0, 1.0)
            if len(probabilities) != len(rows) or len(raw_probabilities) != len(rows):
                raise RuntimeError("model returned a prediction count that does not match the batch")
            drift = self.drift.evaluate(model_id=str(model["id"]), reference=bundle.drift_reference)
            risk_model = bundle.execution_risk_model
            for row, raw_probability, calibrated_probability in zip(
                rows, raw_probabilities, probabilities, strict=True
            ):
                probability = float(calibrated_probability)
                policy_base_threshold = max(float(bundle.threshold), 0.25)
                age = age_gate(row.get("age_minutes"))
                hard_threshold = max(policy_base_threshold, float(age.probability_floor))
                adaptive_threshold = self.adaptive.effective_threshold(
                    policy_base_threshold, adaptive_decision
                )
                # Adaptive EXPANSIVE may never undercut the sparse-budget/global/
                # age hard floor. DEFENSIVE may raise it further.
                threshold = clamp_adaptive_threshold(
                    policy_base_threshold, float(age.probability_floor), adaptive_threshold
                )

                risk_probability: float | None = None
                if risk_model is not None:
                    try:
                        risk_probability = float(risk_model.predict_probability(row))
                    except Exception:
                        risk_probability = None
                risk_ceiling = (
                    RISK_CEILING_CAUTION if drift.state == "caution" else RISK_CEILING_NORMAL
                )
                hard_gate_ok = True
                if not age.allowed:
                    hard_gate_ok = False
                    decision_reason = age.reason
                elif risk_probability is None:
                    hard_gate_ok = False
                    decision_reason = "execution_risk_unavailable"
                elif risk_probability > risk_ceiling:
                    hard_gate_ok = False
                    decision_reason = "execution_risk_above_ceiling"
                elif drift.state == "severe":
                    hard_gate_ok = False
                    decision_reason = "drift_severe_model_freeze"
                elif probability < threshold:
                    decision_reason = "calibrated_probability_below_threshold"
                else:
                    decision_reason = "selected"
                neutral_chosen = bool(hard_gate_ok and probability >= hard_threshold)
                chosen = bool(hard_gate_ok and probability >= threshold)
                if chosen:
                    decision_reason = "selected"
                with self.database.transaction(immediate=True) as connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO predictions(
                            sample_id,model_id,probability,strategy_key,threshold,selected,
                            base_threshold,neutral_selected,adaptive_action,adaptive_delta_logit,
                            regime_snapshot_id,action_propensity,policy_version,
                            raw_probability,decision_policy_version,decision_reason,
                            execution_risk_probability,drift_state,policy_base_threshold,
                            age_probability_floor,predicted_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(sample_id,model_id,strategy_key) DO NOTHING
                        """,
                        (
                            row["id"], model["id"], probability, strategy, threshold,
                            int(chosen), hard_threshold, int(neutral_chosen), adaptive_decision.action,
                            adaptive_decision.delta_logit, adaptive_decision.regime_snapshot_id,
                            adaptive_decision.propensity, adaptive_decision.policy_version,
                            float(raw_probability), DECISION_POLICY_VERSION, decision_reason,
                            risk_probability, drift.state, policy_base_threshold,
                            float(age.probability_floor), moment.isoformat(),
                        ),
                    )
                    if cursor.rowcount == 1:
                        predictions_written += 1
                        selected += int(chosen)
                        scored += 1
            self.database.set_runtime_state(
                f"model_drift:{model['id']}", drift.as_dict()
            )

        opened, stale, blocked = self._reconcile_model_signals(moment=moment)
        rule_opened, rule_stale, rule_blocked = self._reconcile_rule_only(moment=moment, limit=limit)
        self.adaptive.settle_feedback()
        settled = 0 if self.settings.paper_market_monitor_enabled else self.paper.settle_mature_positions()
        result = PredictionCycleResult(
            model_ids=tuple(active_ids),
            samples_scored=scored,
            predictions_written=predictions_written,
            signals_selected=selected,
            model_positions_opened=opened,
            rule_positions_opened=rule_opened,
            paper_positions_settled=settled,
            stale_signals=stale + rule_stale,
            blocked_signals=blocked + rule_blocked,
        )
        self.database.set_runtime_state("prediction_worker_last_cycle", asdict(result))
        return result

    def _load_bundle(self, model: dict[str, Any]) -> Any:
        artifact = Path(model["artifact_path"])
        artifact = artifact if artifact.is_absolute() else PROJECT_ROOT / artifact
        if not artifact.exists():
            raise FileNotFoundError(f"active model artifact is missing: {artifact.name}")
        return ModelRegistry(artifact.parent).load(artifact.stem)

    @staticmethod
    def _prediction_frame(row: dict[str, Any], feature_names: tuple[str, ...]) -> pd.DataFrame:
        try:
            source = json.loads(row.get("features_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            source = {}
        record = {
            name: materialize_entry_feature(
                name,
                source,
                entry_price=row.get("entry_price"),
            )
            for name in feature_names
        }
        return pd.DataFrame.from_records([record], columns=list(feature_names))

    def _reconcile_model_signals(
        self,
        *,
        moment: datetime,
        ignore_model_rollover_gate: bool = False,
    ) -> tuple[int, int, int]:
        if not self.settings.simulation_enabled:
            return 0, 0, 0
        rows = self.database.fetch_all(
            """
            SELECT p.id AS prediction_id,p.sample_id,p.strategy_key,p.model_id,s.entry_time
            FROM predictions p
            JOIN samples s ON s.id=p.sample_id
            JOIN active_model_slots a ON a.model_id=p.model_id
            WHERE p.selected=1
              AND p.decision_policy_version=?
              AND p.decision_reason='selected'
              AND p.strategy_key IN ('model_1','model_2','model_3')
              AND s.feature_schema_version=?
              AND s.token_type IN ('new_creation','near_completion')
              AND NOT EXISTS(
                  SELECT 1 FROM positions pos
                  WHERE pos.prediction_id=p.id AND pos.strategy_key=p.strategy_key
              )
            ORDER BY s.entry_time,p.id
            """,
            (DECISION_POLICY_VERSION, FEATURE_SCHEMA_VERSION),
        )
        opened = stale = blocked = 0
        now_epoch = int(moment.timestamp())
        rollover_paused = (not ignore_model_rollover_gate) and bool(
            self.database.get_runtime_state("model_entries_paused_for_rollover", False)
        )
        for row in rows:
            if now_epoch - int(row["entry_time"]) > self.settings.signal_max_age_seconds:
                stale += 1
                continue
            if rollover_paused:
                blocked += 1
                continue
            result = self.paper.open_from_prediction(
                sample_id=int(row["sample_id"]),
                prediction_id=int(row["prediction_id"]),
                model_id=str(row["model_id"]),
                strategy_key=str(row["strategy_key"]),
            )
            if result.opened:
                opened += 1
            elif result.reason != "already_opened":
                blocked += 1
        return opened, stale, blocked

    def _reconcile_rule_only(
        self,
        *,
        moment: datetime,
        limit: int,
        ignore_model_rollover_gate: bool = False,
    ) -> tuple[int, int, int]:
        if not self.settings.simulation_enabled:
            return 0, 0, 0
        session = self.paper.ensure_simulation_session()
        try:
            session_start = int(datetime.fromisoformat(str(session["started_at"])).timestamp())
        except (TypeError, ValueError):
            session_start = 0
        now_epoch = int(moment.timestamp())
        admission_cutoff = max(
            session_start - int(self.settings.signal_max_age_seconds),
            now_epoch - int(self.settings.signal_max_age_seconds),
        )
        rows = self.database.fetch_all(
            """
            SELECT s.id,s.entry_time
            FROM samples s
            WHERE s.entry_time>=?
              AND s.feature_schema_version=?
              AND s.token_type IN ('new_creation','near_completion')
              AND NOT EXISTS(
                  SELECT 1 FROM positions pos
                  WHERE pos.sample_id=s.id AND pos.simulation_session_id=?
                    AND pos.strategy_key='rules_only'
              )
            ORDER BY s.entry_time,s.id
            LIMIT ?
            """,
            (admission_cutoff, FEATURE_SCHEMA_VERSION, session["id"], limit),
        )
        opened = stale = blocked = 0
        rollover_paused = (not ignore_model_rollover_gate) and bool(
            self.database.get_runtime_state("model_entries_paused_for_rollover", False)
        )
        for row in rows:
            if now_epoch - int(row["entry_time"]) > self.settings.signal_max_age_seconds:
                stale += 1
                continue
            if rollover_paused:
                blocked += 1
                continue
            result = self.paper.open_rule_only(sample_id=int(row["id"]))
            if result.opened:
                opened += 1
            elif result.reason != "already_opened":
                blocked += 1
        return opened, stale, blocked


class PredictionWorker:
    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.service = PredictionService(database, self.settings)
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        self.database.set_runtime_state(
            "prediction_worker_status", {"state": "running", "started_at": utc_now_iso()}
        )
        while not self._stop.is_set():
            try:
                result = await asyncio.to_thread(self.service.run_cycle)
                self.database.set_runtime_state(
                    "prediction_worker_status",
                    {"state": "running", "last_cycle_at": utc_now_iso(), **asdict(result)},
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                self.database.set_runtime_state(
                    "prediction_worker_status",
                    {"state": "degraded", "last_cycle_at": utc_now_iso(), "error": message},
                )
                self.database.audit(
                    category="prediction",
                    action="cycle_failed",
                    severity="error",
                    details={"error": message},
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=float(self.settings.signal_poll_seconds)
                )
            except TimeoutError:
                pass
        self.database.set_runtime_state(
            "prediction_worker_status", {"state": "stopped", "stopped_at": utc_now_iso()}
        )

    def stop(self) -> None:
        self._stop.set()
