from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from backend.app.collector.constants import FEATURE_SCHEMA_VERSION, LabelPolicy
from backend.app.config import get_settings
from backend.app.database import Database
from backend.app.ml.decision_policy import (
    AGE_POLICY_CANDIDATES,
    DECISION_POLICY_VERSION,
    DEPLOYMENT_CERTIFICATION_VERSION,
)
from backend.app.ml.features import FeatureBuilder, FeaturePolicy
from backend.app.ml.registry import ModelRegistry
from backend.app.services.training import TrainingService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify that the current deployment certificate covers the exact deployable fitted instances."
    )
    parser.add_argument("--run-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    database = Database(settings.database_path)
    service = TrainingService(database, settings)
    run = database.fetch_one(
        "SELECT * FROM training_runs WHERE id=? AND status='completed'",
        (args.run_id,),
    )
    if not run:
        raise SystemExit(f"completed training run not found: {args.run_id}")
    summary = json.loads(run.get("summary_json") or "{}")
    certification = summary.get("deployment_certification") or {}
    final_start = certification.get("final_window_start")
    final_end = certification.get("final_window_end")
    if not final_start or not final_end:
        raise SystemExit("training run has no frozen final window")

    rows = [
        row
        for row in service.samples.list_mature()
        if row.get("feature_schema_version") == FEATURE_SCHEMA_VERSION
        and row.get("label_version") == LabelPolicy().label_version
        and row.get("token_type") in {"new_creation", "near_completion"}
    ]
    frame, _ = service._training_frame(rows)
    requested = service.normalize_feature_selection(summary.get("requested_feature_names"))
    dataset = FeatureBuilder(FeaturePolicy(feature_allowlist=requested)).prepare(frame)
    timestamps = dataset.timestamps.to_numpy(dtype="datetime64[ns]")
    start = np.datetime64(str(final_start).replace("+00:00", ""))
    end = np.datetime64(str(final_end).replace("+00:00", ""))
    final_positions = np.flatnonzero((timestamps >= start) & (timestamps <= end))
    if not len(final_positions):
        raise SystemExit("frozen final window has no matching current mature samples")

    registry = ModelRegistry(settings.model_directory)
    failures: list[str] = []
    models_out: list[dict[str, object]] = []
    cert_by_id = {
        str(item.get("model_id")): item
        for item in certification.get("models") or []
        if item.get("model_id")
    }
    final_start_epoch = int(dataset.timestamps.iloc[final_positions[0]].timestamp())
    for top in summary.get("top_models") or []:
        model_id = str(top.get("id") or "")
        model = service.models.get(model_id)
        if not model:
            failures.append(f"{model_id}:model_registry_missing")
            continue
        metrics = model.get("metrics") or {}
        parameters = model.get("parameters") or {}
        evaluation_path = Path(str(metrics.get("evaluation_artifact_path") or ""))
        production_path = Path(str(model.get("artifact_path") or ""))
        if not evaluation_path.is_absolute():
            evaluation_path = PROJECT_ROOT / evaluation_path
        if not production_path.is_absolute():
            production_path = PROJECT_ROOT / production_path
        production = registry.load(production_path.stem)
        evaluation = registry.load(evaluation_path.stem)
        features = list(production.feature_names)
        production_probabilities = np.asarray(
            production.predict_probabilities(dataset.X.iloc[final_positions][features]),
            dtype=float,
        )
        evaluation_probabilities = np.asarray(
            evaluation.predict_probabilities(dataset.X.iloc[final_positions][features]),
            dtype=float,
        )
        max_abs_delta = float(
            np.max(np.abs(production_probabilities - evaluation_probabilities))
        )
        training_end = int(model.get("training_window_end") or 0)
        gap_seconds = final_start_epoch - training_end
        model_cert = cert_by_id.get(model_id) or metrics.get("deployment_certification") or {}
        checks = {
            "decision_policy_current": parameters.get("decision_policy_version")
            == DECISION_POLICY_VERSION,
            "certification_version_current": model_cert.get("version")
            == DEPLOYMENT_CERTIFICATION_VERSION,
            "deployment_fit_scope_current": parameters.get("deployment_fit_scope")
            == "final_train_only_certified_instance",
            "age_policy_current": parameters.get("age_policy_version")
            in AGE_POLICY_CANDIDATES,
            "production_evaluation_probabilities_identical": max_abs_delta <= 1e-12,
            "full_label_gap_before_final": gap_seconds >= LabelPolicy().window_seconds,
        }
        for name, passed in checks.items():
            if not passed:
                failures.append(f"{model_id}:{name}")
        models_out.append(
            {
                "model_id": model_id,
                "algorithm": model.get("algorithm"),
                "age_policy_version": parameters.get("age_policy_version"),
                "qualified_deployment_evidence": model_cert.get(
                    "qualified_deployment_evidence"
                ),
                "training_to_final_gap_seconds": gap_seconds,
                "max_abs_probability_delta": max_abs_delta,
                "checks": checks,
            }
        )

    output = {
        "run_id": args.run_id,
        "decision_policy_version": DECISION_POLICY_VERSION,
        "deployment_certification_version": DEPLOYMENT_CERTIFICATION_VERSION,
        "generation_eligible": bool(certification.get("eligible")),
        "generation_blockers": list(certification.get("blockers") or []),
        "final_rows": int(len(final_positions)),
        "models": models_out,
        "failures": failures,
        "passed": not failures and len(models_out) == 3,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
