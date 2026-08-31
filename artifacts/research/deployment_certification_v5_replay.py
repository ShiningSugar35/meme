from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import joblib
import pandas as pd

from backend.app.collector.constants import FEATURE_SCHEMA_VERSION, LabelPolicy
from backend.app.ml.decision_policy import age_adjusted_threshold, age_gate

# Historical Phase16 replay constants only; current Phase18 paper does not gate on execution-risk.
RISK_CEILING_CAUTION = 0.35
RISK_CEILING_NORMAL = 0.40
from backend.app.database import Database
from backend.app.ml.features import materialize_entry_feature
from backend.app.services.execution_risk import ExecutionRiskTrainer
from backend.app.services.training import evaluate_final_deployment_evidence

DATABASE_PATH = PROJECT_ROOT / "data" / "meme_quant.db"


def _feature_frame(rows: list[dict[str, Any]], feature_names: tuple[str, ...]) -> pd.DataFrame:
    records: list[dict[str, float]] = []
    for row in rows:
        source = json.loads(row.get("features_json") or "{}")
        records.append(
            {
                name: materialize_entry_feature(
                    name,
                    source,
                    entry_price=row.get("entry_price"),
                )
                for name in feature_names
            }
        )
    return pd.DataFrame.from_records(records, columns=list(feature_names))


def main() -> None:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    run = connection.execute(
        """
        SELECT id,summary_json
        FROM training_runs
        WHERE status='completed' AND promoted=1
        ORDER BY completed_at DESC
        LIMIT 1
        """
    ).fetchone()
    if run is None:
        raise RuntimeError("no promoted training run is available")
    summary = json.loads(run["summary_json"] or "{}")
    deployment = summary.get("deployment_certification") or {}
    window_start = deployment.get("final_window_start")
    window_end = deployment.get("final_window_end")
    if not window_start or not window_end:
        raise RuntimeError("latest promoted run has no frozen final window")

    start_epoch = int(datetime.fromisoformat(str(window_start)).timestamp())
    end_epoch = int(datetime.fromisoformat(str(window_end)).timestamp())
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT *
            FROM samples
            WHERE entry_time BETWEEN ? AND ?
              AND feature_schema_version=?
              AND label_version=?
              AND label_status='mature'
              AND tag IN (0,1)
            ORDER BY entry_time,id
            """,
            (
                start_epoch,
                end_epoch,
                FEATURE_SCHEMA_VERSION,
                LabelPolicy().label_version,
            ),
        )
    ]
    if not rows:
        raise RuntimeError("frozen final window has no matching mature samples")

    labels = [int(row["tag"]) for row in rows]
    risk_training_cutoff = start_epoch - LabelPolicy().window_seconds
    risk_training = ExecutionRiskTrainer(Database(DATABASE_PATH)).train(
        max_entry_time_exclusive=risk_training_cutoff,
        max_exit_time_exclusive=str(window_start),
    )
    if not risk_training.certified or risk_training.model is None:
        raise RuntimeError(
            f"cutoff-safe execution-risk head is unavailable: {risk_training.reason}"
        )
    risk_model = risk_training.model
    old_certification_by_model = {
        str(item.get("model_id")): item
        for item in deployment.get("models", [])
        if item.get("model_id")
    }
    results: list[dict[str, Any]] = []
    for active in connection.execute(
        """
        SELECT a.slot,a.model_id,m.algorithm,m.metrics_json
        FROM active_model_slots a
        JOIN models m ON m.id=a.model_id
        ORDER BY a.slot
        """
    ):
        metrics = json.loads(active["metrics_json"] or "{}")
        artifact = Path(str(metrics["evaluation_artifact_path"]))
        if not artifact.is_absolute():
            artifact = PROJECT_ROOT / artifact
        bundle = joblib.load(artifact)
        frame = _feature_frame(rows, tuple(bundle.feature_names))
        probabilities = bundle.predict_probabilities(frame)
        old_model_cert = old_certification_by_model.get(str(active["model_id"]), {})
        drift_state = str(
            ((old_model_cert.get("final_drift") or {}).get("state"))
            or ((metrics.get("final_drift_certification") or {}).get("state"))
            or "normal"
        )
        risk_ceiling = (
            RISK_CEILING_CAUTION if drift_state == "caution" else RISK_CEILING_NORMAL
        )
        selected_mask: list[bool] = []
        for row, probability in zip(rows, probabilities, strict=True):
            age = age_gate(row.get("age_minutes"))
            threshold = age_adjusted_threshold(float(bundle.threshold), age)
            risk_probability = float(risk_model.predict_probability(row))
            selected_mask.append(
                bool(
                    age.allowed
                    and risk_probability is not None
                    and risk_probability <= risk_ceiling
                    and drift_state != "severe"
                    and float(probability) >= threshold
                )
            )
        evidence = evaluate_final_deployment_evidence(
            labels,
            probabilities,
            selected_mask,
        )
        results.append(
            {
                "slot": int(active["slot"]),
                "model_id": str(active["model_id"]),
                "algorithm": str(active["algorithm"]),
                "drift_state": drift_state,
                "old_certification": {
                    "eligible_generation": bool(deployment.get("eligible")),
                    "selected": old_model_cert.get("final_model_policy_selected"),
                    "profit_units": old_model_cert.get("final_model_policy_profit_units"),
                },
                "v5_evidence": evidence,
            }
        )

    hard_blockers = [
        f"{item['algorithm']}:final_recent_drift_severe"
        for item in results
        if item["drift_state"] == "severe"
    ]
    qualified = [
        item["model_id"] for item in results if item["v5_evidence"]["qualified"]
    ]
    if not qualified:
        hard_blockers.append(
            "no_top3_model_has_positive_ranked_end_to_end_final_evidence"
        )
    output = {
        "training_run_id": str(run["id"]),
        "frozen_final_window": {"start": window_start, "end": window_end},
        "rows": len(rows),
        "qualified_model_ids": qualified,
        "risk_training": risk_training.as_dict(),
        "would_v5_generation_be_eligible": not hard_blockers,
        "generation_blockers": hard_blockers,
        "models": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
