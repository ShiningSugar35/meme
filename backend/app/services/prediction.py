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
    AGE_POLICY_CANDIDATES,
    DECISION_POLICY_VERSION,
    age_adjusted_threshold,
    age_gate,
    clamp_adaptive_threshold,
    deployment_certification_is_current,
    deployment_model_is_qualified,
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
    tradable_model_ids: tuple[str, ...] = ()
    shadow_model_ids: tuple[str, ...] = ()
    samples_scored: int = 0
    shadow_samples_scored: int = 0
    shadow_predictions_written: int = 0
    shadow_signals_selected: int = 0
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
            and (item.get("parameters") or {}).get("age_policy_version") in AGE_POLICY_CANDIDATES
            and (item.get("parameters") or {}).get("deployment_fit_scope") == "final_train_only_certified_instance"
            and deployment_certification_is_current(
                self._deployment_certification(item)
            )
            for item in active
        )
        if not readiness.ready or not active_contract_current:
            shadow_ids, shadow_scored, shadow_written, shadow_selected = (
                self._score_pending_generation_shadow(moment=moment, limit=limit)
                if readiness.ready
                else ((), 0, 0, 0)
            )
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
                blocked_reason = "active_top3_contract_stale"
            result = PredictionCycleResult(
                model_ids=tuple(item.get("id") for item in active) if active_contract_current else (),
                shadow_model_ids=shadow_ids,
                shadow_samples_scored=shadow_scored,
                shadow_predictions_written=shadow_written,
                shadow_signals_selected=shadow_selected,
                rule_positions_opened=rule_opened,
                paper_positions_settled=settled,
                stale_signals=rule_stale,
                blocked_signals=rule_blocked,
                reason=blocked_reason,
            )
            self.database.set_runtime_state("prediction_worker_last_cycle", asdict(result))
            return result

        predictions_written = selected = scored = 0
        shadow_written = shadow_selected = shadow_scored = 0
        active_ids: list[str] = []
        tradable_ids: list[str] = []
        shadow_ids: list[str] = []
        adaptive_decision = self.adaptive.decision(now_ts=int(moment.timestamp()))
        for slot, model in enumerate(active, start=1):
            bundle = self._load_bundle(model)
            active_ids.append(str(model["id"]))
            deployment_qualified = deployment_model_is_qualified(
                self._deployment_certification(model)
            )
            shadow_mode = not deployment_qualified
            if deployment_qualified:
                tradable_ids.append(str(model["id"]))
            else:
                shadow_ids.append(str(model["id"]))
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
            strategy = f"shadow_model_{slot}" if shadow_mode else model_strategy(slot)
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
                or bundle.age_policy_version not in AGE_POLICY_CANDIDATES
            ):
                raise RuntimeError("active model artifact does not satisfy the current decision contract")
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
                policy_base_threshold = float(bundle.threshold)
                age = age_gate(row.get("age_minutes"), bundle.age_policy_version)
                age_threshold = age_adjusted_threshold(policy_base_threshold, age)
                hard_threshold = age_threshold
                adaptive_threshold = self.adaptive.effective_threshold(
                    policy_base_threshold, adaptive_decision
                )
                # Adaptive EXPANSIVE may never undercut the model development threshold
                # hard threshold. DEFENSIVE may raise it further.
                threshold = clamp_adaptive_threshold(
                    policy_base_threshold, age_threshold, adaptive_threshold
                )

                risk_probability: float | None = None
                if risk_model is not None:
                    try:
                        risk_probability = float(risk_model.predict_probability(row))
                    except Exception:
                        risk_probability = None
                hard_gate_ok = True
                if not age.allowed:
                    hard_gate_ok = False
                    decision_reason = age.reason
                elif probability < threshold:
                    decision_reason = "calibrated_probability_below_threshold"
                else:
                    decision_reason = "selected"
                neutral_chosen = bool(hard_gate_ok and probability >= hard_threshold)
                chosen = bool(hard_gate_ok and probability >= threshold)
                if chosen:
                    decision_reason = "selected"
                if shadow_mode:
                    decision_reason = f"shadow_{decision_reason}"
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
                            age_threshold, moment.isoformat(),
                        ),
                    )
                    if cursor.rowcount == 1:
                        if shadow_mode:
                            shadow_written += 1
                            shadow_selected += int(chosen)
                            shadow_scored += 1
                        else:
                            predictions_written += 1
                            selected += int(chosen)
                            scored += 1
            self.database.set_runtime_state(
                f"model_drift:{model['id']}", drift.as_dict()
            )

        opened, stale, blocked = self._reconcile_model_signals(moment=moment)
        rule_opened, rule_stale, rule_blocked = self._reconcile_rule_only(moment=moment, limit=limit)
        self.adaptive.settle_feedback()
        if shadow_ids:
            self._maybe_refresh_shadow_health(
                model_ids=tuple(shadow_ids),
                moment=moment,
                force=bool(shadow_written),
            )
        settled = 0 if self.settings.paper_market_monitor_enabled else self.paper.settle_mature_positions()
        result = PredictionCycleResult(
            model_ids=tuple(active_ids),
            tradable_model_ids=tuple(tradable_ids),
            shadow_model_ids=tuple(shadow_ids),
            samples_scored=scored,
            shadow_samples_scored=shadow_scored,
            shadow_predictions_written=shadow_written,
            shadow_signals_selected=shadow_selected,
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

    @staticmethod
    def _deployment_certification(model: dict[str, Any]) -> dict[str, Any]:
        """Read the per-model certificate from durable registered metadata."""
        for source_name in ("metrics", "parameters", "active_metrics"):
            source = model.get(source_name)
            if not isinstance(source, dict):
                continue
            certification = source.get("deployment_certification")
            if isinstance(certification, dict):
                return certification
        return {}

    def _score_pending_generation_shadow(
        self,
        *,
        moment: datetime,
        limit: int,
    ) -> tuple[tuple[str, ...], int, int, int]:
        """Score the newest current-contract pending Top-3 without opening positions."""
        pending = self.database.fetch_one(
            """
            SELECT * FROM training_runs
            WHERE status='completed' AND promoted=0
            ORDER BY completed_at DESC, requested_at DESC
            LIMIT 1
            """
        )
        if not pending:
            return (), 0, 0, 0
        try:
            summary = json.loads(pending.get("summary_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return (), 0, 0, 0
        certification = summary.get("deployment_certification") or {}
        proposed = summary.get("top_models") or []
        if (
            summary.get("decision_policy_version") != DECISION_POLICY_VERSION
            or not deployment_certification_is_current(certification)
            or not isinstance(proposed, list)
            or len(proposed) != 3
        ):
            return (), 0, 0, 0
        try:
            completed_at = datetime.fromisoformat(str(pending.get("completed_at") or ""))
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)
            entry_cutoff = int(completed_at.timestamp())
        except (TypeError, ValueError):
            return (), 0, 0, 0

        candidates: list[tuple[int, dict[str, Any], Any]] = []
        for slot, proposed_model in enumerate(proposed, start=1):
            if not isinstance(proposed_model, dict):
                return (), 0, 0, 0
            model_id = str(proposed_model.get("id") or "")
            model = self.models.get(model_id)
            if model is None:
                return (), 0, 0, 0
            parameters = model.get("parameters") or {}
            if (
                parameters.get("label_version") != LabelPolicy().label_version
                or parameters.get("economic_objective_version") != ECONOMIC_OBJECTIVE_VERSION
                or parameters.get("decision_policy_version") != DECISION_POLICY_VERSION
                or parameters.get("age_policy_version") not in AGE_POLICY_CANDIDATES
                or parameters.get("deployment_fit_scope") != "final_train_only_certified_instance"
                or not deployment_certification_is_current(self._deployment_certification(model))
            ):
                return (), 0, 0, 0
            bundle = self._load_bundle(model)
            if (
                bundle.calibrator is None
                or bundle.sparse_budget is None
                or bundle.decision_policy_version != DECISION_POLICY_VERSION
                or bundle.label_version != LabelPolicy().label_version
                or bundle.economic_objective_version != ECONOMIC_OBJECTIVE_VERSION
                or bundle.age_policy_version not in AGE_POLICY_CANDIDATES
            ):
                return (), 0, 0, 0
            candidates.append((slot, model, bundle))

        model_ids = tuple(str(model["id"]) for _, model, _ in candidates)
        scored = written = selected = 0
        for slot, model, bundle in candidates:
            strategy = f"shadow_model_{slot}"
            rows = self.database.fetch_all(
                """
                SELECT s.*
                FROM samples s
                WHERE s.entry_time >= ?
                  AND s.feature_schema_version=?
                  AND s.token_type IN ('new_creation','near_completion')
                  AND NOT EXISTS(
                      SELECT 1 FROM predictions p
                      WHERE p.sample_id=s.id AND p.model_id=? AND p.strategy_key=?
                  )
                ORDER BY s.entry_time,s.id
                LIMIT ?
                """,
                (entry_cutoff, FEATURE_SCHEMA_VERSION, model["id"], strategy, limit),
            )
            if not rows:
                continue
            frame = pd.concat(
                [self._prediction_frame(row, bundle.feature_names) for row in rows],
                ignore_index=True,
            )
            raw_probabilities = np.clip(bundle.predict_raw_probabilities(frame), 0.0, 1.0)
            probabilities = np.clip(bundle.predict_probabilities(frame), 0.0, 1.0)
            drift = self.drift.evaluate(model_id=str(model["id"]), reference=bundle.drift_reference)
            risk_model = bundle.execution_risk_model
            for row, raw_probability, calibrated_probability in zip(
                rows, raw_probabilities, probabilities, strict=True
            ):
                probability = float(calibrated_probability)
                policy_base_threshold = float(bundle.threshold)
                age = age_gate(row.get("age_minutes"), bundle.age_policy_version)
                age_threshold = age_adjusted_threshold(policy_base_threshold, age)
                risk_probability: float | None = None
                if risk_model is not None:
                    try:
                        risk_probability = float(risk_model.predict_probability(row))
                    except Exception:
                        risk_probability = None
                if not age.allowed:
                    chosen = False
                    decision_reason = f"shadow_{age.reason}"
                elif probability < age_threshold:
                    chosen = False
                    decision_reason = "shadow_calibrated_probability_below_threshold"
                else:
                    chosen = True
                    decision_reason = "shadow_selected"
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
                            row["id"], model["id"], probability, strategy, age_threshold,
                            int(chosen), age_threshold, int(chosen), "SHADOW", 0.0,
                            None, 1.0, "shadow_observation_v1", float(raw_probability),
                            DECISION_POLICY_VERSION, decision_reason, risk_probability,
                            drift.state, policy_base_threshold, age_threshold, moment.isoformat(),
                        ),
                    )
                    if cursor.rowcount == 1:
                        written += 1
                        scored += 1
                        selected += int(chosen)
            self.database.set_runtime_state(
                f"model_drift:{model['id']}", drift.as_dict()
            )
        shadow_state = {
            "training_run_id": str(pending["id"]),
            "model_ids": list(model_ids),
            "samples_scored": scored,
            "predictions_written": written,
            "signals_selected": selected,
            "entry_cutoff": entry_cutoff,
            "updated_at": moment.isoformat(),
        }
        self.database.set_runtime_state("prediction_shadow_last_cycle", shadow_state)
        self._maybe_refresh_shadow_health(
            model_ids=model_ids,
            moment=moment,
            force=bool(written),
        )
        return model_ids, scored, written, selected

    def _maybe_refresh_shadow_health(
        self,
        *,
        model_ids: tuple[str, ...],
        moment: datetime,
        force: bool = False,
    ) -> None:
        if not force:
            previous = self.database.get_runtime_state("shadow_model_health") or {}
            evaluated_at = previous.get("evaluated_at") if isinstance(previous, dict) else None
            try:
                previous_at = datetime.fromisoformat(str(evaluated_at))
                if previous_at.tzinfo is None:
                    previous_at = previous_at.replace(tzinfo=timezone.utc)
                if (moment - previous_at).total_seconds() < self.settings.model_monitor_poll_seconds:
                    return
            except (TypeError, ValueError):
                pass
        self._refresh_shadow_health(model_ids=model_ids, moment=moment)

    def _refresh_shadow_health(
        self,
        *,
        model_ids: tuple[str, ...],
        moment: datetime,
    ) -> None:
        """Persist mature OOS shadow economics and risk-ceiling sensitivity for audit only."""
        if not model_ids:
            self.database.set_runtime_state(
                "shadow_model_health",
                {"state": "inactive", "evaluated_at": moment.isoformat(), "models": []},
            )
            return
        placeholders = ",".join("?" for _ in model_ids)
        rows = self.database.fetch_all(
            f"""
            SELECT p.model_id,p.strategy_key,p.probability,p.selected,p.decision_reason,
                   p.execution_risk_probability,p.drift_state,p.age_probability_floor,
                   s.id AS sample_id,s.tag,s.age_minutes,
                   (
                       SELECT q.net_pnl_usd
                       FROM positions q
                       WHERE q.sample_id=p.sample_id
                         AND q.account_kind='simulation'
                         AND q.strategy_key='rules_only'
                         AND q.status='closed'
                       ORDER BY q.entry_time DESC LIMIT 1
                   ) AS rules_only_net_pnl_usd
            FROM predictions p
            JOIN samples s ON s.id=p.sample_id
            WHERE p.model_id IN ({placeholders})
              AND p.strategy_key IN ('shadow_model_1','shadow_model_2','shadow_model_3')
              AND p.decision_policy_version=?
              AND s.label_status='mature' AND s.tag IN (0,1)
              AND s.feature_schema_version=? AND s.label_version=?
            ORDER BY s.entry_time,p.id
            """,
            (*model_ids, DECISION_POLICY_VERSION, FEATURE_SCHEMA_VERSION, LabelPolicy().label_version),
        )
        reports: list[dict[str, Any]] = []
        for model_id in model_ids:
            model_rows = [row for row in rows if str(row.get("model_id")) == model_id]
            selected_rows = [row for row in model_rows if int(row.get("selected") or 0) == 1]
            tp = sum(int(row.get("tag") or 0) == 1 for row in selected_rows)
            fp = len(selected_rows) - tp
            selected_pnl = [
                float(row["rules_only_net_pnl_usd"])
                for row in selected_rows
                if row.get("rules_only_net_pnl_usd") is not None
            ]
            risk_sensitivity: dict[str, dict[str, Any]] = {}
            for ceiling in (0.35, 0.40, 0.45):
                would_select: list[dict[str, Any]] = []
                for row in model_rows:
                    reason = str(row.get("decision_reason") or "")
                    if reason.startswith("shadow_age_"):
                        continue
                    try:
                        probability = float(row.get("probability"))
                        age_floor = float(row.get("age_probability_floor"))
                        risk_probability = float(row.get("execution_risk_probability"))
                    except (TypeError, ValueError):
                        continue
                    if (
                        not np.isfinite([probability, age_floor, risk_probability]).all()
                        or probability < age_floor
                        or risk_probability > ceiling
                    ):
                        continue
                    would_select.append(row)
                sensitivity_tp = sum(int(row.get("tag") or 0) == 1 for row in would_select)
                sensitivity_fp = len(would_select) - sensitivity_tp
                pnl_values = [
                    float(row["rules_only_net_pnl_usd"])
                    for row in would_select
                    if row.get("rules_only_net_pnl_usd") is not None
                ]
                risk_sensitivity[str(ceiling)] = {
                    "selected": len(would_select),
                    "true_positives": sensitivity_tp,
                    "false_positives": sensitivity_fp,
                    "profit_units": float(3 * sensitivity_tp - sensitivity_fp),
                    "rules_only_closed": len(pnl_values),
                    "rules_only_net_pnl_usd": float(sum(pnl_values)),
                }
            model = self.models.get(model_id) or {}
            reports.append(
                {
                    "model_id": model_id,
                    "algorithm": model.get("algorithm"),
                    "age_policy_version": (model.get("parameters") or {}).get("age_policy_version"),
                    "mature_predictions": len(model_rows),
                    "selected": len(selected_rows),
                    "true_positives": tp,
                    "false_positives": fp,
                    "precision": (tp / len(selected_rows)) if selected_rows else None,
                    "profit_units": float(3 * tp - fp),
                    "rules_only_closed": len(selected_pnl),
                    "rules_only_net_pnl_usd": float(sum(selected_pnl)),
                    "risk_ceiling_sensitivity": risk_sensitivity,
                }
            )
        self.database.set_runtime_state(
            "shadow_model_health",
            {
                "state": "observing",
                "decision_policy_version": DECISION_POLICY_VERSION,
                "evaluated_at": moment.isoformat(),
                "models": reports,
            },
        )

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
                  WHERE pos.sample_id=s.id AND pos.account_kind='simulation'
                    AND pos.strategy_key='rules_only'
              )
            ORDER BY s.entry_time,s.id
            LIMIT ?
            """,
            (admission_cutoff, FEATURE_SCHEMA_VERSION, limit),
        )
        opened = stale = blocked = 0
        for row in rows:
            if now_epoch - int(row["entry_time"]) > self.settings.signal_max_age_seconds:
                stale += 1
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
