from __future__ import annotations

import json
from typing import Any, Mapping

from ..database import Database, utc_now_iso


class ModelRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def champion(self) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT * FROM models WHERE status='champion' LIMIT 1")
        return self._decode(row) if row else None

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [self._decode(row) for row in self.database.fetch_all(
            "SELECT * FROM models ORDER BY trained_at DESC LIMIT ?", (limit,)
        )]

    def get(self, model_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT * FROM models WHERE id=?", (model_id,))
        return self._decode(row) if row else None

    def register(self, model: Mapping[str, Any]) -> None:
        self.database.execute(
            """
            INSERT INTO models(
                id, version, algorithm, status, early_stage, trained_at,
                training_window_start, training_window_end, validation_window_start,
                validation_window_end, feature_names_json, parameters_json,
                thresholds_json, metrics_json, artifact_path, training_data_hash,
                parent_model_id, promoted_at, rejection_reason
            ) VALUES(
                :id, :version, :algorithm, :status, :early_stage, :trained_at,
                :training_window_start, :training_window_end, :validation_window_start,
                :validation_window_end, :feature_names_json, :parameters_json,
                :thresholds_json, :metrics_json, :artifact_path, :training_data_hash,
                :parent_model_id, :promoted_at, :rejection_reason
            )
            """,
            {
                "id": model["id"],
                "version": model["version"],
                "algorithm": model["algorithm"],
                "status": model.get("status", "candidate"),
                "early_stage": int(bool(model.get("early_stage"))),
                "trained_at": model.get("trained_at", utc_now_iso()),
                "training_window_start": model.get("training_window_start"),
                "training_window_end": model.get("training_window_end"),
                "validation_window_start": model.get("validation_window_start"),
                "validation_window_end": model.get("validation_window_end"),
                "feature_names_json": json.dumps(model.get("feature_names", []), ensure_ascii=False),
                "parameters_json": json.dumps(model.get("parameters", {}), ensure_ascii=False),
                "thresholds_json": json.dumps(model.get("thresholds", {}), ensure_ascii=False),
                "metrics_json": json.dumps(model.get("metrics", {}), ensure_ascii=False),
                "artifact_path": str(model["artifact_path"]),
                "training_data_hash": model.get("training_data_hash"),
                "parent_model_id": model.get("parent_model_id"),
                "promoted_at": model.get("promoted_at"),
                "rejection_reason": model.get("rejection_reason"),
            },
        )

    def promote(self, model_id: str) -> None:
        now = utc_now_iso()
        with self.database.transaction(immediate=True) as connection:
            connection.execute("UPDATE models SET status='retired' WHERE status='champion'")
            cursor = connection.execute(
                "UPDATE models SET status='champion', promoted_at=? WHERE id=? AND status='candidate'",
                (now, model_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("candidate model not found or not promotable")

    def rollback(self, model_id: str) -> None:
        now = utc_now_iso()
        with self.database.transaction(immediate=True) as connection:
            target = connection.execute(
                "SELECT status FROM models WHERE id=?", (model_id,)
            ).fetchone()
            if target is None or target["status"] != "retired":
                raise ValueError("rollback target must be a retired model")
            connection.execute("UPDATE models SET status='retired' WHERE status='champion'")
            cursor = connection.execute(
                "UPDATE models SET status='champion', promoted_at=?, rejection_reason=NULL WHERE id=? AND status='retired'",
                (now, model_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("retired model could not be restored")

    @staticmethod
    def _decode(row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for target, source in (
            ("feature_names", "feature_names_json"),
            ("parameters", "parameters_json"),
            ("thresholds", "thresholds_json"),
            ("metrics", "metrics_json"),
        ):
            result[target] = json.loads(result.pop(source) or ("[]" if target == "feature_names" else "{}"))
        result["early_stage"] = bool(result["early_stage"])
        return result

