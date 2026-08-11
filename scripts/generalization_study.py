from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import get_settings
from backend.app.database import Database
from backend.app.ml.features import DEFAULT_MODEL_TRAINING_FEATURES, FeatureBuilder, FeaturePolicy
from backend.app.ml.models import build_pipeline, candidate_catalog, fit_pipeline, positive_probabilities
from backend.app.ml.splits import TemporalSplitter
from backend.app.repositories.samples import SampleRepository
from backend.app.services.training import TrainingService


def threshold_from_development(y: np.ndarray, probabilities: np.ndarray) -> dict[str, float | int | None]:
    best: tuple[float, float, int, float] | None = None
    fallback: tuple[float, float, int, float] | None = None
    positives = max(int(np.sum(y == 1)), 1)
    for threshold in sorted(set(float(value) for value in probabilities), reverse=True):
        selected = probabilities >= threshold
        trades = int(selected.sum())
        if trades < 3:
            continue
        tp = int(np.sum(selected & (y == 1)))
        precision = tp / trades
        recall = tp / positives
        candidate = (precision, recall, trades, threshold)
        if fallback is None or candidate[:3] > fallback[:3]:
            fallback = candidate
        if precision >= 0.35:
            ranked = (recall, precision, trades, threshold)
            if best is None or ranked > (best[1], best[0], best[2], best[3]):
                best = candidate
    chosen = best or fallback
    if chosen is None:
        return {"threshold": None, "development_precision": None, "development_recall": 0.0, "development_trades": 0, "met_35_precision": False}
    precision, recall, trades, threshold = chosen
    return {
        "threshold": threshold,
        "development_precision": precision,
        "development_recall": recall,
        "development_trades": trades,
        "met_35_precision": precision >= 0.35,
    }


def classification_metrics(y: np.ndarray, probabilities: np.ndarray, threshold: float | None) -> dict[str, float | int | None]:
    auc = float(roc_auc_score(y, probabilities)) if len(set(y.tolist())) > 1 else None
    ap = float(average_precision_score(y, probabilities)) if int(y.sum()) else None
    if threshold is None:
        return {"roc_auc": auc, "average_precision": ap, "precision": None, "recall": 0.0, "trades": 0}
    selected = probabilities >= threshold
    trades = int(selected.sum())
    tp = int(np.sum(selected & (y == 1)))
    positives = int(np.sum(y == 1))
    return {
        "roc_auc": auc,
        "average_precision": ap,
        "precision": (tp / trades) if trades else None,
        "recall": (tp / positives) if positives else 0.0,
        "trades": trades,
    }


def main() -> int:
    settings = get_settings()
    database = Database(settings.database_path)
    rows = SampleRepository(database).list_mature()
    frame, data_hash = TrainingService._training_frame(rows)
    dataset = FeatureBuilder(FeaturePolicy(feature_allowlist=DEFAULT_MODEL_TRAINING_FEATURES)).prepare(frame)
    plan = TemporalSplitter().build(dataset)
    results: list[dict[str, object]] = []

    for spec in candidate_catalog(42):
        if not spec.available:
            results.append({"algorithm": spec.name, "status": "skipped", "reason": spec.skip_reason})
            continue
        fold_rows: list[dict[str, float | int | None]] = []
        dev_y: list[np.ndarray] = []
        dev_p: list[np.ndarray] = []
        for fold in plan.development_folds:
            pipeline = build_pipeline(spec, dataset.X.iloc[fold.train_indices])
            fit_pipeline(
                pipeline,
                dataset.X.iloc[fold.train_indices],
                dataset.y.iloc[fold.train_indices],
                np.ones(len(fold.train_indices), dtype=float),
            )
            probabilities = positive_probabilities(pipeline, dataset.X.iloc[fold.test_indices])
            y = dataset.y.iloc[fold.test_indices].to_numpy(dtype=int)
            dev_y.append(y)
            dev_p.append(probabilities)
            fold_rows.append(classification_metrics(y, probabilities, None))

        combined_y = np.concatenate(dev_y)
        combined_p = np.concatenate(dev_p)
        threshold = threshold_from_development(combined_y, combined_p)
        final_pipeline = build_pipeline(spec, dataset.X.iloc[plan.final_split.train_indices])
        fit_pipeline(
            final_pipeline,
            dataset.X.iloc[plan.final_split.train_indices],
            dataset.y.iloc[plan.final_split.train_indices],
            np.ones(len(plan.final_split.train_indices), dtype=float),
        )
        final_probabilities = positive_probabilities(final_pipeline, dataset.X.iloc[plan.final_split.test_indices])
        final_y = dataset.y.iloc[plan.final_split.test_indices].to_numpy(dtype=int)
        final_metrics = classification_metrics(final_y, final_probabilities, threshold["threshold"] if isinstance(threshold["threshold"], float) else None)
        aps = [float(item["average_precision"]) for item in fold_rows if item["average_precision"] is not None]
        robust_ap = (
            0.5 * float(final_metrics["average_precision"] or 0.0)
            + 0.3 * float(np.mean(aps) if aps else 0.0)
            + 0.2 * float(np.min(aps) if aps else 0.0)
        )
        results.append(
            {
                "algorithm": spec.name,
                "status": "ok",
                "development_threshold": threshold,
                "development_folds": fold_rows,
                "final": final_metrics,
                "robust_ap_score": robust_ap,
            }
        )

    ranked = sorted(
        (item for item in results if item.get("status") == "ok"),
        key=lambda item: float(item.get("robust_ap_score") or 0.0),
        reverse=True,
    )
    payload = {
        "data_hash": data_hash,
        "rows": len(dataset),
        "positives": int(dataset.y.sum()),
        "positive_rate": float(dataset.y.mean()),
        "features": list(dataset.feature_names),
        "early_stage": plan.early_stage,
        "winner": ranked[0]["algorithm"] if ranked else None,
        "ranking": ranked,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
