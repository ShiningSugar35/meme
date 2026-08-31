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
from backend.app.ml.decision_policy import age_adjusted_threshold, age_gate

# Historical Phase16 replay constants only; current Phase18 paper does not gate on execution-risk.
RISK_CEILING_CAUTION = 0.35
RISK_CEILING_NORMAL = 0.40
from backend.app.ml.features import FeatureBuilder, FeaturePolicy
from backend.app.ml.registry import ModelRegistry
from backend.app.services.training import TrainingService


def main() -> None:
    settings = get_settings()
    database = Database(settings.database_path)
    service = TrainingService(database, settings)
    latest = database.fetch_one(
        "SELECT * FROM training_runs WHERE status='completed' ORDER BY completed_at DESC LIMIT 1"
    )
    if not latest:
        raise SystemExit("no completed training run")
    summary = json.loads(latest.get("summary_json") or "{}")
    cert = summary.get("deployment_certification") or {}
    rows = [
        row
        for row in service.samples.list_mature()
        if row.get("feature_schema_version") == FEATURE_SCHEMA_VERSION
        and row.get("label_version") == LabelPolicy().label_version
        and row.get("token_type") in {"new_creation", "near_completion"}
    ]
    frame, _ = service._training_frame(rows)
    selected_features = service.normalize_feature_selection(summary.get("requested_feature_names"))
    dataset = FeatureBuilder(FeaturePolicy(feature_allowlist=selected_features)).prepare(frame)
    start = cert.get("final_window_start")
    end = cert.get("final_window_end")
    start_ts = np.datetime64(start.replace("+00:00", ""))
    end_ts = np.datetime64(end.replace("+00:00", ""))
    timestamp_values = dataset.timestamps.to_numpy(dtype="datetime64[ns]")
    positions = np.flatnonzero((timestamp_values >= start_ts) & (timestamp_values <= end_ts))
    source_rows = [rows[int(dataset.source_rows[position])] for position in positions]
    registry = ModelRegistry(settings.model_directory)

    output: dict[str, object] = {
        "run_id": latest["id"],
        "rows": len(rows),
        "final_rows": int(len(positions)),
        "final_window_start": start,
        "final_window_end": end,
        "models": [],
    }
    cert_by_algorithm = {item["algorithm"]: item for item in cert.get("models") or []}
    for model in summary.get("top_models") or []:
        algorithm = model["algorithm"]
        model_cert = cert_by_algorithm[algorithm]
        eval_rel = model.get("metrics", {}).get("evaluation_artifact_path")
        if not eval_rel:
            continue
        eval_id = Path(eval_rel).stem
        evaluation_bundle = registry.load(eval_id)
        probabilities = evaluation_bundle.predict_probabilities(
            dataset.X.iloc[positions][list(evaluation_bundle.feature_names)]
        )
        threshold = float(model_cert["development_budget_threshold"])
        risk_ceiling = (
            RISK_CEILING_CAUTION
            if model_cert.get("final_drift", {}).get("state") == "caution"
            else RISK_CEILING_NORMAL
        )
        raw_probability_ok = []
        probability_ok = []
        age_ok = []
        risk_ok = []
        all_ok = []
        risk_values = []
        probability_values = []
        age_reasons: dict[str, int] = {}
        for source, probability in zip(source_rows, probabilities, strict=True):
            age = age_gate(source.get("age_minutes"), evaluation_bundle.age_policy_version)
            age_reasons[age.reason] = age_reasons.get(age.reason, 0) + 1
            probability_value = float(probability)
            hard_threshold = age_adjusted_threshold(threshold, age)
            raw_p_ok = probability_value >= threshold
            p_ok = probability_value >= hard_threshold
            try:
                risk = float(evaluation_bundle.execution_risk_model.predict_probability(source))
            except Exception:
                risk = float("nan")
            r_ok = bool(np.isfinite(risk) and risk <= risk_ceiling)
            raw_probability_ok.append(raw_p_ok)
            probability_ok.append(p_ok)
            age_ok.append(bool(age.allowed))
            risk_ok.append(r_ok)
            all_ok.append(bool(age.allowed and p_ok and r_ok))
            risk_values.append(risk)
            probability_values.append(probability_value)
        finite_risk = np.asarray([x for x in risk_values if np.isfinite(x)], dtype=float)
        output["models"].append(
            {
                "algorithm": algorithm,
                "threshold": threshold,
                "risk_ceiling": risk_ceiling,
                "raw_probability_ok": int(sum(raw_probability_ok)),
                "age_adjusted_probability_ok": int(sum(probability_ok)),
                "age_ok": int(sum(age_ok)),
                "risk_ok": int(sum(risk_ok)),
                "probability_and_age": int(sum(p and a for p, a in zip(probability_ok, age_ok, strict=True))),
                "probability_and_risk": int(sum(p and r for p, r in zip(probability_ok, risk_ok, strict=True))),
                "age_and_risk": int(sum(a and r for a, r in zip(age_ok, risk_ok, strict=True))),
                "all_ok": int(sum(all_ok)),
                "age_reasons": age_reasons,
                "model_probability_quantiles": {
                    str(q): float(np.quantile(np.asarray(probability_values, dtype=float), q))
                    for q in (0.5, 0.75, 0.9, 0.95, 0.99)
                },
                "risk_ceiling_sensitivity": {
                    str(ceiling): int(sum(
                        bool(a and p and np.isfinite(r) and r <= ceiling)
                        for a, p, r in zip(age_ok, probability_ok, risk_values, strict=True)
                    ))
                    for ceiling in (0.40, 0.45, 0.50, 0.55, 0.60)
                },
                "risk_probability_quantiles": (
                    {str(q): float(np.quantile(finite_risk, q)) for q in (0.1, 0.25, 0.5, 0.75, 0.9)}
                    if finite_risk.size
                    else {}
                ),
            }
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
