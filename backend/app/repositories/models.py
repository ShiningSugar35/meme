from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from ..database import Database, utc_now_iso


class ModelRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def champion(self) -> dict[str, Any] | None:
        """Compatibility alias: rank-1 active model is the Champion."""
        active = self.active_models()
        if active:
            return active[0]
        row = self.database.fetch_one("SELECT * FROM models WHERE status='champion' LIMIT 1")
        return self._decode(row) if row else None

    def active_models(self) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """
            SELECT m.*, s.slot AS active_slot, s.composite_score AS active_composite_score,
                   s.threshold AS active_threshold, s.metrics_json AS active_metrics_json,
                   s.selected_at AS active_selected_at
            FROM active_model_slots s
            JOIN models m ON m.id=s.model_id
            ORDER BY s.slot
            """
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            decoded = self._decode(row)
            decoded["active_metrics"] = json.loads(decoded.pop("active_metrics_json") or "{}")
            result.append(decoded)
        return result

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [
            self._decode(row)
            for row in self.database.fetch_all(
                "SELECT * FROM models ORDER BY trained_at DESC LIMIT ?", (limit,)
            )
        ]

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

    def set_active_models(self, models: Sequence[Mapping[str, Any]]) -> None:
        """Atomically install the ranked Top-3 model set."""
        if len(models) != 3:
            raise ValueError("exactly three active models are required")
        ids = [str(model["id"]) for model in models]
        if len(set(ids)) != 3:
            raise ValueError("active model ids must be unique")
        now = utc_now_iso()
        with self.database.transaction(immediate=True) as connection:
            available = {
                str(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM models WHERE id IN ({','.join('?' for _ in ids)})",
                    ids,
                ).fetchall()
            }
            if available != set(ids):
                raise ValueError("one or more active model candidates are not registered")
            connection.execute("DELETE FROM active_model_slots")
            connection.execute("UPDATE models SET status='retired' WHERE status='champion'")
            for slot, model in enumerate(models, start=1):
                model_id = str(model["id"])
                status = "champion" if slot == 1 else "candidate"
                connection.execute(
                    "UPDATE models SET status=?, promoted_at=?, rejection_reason=NULL WHERE id=?",
                    (status, now, model_id),
                )
                connection.execute(
                    """
                    INSERT INTO active_model_slots(
                        slot,model_id,composite_score,threshold,metrics_json,selected_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        slot,
                        model_id,
                        float(model.get("composite_score") or 0.0),
                        float(model.get("threshold") or 0.0),
                        json.dumps(model.get("metrics") or {}, ensure_ascii=False, separators=(",", ":")),
                        now,
                    ),
                )
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"""
                UPDATE models SET status='rejected',
                    rejection_reason=COALESCE(rejection_reason,'not_selected_for_active_top3')
                WHERE status='candidate' AND id NOT IN ({placeholders})
                """,
                ids,
            )

    def promote(self, model_id: str) -> None:
        """Legacy single-model compatibility path used only by old tooling."""
        model = self.get(model_id)
        if model is None:
            raise ValueError("candidate model not found or not promotable")
        active = self.active_models()
        replacement = [model, *[item for item in active if item["id"] != model_id]][:3]
        if len(replacement) != 3:
            raise ValueError("single-model promote is unavailable until three candidates exist")
        self.set_active_models(
            [
                {
                    "id": item["id"],
                    "composite_score": item.get("active_composite_score") or item.get("metrics", {}).get("composite_score") or 0.0,
                    "threshold": item.get("active_threshold") or item.get("thresholds", {}).get("decision") or 0.0,
                    "metrics": item.get("metrics", {}),
                }
                for item in replacement
            ]
        )

    def rollback(self, model_id: str) -> None:
        target = self.get(model_id)
        if target is None or target["status"] != "retired":
            raise ValueError("rollback target must be a retired model")
        active = self.active_models()
        if len(active) < 2:
            raise ValueError("active Top-3 registry is incomplete")
        replacement = [target, *[item for item in active if item["id"] != model_id]][:3]
        self.set_active_models(
            [
                {
                    "id": item["id"],
                    "composite_score": item.get("active_composite_score") or item.get("metrics", {}).get("composite_score") or 0.0,
                    "threshold": item.get("active_threshold") or item.get("thresholds", {}).get("decision") or 0.0,
                    "metrics": item.get("metrics", {}),
                }
                for item in replacement
            ]
        )

    @staticmethod
    def _decode(row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for target, source in (
            ("feature_names", "feature_names_json"),
            ("parameters", "parameters_json"),
            ("thresholds", "thresholds_json"),
            ("metrics", "metrics_json"),
        ):
            if source in result:
                result[target] = json.loads(
                    result.pop(source) or ("[]" if target == "feature_names" else "{}")
                )
        result["early_stage"] = bool(result["early_stage"])
        return result
