from __future__ import annotations

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
from backend.app.ml.features import FeatureBuilder, FeaturePolicy
from backend.app.ml.registry import ModelRegistry
from backend.app.services.execution_risk import ExecutionRiskTrainer
from backend.app.services.training import TrainingService

RUN_ID = "df7aba38-a885-4ec2-8d03-42ba1bcc69a6"


def relative_age_threshold(base: float, age_minutes: float | int | None) -> tuple[bool, float]:
    if age_minutes is None:
        return False, base
    age = float(age_minutes)
    if not 2.0 < age < 300.0:
        return False, base
    if age < 30.0:
        return True, min(0.999999, base + 0.04)
    if age < 60.0:
        return True, base
    if age < 120.0:
        return False, base
    return True, base


def main() -> None:
    settings = get_settings()
    db = Database(settings.database_path)
    risk_result = ExecutionRiskTrainer(db).train()
    print("RISK_TRAINING")
    print(json.dumps(risk_result.as_dict(), ensure_ascii=False, indent=2))
    if not risk_result.certified or risk_result.model is None:
        return
    risk_model = risk_result.model

    service = TrainingService(db, settings)
    run = db.fetch_one("SELECT summary_json FROM training_runs WHERE id=?", (RUN_ID,))
    summary = json.loads(run.get("summary_json") or "{}")
    cert = summary.get("deployment_certification") or {}
    cert_by_algorithm = {x["algorithm"]: x for x in cert.get("models") or []}
    rows = [
        row for row in service.samples.list_mature()
        if row.get("feature_schema_version") == FEATURE_SCHEMA_VERSION
        and row.get("label_version") == LabelPolicy().label_version
        and row.get("token_type") in {"new_creation", "near_completion"}
    ]
    frame, _ = service._training_frame(rows)
    requested = service.normalize_feature_selection(summary.get("requested_feature_names"))
    dataset = FeatureBuilder(FeaturePolicy(feature_allowlist=requested)).prepare(frame)
    start = np.datetime64(str(cert["final_window_start"]).replace("+00:00", ""))
    end = np.datetime64(str(cert["final_window_end"]).replace("+00:00", ""))
    times = dataset.timestamps.to_numpy(dtype="datetime64[ns]")
    positions = np.flatnonzero((times >= start) & (times <= end))
    source_rows = [rows[int(dataset.source_rows[p])] for p in positions]
    labels = dataset.y.iloc[positions].to_numpy(dtype=int)
    registry = ModelRegistry(settings.model_directory)

    risk_scores = np.asarray([risk_model.predict_probability(row) for row in source_rows], dtype=float)
    output = {
        "risk_score_quantiles": {str(q): float(np.quantile(risk_scores, q)) for q in (0.1,0.25,0.5,0.75,0.9,0.95)},
        "models": [],
    }
    for top in summary.get("top_models") or []:
        algorithm = top["algorithm"]
        base = float(cert_by_algorithm[algorithm]["development_budget_threshold"])
        eval_path = top.get("metrics", {}).get("evaluation_artifact_path")
        bundle = registry.load(Path(eval_path).stem)
        probs = bundle.predict_probabilities(dataset.X.iloc[positions][list(bundle.feature_names)])
        candidates = []
        for i, (row, label, prob, risk) in enumerate(zip(source_rows, labels, probs, risk_scores, strict=True)):
            allowed, threshold = relative_age_threshold(base, row.get("age_minutes"))
            if allowed and float(prob) >= threshold:
                candidates.append({
                    "i": i,
                    "label": int(label),
                    "age": row.get("age_minutes"),
                    "probability": float(prob),
                    "threshold": float(threshold),
                    "risk_score": float(risk),
                })
        sensitivity = {}
        for ceiling in (0.25,0.30,0.35,0.40,0.45,0.50,0.55,0.60):
            selected = [c for c in candidates if c["risk_score"] <= ceiling]
            tp = sum(c["label"] == 1 for c in selected)
            fp = len(selected) - tp
            sensitivity[str(ceiling)] = {
                "selected": len(selected),
                "tp": tp,
                "fp": fp,
                "precision": tp / len(selected) if selected else None,
                "profit_units_3to1": float(3 * tp - fp),
            }
        output["models"].append({
            "algorithm": algorithm,
            "age_score_candidates": candidates,
            "sensitivity": sensitivity,
        })
    print("FINAL_REPLAY")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
