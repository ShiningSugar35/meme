from __future__ import annotations

import hashlib
import json
import traceback
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from ..config import PROJECT_ROOT, Settings, get_settings
from ..database import Database, utc_now_iso
from ..ml.features import (
    AVAILABLE_MODEL_FEATURES,
    DEFAULT_MODEL_TRAINING_FEATURES,
    FeatureBuilder,
    FeaturePolicy,
)
from ..ml.registry import ModelRegistry
from ..ml.trainer import ModelTrainer, TrainerConfig
from ..repositories.models import ModelRepository
from ..repositories.samples import SampleRepository


TrainingTrigger = Literal["manual", "weekly", "daily", "startup_catchup", "degraded"]


class TrainingService:
    """Persistent Top-3 model training and atomic active-set installation."""

    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.samples = SampleRepository(database)
        self.models = ModelRepository(database)
        self.registry = ModelRegistry(self.settings.model_directory)

    def create_run(
        self,
        trigger: TrainingTrigger = "manual",
        *,
        feature_names: list[str] | tuple[str, ...] | None = None,
        scheduled_for: str | None = None,
    ) -> str:
        selected_features = self.normalize_feature_selection(feature_names)
        request_payload = {"feature_names": list(selected_features)}
        request_json = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"))
        run_id = str(uuid.uuid4())
        with self.database.transaction(immediate=True) as connection:
            if scheduled_for is not None:
                existing = connection.execute(
                    "SELECT id FROM training_runs WHERE scheduled_for=? LIMIT 1",
                    (scheduled_for,),
                ).fetchone()
                if existing:
                    return str(existing["id"])
            connection.execute(
                """
                INSERT INTO training_runs(
                    id, trigger, status, requested_at, request_json, scheduled_for, retry_count
                ) VALUES(?, ?, 'queued', ?, ?, ?, 0)
                """,
                (run_id, trigger, utc_now_iso(), request_json, scheduled_for),
            )
        self.database.audit(
            category="model",
            action="training_queued",
            entity_type="training_run",
            entity_id=run_id,
            details={
                "trigger": trigger,
                "feature_names": list(selected_features),
                "scheduled_for": scheduled_for,
            },
        )
        return run_id

    @staticmethod
    def normalize_feature_selection(
        feature_names: list[str] | tuple[str, ...] | None,
    ) -> tuple[str, ...]:
        if feature_names is None:
            return DEFAULT_MODEL_TRAINING_FEATURES
        requested = {str(name).strip() for name in feature_names if str(name).strip()}
        if not requested:
            raise ValueError("at least one model feature must be selected")
        unknown = sorted(requested.difference(AVAILABLE_MODEL_FEATURES))
        if unknown:
            raise ValueError(f"unsupported model features: {unknown}")
        return tuple(name for name in AVAILABLE_MODEL_FEATURES if name in requested)

    def run(self, run_id: str) -> None:
        row = self.database.fetch_one("SELECT * FROM training_runs WHERE id=?", (run_id,))
        if not row:
            raise ValueError("training run not found")
        with self.database.transaction(immediate=True) as connection:
            running = connection.execute(
                "SELECT id FROM training_runs WHERE status='running' AND id<>? LIMIT 1",
                (run_id,),
            ).fetchone()
            if running:
                return
            cursor = connection.execute(
                """
                UPDATE training_runs
                SET status='running', started_at=?, completed_at=NULL, error_message=NULL
                WHERE id=? AND status='queued'
                """,
                (utc_now_iso(), run_id),
            )
            if cursor.rowcount != 1:
                return

        active_before = self.models.active_models()
        rank1_before = active_before[0] if active_before else self.models.champion()
        try:
            try:
                request_payload = json.loads(row.get("request_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                request_payload = {}
            selected_features = self.normalize_feature_selection(request_payload.get("feature_names"))
            rows = self.samples.list_mature()
            frame, data_hash = self._training_frame(rows)
            dataset = FeatureBuilder(
                FeaturePolicy(feature_allowlist=selected_features)
            ).prepare(frame)
            result = ModelTrainer(TrainerConfig()).train(dataset)

            registered: list[dict[str, Any]] = []
            candidate_map = result.candidates_by_algorithm
            for rank, (bundle, evaluation_bundle) in enumerate(
                zip(result.bundles, result.evaluation_bundles, strict=True), start=1
            ):
                production_path = self.registry.save(bundle)
                evaluation_path = self.registry.save(evaluation_bundle)
                candidate = candidate_map[bundle.algorithm]
                metrics = {
                    **dict(bundle.metrics),
                    "precision": candidate.final_metrics.precision if candidate.final_metrics else None,
                    "recall": candidate.final_metrics.recall if candidate.final_metrics else None,
                    "trade_count": candidate.final_metrics.trade_count if candidate.final_metrics else 0,
                    "fixed_profit_usd": candidate.final_metrics.fixed_profit_usd if candidate.final_metrics else None,
                    "profit_units": candidate.final_metrics.profit_units if candidate.final_metrics else None,
                    "economic_score": candidate.economic_score,
                    "generalization_score": candidate.generalization.score if candidate.generalization else None,
                    "composite_score": candidate.composite_score,
                    "evaluation_artifact_path": str(evaluation_path.relative_to(PROJECT_ROOT)),
                    "rule_baseline_final": asdict(result.rule_baseline),
                    "warnings": list(result.warnings),
                }
                self.models.register(
                    {
                        "id": bundle.model_id,
                        "version": bundle.model_id,
                        "algorithm": bundle.algorithm,
                        "status": "candidate",
                        "early_stage": bundle.early_stage,
                        "trained_at": bundle.created_at.isoformat(),
                        "training_window_start": int(bundle.training_start.timestamp()),
                        "training_window_end": int(bundle.training_end.timestamp()),
                        "validation_window_start": int(
                            dataset.timestamps.iloc[result.plan.final_split.test_indices[0]].timestamp()
                        ),
                        "validation_window_end": int(
                            dataset.timestamps.iloc[result.plan.final_split.test_indices[-1]].timestamp()
                        ),
                        "feature_names": list(bundle.feature_names),
                        "parameters": {
                            "candidate_pool": list(TrainerConfig().candidate_names),
                            "selection": "top3_oos_fixed_payoff_decay_occam",
                            "economic_weight": TrainerConfig().economic_weight,
                            "generalization_weight": TrainerConfig().generalization_weight,
                            "gap_hours": result.plan.gap_hours,
                            "feature_names": list(bundle.feature_names),
                            "requested_feature_pool": list(selected_features),
                        },
                        "thresholds": bundle.thresholds.as_dict(),
                        "metrics": metrics,
                        "artifact_path": str(production_path.relative_to(PROJECT_ROOT)),
                        "training_data_hash": data_hash,
                        "parent_model_id": rank1_before["id"] if rank1_before else None,
                    }
                )
                registered.append(
                    {
                        "id": bundle.model_id,
                        "rank": rank,
                        "algorithm": bundle.algorithm,
                        "threshold": bundle.threshold,
                        "feature_names": list(bundle.feature_names),
                        "composite_score": float(candidate.composite_score or 0.0),
                        "economic_score": float(candidate.economic_score or 0.0),
                        "generalization_score": float(
                            candidate.generalization.score if candidate.generalization else 0.0
                        ),
                        "metrics": metrics,
                    }
                )

            completed_at = utc_now_iso()
            summary = {
                "rows": len(dataset),
                "early_stage": result.plan.early_stage,
                "data_hash": data_hash,
                "requested_feature_names": list(selected_features),
                "top_models": registered,
                "rule_baseline_final": asdict(result.rule_baseline),
                "candidates": [asdict(candidate) for candidate in result.candidates],
                "warnings": list(result.warnings),
                "activation": {
                    "status": "waiting_for_flat",
                    "required_flat_strategies": ["model_1", "model_2", "model_3"],
                },
                "selection_formula": {
                    "economic": "mean_clip((6*TP-FP)/(6*N_positive),-1,1)",
                    "generalization": "0.60*AP_skill_mean + 0.20*stability + 0.20*decay",
                    "composite": "0.60*economic + 0.40*generalization",
                    "occam": "smallest feature subset within one standard error of algorithm best",
                },
            }
            self.database.execute(
                """
                UPDATE training_runs
                SET status='completed', completed_at=?, candidate_model_id=?, champion_before_id=?,
                    promoted=0, summary_json=?
                WHERE id=?
                """,
                (
                    completed_at,
                    registered[0]["id"],
                    rank1_before["id"] if rank1_before else None,
                    json.dumps(summary, ensure_ascii=False),
                    run_id,
                ),
            )
            self.database.set_runtime_state("last_training_completed_at", completed_at)
            self.database.audit(
                category="model",
                action="top3_training_completed_pending_activation",
                entity_type="training_run",
                entity_id=run_id,
                details={
                    "model_ids": [item["id"] for item in registered],
                    "algorithms": [item["algorithm"] for item in registered],
                    "rows": len(dataset),
                },
            )
            self.promote_pending_if_flat(run_id=run_id)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:2000]
            self.database.execute(
                "UPDATE training_runs SET status='failed', completed_at=?, error_message=? WHERE id=?",
                (utc_now_iso(), message, run_id),
            )
            self.database.audit(
                category="model",
                action="training_failed",
                severity="error",
                entity_type="training_run",
                entity_id=run_id,
                details={"error": message, "trace_tail": traceback.format_exc(limit=2)[-1500:]},
            )

    def promote_pending_if_flat(self, *, run_id: str | None = None) -> str | None:
        """Activate the newest completed Top-3 only after model strategies are flat.

        Training and activation are deliberately decoupled. A completed candidate
        set survives process restarts in ``training_runs.summary_json`` and the
        TrainingWorker retries this promotion check every poll. Older pending
        generations are ignored once a newer completed candidate set exists.
        """
        if run_id is not None:
            candidates = self.database.fetch_all(
                "SELECT * FROM training_runs WHERE id=? AND status='completed' AND promoted=0",
                (run_id,),
            )
        else:
            candidates = self.database.fetch_all(
                """
                SELECT * FROM training_runs
                WHERE status='completed' AND promoted=0
                ORDER BY completed_at DESC, requested_at DESC
                """
            )
        pending: dict[str, Any] | None = None
        top_models: list[dict[str, Any]] = []
        for row in candidates:
            try:
                summary = json.loads(row.get("summary_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            proposed = summary.get("top_models") if isinstance(summary, dict) else None
            if not isinstance(proposed, list) or len(proposed) != 3:
                continue
            ids = [str(item.get("id") or "") for item in proposed if isinstance(item, dict)]
            if len(ids) != 3 or not all(ids):
                continue
            statuses = self.database.fetch_all(
                f"SELECT id,status FROM models WHERE id IN ({','.join('?' for _ in ids)})",
                tuple(ids),
            )
            status_map = {str(item["id"]): str(item["status"]) for item in statuses}
            if not all(status_map.get(model_id) == "candidate" for model_id in ids):
                continue
            pending = row
            top_models = [dict(item) for item in proposed]
            break
        if pending is None:
            return None

        open_count = int((self.database.fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM positions
            WHERE account_kind='simulation'
              AND strategy_key IN ('model_1','model_2','model_3')
              AND status IN ('opening','open','closing','manual_intervention')
            """
        ) or {"count": 0})["count"])
        if open_count:
            now = utc_now_iso()
            self.database.set_runtime_state("model_entries_paused_for_rollover", True)
            self.database.set_runtime_state(
                "model_entry_rollover_gate",
                {
                    "paused": True,
                    "scheduled_for": pending.get("scheduled_for"),
                    "schedule_mode": "manual" if not pending.get("scheduled_for") else str(pending.get("trigger") or "scheduled"),
                    "reason": "candidate_models_waiting_for_all_model_positions_to_close",
                    "updated_at": now,
                },
            )
            self.database.set_runtime_state(
                "model_rollover_status",
                {
                    "state": "waiting_for_flat",
                    "run_id": str(pending["id"]),
                    "open_model_positions": open_count,
                    "candidate_model_ids": [str(item["id"]) for item in top_models],
                    "trained_at": pending.get("completed_at"),
                    "updated_at": now,
                },
            )
            return None

        activated_at = utc_now_iso()
        self.models.set_active_models(top_models)
        # Import locally to keep the training module independent of simulation
        # implementation details during module import.
        from .paper_trading import PaperTradingService

        PaperTradingService(self.database, self.settings).reset_model_accounts_for_activation(
            top_models,
            activated_at=activated_at,
        )
        try:
            summary = json.loads(pending.get("summary_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            summary = {}
        summary["activation"] = {
            "status": "activated",
            "activated_at": activated_at,
            "required_flat_strategies": ["model_1", "model_2", "model_3"],
        }
        self.database.execute(
            "UPDATE training_runs SET promoted=1, summary_json=? WHERE id=? AND promoted=0",
            (json.dumps(summary, ensure_ascii=False), pending["id"]),
        )
        self.database.set_runtime_state("last_model_activation_at", activated_at)
        self.database.set_runtime_state("model_entries_paused_for_rollover", False)
        self.database.set_runtime_state(
            "model_entry_rollover_gate",
            {
                "paused": False,
                "scheduled_for": pending.get("scheduled_for"),
                "schedule_mode": str(pending.get("trigger") or "manual"),
                "reason": "candidate_models_activated_after_flat",
                "updated_at": activated_at,
            },
        )
        self.database.set_runtime_state(
            "model_rollover_status",
            {
                "state": "activated",
                "run_id": str(pending["id"]),
                "activated_at": activated_at,
                "model_ids": [str(item["id"]) for item in top_models],
                "open_model_positions": 0,
            },
        )
        self.database.audit(
            category="model",
            action="top3_activated_after_flat",
            entity_type="training_run",
            entity_id=str(pending["id"]),
            details={
                "activated_at": activated_at,
                "model_ids": [str(item["id"]) for item in top_models],
            },
        )
        return str(pending["id"])

    def rollback_model(self, model_id: str) -> dict[str, Any]:
        target = self.models.get(model_id)
        if not target:
            raise ValueError("model not found")
        if target["status"] != "retired":
            raise ValueError("only a retired rank-1 model can be rolled back")
        open_count = int((self.database.fetch_one(
            """
            SELECT COUNT(*) AS count FROM positions
            WHERE account_kind='simulation'
              AND strategy_key IN ('model_1','model_2','model_3')
              AND status IN ('opening','open','closing','manual_intervention')
            """
        ) or {"count": 0})["count"])
        if open_count:
            raise ValueError("model rollback requires model_1/model_2/model_3 to be fully flat")
        artifact = Path(str(target["artifact_path"]))
        artifact = artifact if artifact.is_absolute() else PROJECT_ROOT / artifact
        if not artifact.exists():
            raise ValueError("rollback artifact is missing")
        ModelRegistry(artifact.parent).load(artifact.stem)
        previous = self.models.champion()
        self.models.rollback(model_id)
        activated_at = utc_now_iso()
        active = self.models.active_models()
        from .paper_trading import PaperTradingService

        PaperTradingService(self.database, self.settings).reset_model_accounts_for_activation(
            active,
            activated_at=activated_at,
        )
        self.database.set_runtime_state("last_model_activation_at", activated_at)
        self.database.audit(
            category="model",
            action="model_rolled_back",
            severity="warning",
            entity_type="model",
            entity_id=model_id,
            details={
                "previous_rank1_id": previous["id"] if previous else None,
                "statistics_reset": True,
                "activated_at": activated_at,
            },
        )
        champion = self.models.champion()
        assert champion is not None
        return champion

    def feature_catalog(self) -> dict[str, Any]:
        rows = self.samples.list_mature()
        total = len(rows)
        active = self.models.active_models()
        active_features = [set(model.get("feature_names", [])) for model in active]
        items: list[dict[str, Any]] = []
        for name in AVAILABLE_MODEL_FEATURES:
            present = 0
            for row in rows:
                value = row.get("entry_price") if name == "price" else row.get(name)
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                try:
                    if pd.isna(value):
                        continue
                except (TypeError, ValueError):
                    pass
                present += 1
            items.append(
                {
                    "name": name,
                    "default_enabled": name in DEFAULT_MODEL_TRAINING_FEATURES,
                    "active_model_slots": [
                        index + 1 for index, features in enumerate(active_features) if name in features
                    ],
                    "available_rows": present,
                    "total_mature_rows": total,
                    "coverage": (present / total) if total else 0.0,
                }
            )
        return {
            "default_features": list(DEFAULT_MODEL_TRAINING_FEATURES),
            "available_features": list(AVAILABLE_MODEL_FEATURES),
            "total_mature_rows": total,
            "active_model_count": len(active),
            "items": items,
        }

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT * FROM training_runs ORDER BY requested_at DESC LIMIT ?", (limit,)
        )
        for row in rows:
            row["summary"] = json.loads(row.pop("summary_json") or "{}")
            row["request"] = json.loads(row.pop("request_json") or "{}")
            row["promoted"] = bool(row["promoted"])
        return rows

    @staticmethod
    def _training_frame(rows: list[dict[str, Any]]) -> tuple[pd.DataFrame, str]:
        if not rows:
            raise ValueError("no mature samples are available")
        records: list[dict[str, Any]] = []
        digest = hashlib.sha256()
        for row in rows:
            features = {
                key: (row.get("entry_price") if key == "price" else row.get(key))
                for key in AVAILABLE_MODEL_FEATURES
            }
            features.update(
                {
                    "time": row["entry_time"],
                    "tag": row["tag"],
                    "launchpad": row.get("launchpad"),
                    "liquidity_usd": row.get("liquidity"),
                    "final_close_ratio": row.get("final_close_ratio"),
                    "return_is_estimated": bool(row.get("terminal_return_estimated")),
                }
            )
            records.append(features)
            digest.update(f"{row['sample_key']}|{row['tag']}|{row.get('updated_at')}\n".encode())
        return pd.DataFrame.from_records(records), digest.hexdigest()
