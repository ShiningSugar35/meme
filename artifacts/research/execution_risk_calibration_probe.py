from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from backend.app.config import get_settings
from backend.app.database import Database
from backend.app.ml.calibration import fit_sigmoid_calibrator
from backend.app.ml.features import AVAILABLE_MODEL_FEATURES, materialize_entry_feature
from backend.app.services.execution_risk import ExecutionRiskTrainer


def main() -> None:
    db = Database(get_settings().database_path)
    trainer = ExecutionRiskTrainer(db)
    labelled = []
    for row in trainer._rows():
        target = trainer._target(row)
        if target is not None:
            labelled.append((row, target))
    y = np.asarray([target for _, target in labelled], dtype=int)
    names = ("age_minutes", *AVAILABLE_MODEL_FEATURES)
    materialized = {name: [] for name in names}
    for row, _ in labelled:
        try:
            source = json.loads(str(row.get("features_json") or "{}"))
        except json.JSONDecodeError:
            source = {}
        for name in names:
            value = row.get("age_minutes") if name == "age_minutes" else materialize_entry_feature(
                name, source, entry_price=row.get("entry_price")
            )
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
            materialized[name].append(value if value is not None and np.isfinite(value) else None)
    usable = [name for name, values in materialized.items() if sum(v is not None for v in values) >= int(.70 * len(y))]
    train_end = int(len(y) * .70)
    calibrate_end = int(len(y) * .85)
    X = np.empty((len(y), len(usable)), dtype=float)
    medians = {}
    for j, name in enumerate(usable):
        values = np.asarray([np.nan if v is None else float(v) for v in materialized[name]], dtype=float)
        median = float(np.nanmedian(values[:train_end]))
        if not np.isfinite(median):
            median = 0.0
        medians[name] = median
        values[~np.isfinite(values)] = median
        X[:, j] = values

    factories = {
        "extra_trees_balanced": lambda: ExtraTreesClassifier(n_estimators=400,max_depth=6,min_samples_leaf=12,max_features="sqrt",class_weight="balanced",random_state=42,n_jobs=1),
        "extra_trees_unweighted": lambda: ExtraTreesClassifier(n_estimators=400,max_depth=6,min_samples_leaf=12,max_features="sqrt",random_state=42,n_jobs=1),
        "logistic_unweighted": lambda: Pipeline([("scale", StandardScaler()),("model", LogisticRegression(C=.5,max_iter=2000,random_state=42))]),
        "logistic_balanced": lambda: Pipeline([("scale", StandardScaler()),("model", LogisticRegression(C=.5,class_weight="balanced",max_iter=2000,random_state=42))]),
    }
    out = {
        "rows": len(y),
        "prevalence": float(y.mean()),
        "train_rows": train_end,
        "calibration_rows": calibrate_end-train_end,
        "certification_rows": len(y)-calibrate_end,
        "models": {},
    }
    for name, factory in factories.items():
        est = factory()
        est.fit(X[:train_end], y[:train_end])
        cal_raw = est.predict_proba(X[train_end:calibrate_end])[:,1]
        cert_raw = est.predict_proba(X[calibrate_end:])[:,1]
        calibrator = fit_sigmoid_calibrator(
            cal_raw,
            y[train_end:calibrate_end],
            source_indices=range(train_end,calibrate_end),
        )
        cert_cal = calibrator.transform(cert_raw)
        out["models"][name] = {
            "calibration_prevalence": float(y[train_end:calibrate_end].mean()),
            "certification_prevalence": float(y[calibrate_end:].mean()),
            "raw": {
                "auc": float(roc_auc_score(y[calibrate_end:], cert_raw)),
                "ap": float(average_precision_score(y[calibrate_end:], cert_raw)),
                "brier": float(brier_score_loss(y[calibrate_end:], cert_raw)),
                "mean": float(np.mean(cert_raw)),
                "quantiles": {str(q):float(np.quantile(cert_raw,q)) for q in (.1,.25,.5,.75,.9)},
            },
            "calibrated": {
                "auc": float(roc_auc_score(y[calibrate_end:], cert_cal)),
                "ap": float(average_precision_score(y[calibrate_end:], cert_cal)),
                "brier": float(brier_score_loss(y[calibrate_end:], cert_cal)),
                "mean": float(np.mean(cert_cal)),
                "quantiles": {str(q):float(np.quantile(cert_cal,q)) for q in (.1,.25,.5,.75,.9)},
            },
            "calibrator": calibrator.provenance(),
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
