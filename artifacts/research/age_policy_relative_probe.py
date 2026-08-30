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
from backend.app.services.training import TrainingService

RUN_ID = "df7aba38-a885-4ec2-8d03-42ba1bcc69a6"


def relative_age_threshold(base: float, age_minutes: float | int | None) -> tuple[bool, float, str]:
    if age_minutes is None:
        return False, base, "age_missing"
    age = float(age_minutes)
    if not 2.0 < age < 300.0:
        return False, base, "age_outside_admission_contract"
    if age < 10.0:
        return True, min(0.999999, base + 0.04), "age_2_10_plus_004"
    if age < 30.0:
        return True, min(0.999999, base + 0.04), "age_10_30_plus_004"
    if age < 60.0:
        return True, base, "age_30_60_base"
    if age < 120.0:
        return False, base, "age_60_120_abstain"
    return True, base, "age_120_300_base"


def main() -> None:
    settings = get_settings()
    db = Database(settings.database_path)
    service = TrainingService(db, settings)
    run = db.fetch_one("SELECT summary_json FROM training_runs WHERE id=?", (RUN_ID,))
    if not run:
        raise SystemExit("run not found")
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

    output = {"run_id": RUN_ID, "final_rows": len(positions), "models": []}
    for top in summary.get("top_models") or []:
        algorithm = top["algorithm"]
        model_cert = cert_by_algorithm[algorithm]
        base = float(model_cert["development_budget_threshold"])
        eval_path = top.get("metrics", {}).get("evaluation_artifact_path")
        bundle = registry.load(Path(eval_path).stem)
        probabilities = bundle.predict_probabilities(
            dataset.X.iloc[positions][list(bundle.feature_names)]
        )
        risk_model = bundle.execution_risk_model
        rows_out = []
        for i, (source, probability, label) in enumerate(zip(source_rows, probabilities, labels, strict=True)):
            allowed, threshold, age_reason = relative_age_threshold(base, source.get("age_minutes"))
            try:
                risk = float(risk_model.predict_probability(source)) if risk_model is not None else None
            except Exception:
                risk = None
            score_ok = bool(allowed and float(probability) >= threshold)
            rows_out.append({
                "i": i,
                "label": int(label),
                "age": source.get("age_minutes"),
                "probability": float(probability),
                "threshold": float(threshold),
                "age_reason": age_reason,
                "score_ok": score_ok,
                "risk": risk,
            })

        sensitivity = {}
        for ceiling in (0.35, 0.40, 0.45, 0.50):
            selected = [r for r in rows_out if r["score_ok"] and r["risk"] is not None and r["risk"] <= ceiling]
            tp = sum(r["label"] == 1 for r in selected)
            fp = len(selected) - tp
            sensitivity[str(ceiling)] = {
                "selected": len(selected),
                "tp": tp,
                "fp": fp,
                "precision": (tp / len(selected)) if selected else None,
                "profit_units_3to1": float(3 * tp - fp),
                "items": selected,
            }
        score_only = [r for r in rows_out if r["score_ok"]]
        tp_score = sum(r["label"] == 1 for r in score_only)
        output["models"].append({
            "algorithm": algorithm,
            "base_threshold": base,
            "score_age_selected": len(score_only),
            "score_age_tp": tp_score,
            "score_age_fp": len(score_only) - tp_score,
            "score_age_precision": (tp_score / len(score_only)) if score_only else None,
            "score_age_profit_units_3to1": float(3 * tp_score - (len(score_only) - tp_score)),
            "risk_sensitivity": sensitivity,
        })
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
