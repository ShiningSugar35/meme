from __future__ import annotations

import hashlib
import json
import traceback
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
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
from ..ml.promotion import PromotionConfig, PromotionEvaluator
from ..ml.registry import ModelRegistry
from ..ml.trainer import ModelTrainer, TrainerConfig
from ..repositories.models import ModelRepository
from ..repositories.samples import SampleRepository


TrainingTrigger = Literal["manual", "weekly", "startup_catchup", "degraded"]

_SAMPLE_METADATA = {
    "id",
    "sample_key",
    "chain",
    "address",
    "name",
    "symbol",
    "token_type",
    "entry_time",
    "age_minutes",
    "launchpad",
    "entry_price",
    "liquidity",
    "liquidity_estimated",
    "utility_eligible",
    "holder_count",
    "price_2h_max_ratio",
    "price_2h_min_ratio",
    "final_close_ratio",
    "tag",
    "label_status",
    "label_version",
    "label_source",
    "terminal_return_estimated",
    "collected_at",
    "updated_at",
}


class TrainingService:
    """Persistent model-training orchestration around the pure ML package."""

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
                "SELECT id FROM training_runs WHERE status='running' AND id<>? LIMIT 1", (run_id,)
            ).fetchone()
            if running:
                # Keep the durable request queued. The single training worker will
                # return to it after the active run completes; a transient overlap
                # must not permanently discard a manual/weekly/degraded request.
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

        champion_before = self.models.champion()
        try:
            try:
                request_payload = json.loads(row.get("request_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                request_payload = {}
            selected_features = self.normalize_feature_selection(
                request_payload.get("feature_names")
            )
            rows = self.samples.list_mature()
            frame, data_hash = self._training_frame(rows)
            dataset = FeatureBuilder(
                FeaturePolicy(
                    tag2_return_floor=0.20,
                    feature_allowlist=selected_features,
                )
            ).prepare(frame)
            result = ModelTrainer(
                TrainerConfig(min_precision=self.settings.min_precision)
            ).train(dataset)

            production_path = self.registry.save(result.bundle)
            evaluation_path = self.registry.save(result.evaluation_bundle)
            metrics = self._metrics_payload(result, evaluation_path)
            self.models.register(
                {
                    "id": result.bundle.model_id,
                    "version": result.bundle.model_id,
                    "algorithm": result.selected_algorithm,
                    "status": "candidate",
                    "early_stage": result.bundle.early_stage,
                    "trained_at": result.bundle.created_at.isoformat(),
                    "training_window_start": int(result.bundle.training_start.timestamp()),
                    "training_window_end": int(result.bundle.training_end.timestamp()),
                    "validation_window_start": int(dataset.timestamps.iloc[result.plan.final_split.test_indices[0]].timestamp()),
                    "validation_window_end": int(dataset.timestamps.iloc[result.plan.final_split.test_indices[-1]].timestamp()),
                    "feature_names": list(result.bundle.feature_names),
                    "parameters": {
                        "candidate_pool": list(TrainerConfig().candidate_names),
                        "selection": "chronological_oos_utility_with_occam_tiebreak",
                        "gap_hours": result.plan.gap_hours,
                        "feature_names": list(selected_features),
                    },
                    "thresholds": result.bundle.thresholds.as_dict(),
                    "metrics": metrics,
                    "artifact_path": str(production_path.relative_to(PROJECT_ROOT)),
                    "training_data_hash": data_hash,
                    "parent_model_id": champion_before["id"] if champion_before else None,
                }
            )

            promoted = False
            promotion_summary: dict[str, Any]
            if champion_before is None:
                # Bootstrap is allowed so the user can start shadow/simulation/live
                # validation before 120 days. It is not a claim of dollar-utility superiority.
                self.models.promote(result.bundle.model_id)
                promoted = True
                promotion_summary = {
                    "bootstrap": True,
                    "promotion_eligible": False,
                    "reason": "first validated model; no incumbent exists",
                }
            else:
                promotion_summary = self._compare_for_promotion(
                    result,
                    dataset,
                    frame,
                    champion_before,
                )
                if promotion_summary.get("promote"):
                    self.models.promote(result.bundle.model_id)
                    promoted = True
                else:
                    self.database.execute(
                        """
                        UPDATE models
                        SET status='rejected', rejection_reason=?
                        WHERE id=? AND status='candidate'
                        """,
                        ("; ".join(promotion_summary.get("blockers", []))[:2000], result.bundle.model_id),
                    )

            summary = {
                "selected_algorithm": result.selected_algorithm,
                "candidate_model_id": result.bundle.model_id,
                "rows": len(dataset),
                "early_stage": result.bundle.early_stage,
                "data_hash": data_hash,
                "feature_names": list(selected_features),
                "warnings": list(result.warnings),
                "promotion": promotion_summary,
            }
            self.database.execute(
                """
                UPDATE training_runs
                SET status='completed', completed_at=?, candidate_model_id=?, champion_before_id=?,
                    promoted=?, summary_json=?
                WHERE id=?
                """,
                (
                    utc_now_iso(),
                    result.bundle.model_id,
                    champion_before["id"] if champion_before else None,
                    int(promoted),
                    json.dumps(summary, ensure_ascii=False),
                    run_id,
                ),
            )
            self.database.set_runtime_state("last_training_completed_at", utc_now_iso())
            self.database.audit(
                category="model",
                action="training_completed",
                entity_type="training_run",
                entity_id=run_id,
                details={"model_id": result.bundle.model_id, "promoted": promoted, "rows": len(dataset)},
            )
        except Exception as exc:
            # Store a concise diagnostic, never a full environment or secret-bearing response.
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

    def rollback_model(self, model_id: str) -> dict[str, Any]:
        target = self.models.get(model_id)
        if not target:
            raise ValueError("model not found")
        if target["status"] != "retired":
            raise ValueError("only a retired model can be rolled back")
        artifact = Path(str(target["artifact_path"]))
        artifact = artifact if artifact.is_absolute() else PROJECT_ROOT / artifact
        if not artifact.exists():
            raise ValueError("rollback artifact is missing")
        ModelRegistry(artifact.parent).load(artifact.stem)
        previous = self.models.champion()
        self.models.rollback(model_id)
        self.database.audit(
            category="model",
            action="model_rolled_back",
            severity="warning",
            entity_type="model",
            entity_id=model_id,
            details={"previous_champion_id": previous["id"] if previous else None},
        )
        champion = self.models.champion()
        assert champion is not None
        return champion

    def feature_catalog(self) -> dict[str, Any]:
        rows = self.samples.list_mature()
        total = len(rows)
        champion = self.models.champion()
        champion_features = set(champion.get("feature_names", [])) if champion else set()
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
                    "champion_enabled": name in champion_features,
                    "available_rows": present,
                    "total_mature_rows": total,
                    "coverage": (present / total) if total else 0.0,
                }
            )
        return {
            "default_features": list(DEFAULT_MODEL_TRAINING_FEATURES),
            "available_features": list(AVAILABLE_MODEL_FEATURES),
            "total_mature_rows": total,
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
            # Build a superset frame once; the FeatureBuilder allowlist decides
            # which entry-time columns each recipe may actually consume.
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

    @staticmethod
    def _metrics_payload(result: Any, evaluation_path: Path) -> dict[str, Any]:
        final = asdict(result.final_metrics)
        return {
            "precision": result.final_metrics.precision,
            "recall": result.final_metrics.recall,
            "trade_count": result.final_metrics.trade_count,
            "cumulative_pnl_usd": result.final_metrics.cumulative_pnl_usd,
            "proxy_pnl": result.final_metrics.proxy_pnl,
            "max_drawdown_usd": result.final_metrics.max_drawdown_usd,
            "promotion_eligible": result.final_metrics.utility_eligible,
            "utility_unit": result.final_metrics.utility_unit,
            "final_recent_window": final,
            "candidates": [asdict(candidate) for candidate in result.candidates],
            "warnings": list(result.warnings),
            "evaluation_artifact_path": str(evaluation_path.relative_to(PROJECT_ROOT)),
        }

    def _compare_for_promotion(
        self,
        result: Any,
        dataset: Any,
        frame: pd.DataFrame,
        champion: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            incumbent_features = tuple(champion.get("feature_names") or ())
            if not incumbent_features:
                raise ValueError("incumbent feature schema is missing")

            # Rebuild the incumbent recipe on the *same current pre-holdout
            # training history*. Loading its old production refit artifact would
            # make the comparison unfair and, because it is not evaluation-only,
            # would also permanently block automatic updates after bootstrap.
            incumbent_dataset = FeatureBuilder(
                FeaturePolicy(
                    tag2_return_floor=0.20,
                    feature_allowlist=incumbent_features,
                )
            ).prepare(frame)
            incumbent_bundle, incumbent_plan, _ = ModelTrainer(
                TrainerConfig(
                    min_precision=self.settings.min_precision,
                    candidate_names=(str(champion["algorithm"]),),
                )
            ).rebuild_evaluation_bundle(
                incumbent_dataset,
                str(champion["algorithm"]),
            )

            candidate_test = result.plan.final_split.test_indices.tolist()
            incumbent_test = incumbent_plan.final_split.test_indices.tolist()
            if candidate_test != incumbent_test:
                raise ValueError("candidate/incumbent final comparison rows differ")

            union_features = tuple(
                dict.fromkeys((*dataset.feature_names, *incumbent_features))
            )
            comparison_dataset = FeatureBuilder(
                FeaturePolicy(
                    tag2_return_floor=0.20,
                    feature_allowlist=union_features,
                )
            ).prepare(frame)
            decision = PromotionEvaluator(
                PromotionConfig(
                    min_precision=self.settings.min_precision,
                    min_pnl_lift=self.settings.promotion_min_pnl_lift,
                )
            ).compare(
                result.evaluation_bundle,
                incumbent_bundle,
                comparison_dataset,
                result.plan.final_split.test_indices,
            )
            payload = asdict(decision)
            payload["incumbent_recipe_rebuilt"] = True
            payload["incumbent_algorithm"] = champion["algorithm"]
            payload["incumbent_feature_names"] = list(incumbent_features)
            return payload
        except Exception as exc:
            return {
                "eligible": False,
                "promote": False,
                "pnl_lift": None,
                "blockers": [f"fair shared-window comparison failed: {type(exc).__name__}: {exc}"],
                "comparison_rows": len(result.plan.final_split.test_indices),
            }

