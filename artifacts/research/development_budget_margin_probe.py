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
from backend.app.ml.calibration import fit_sigmoid_calibrator
from backend.app.ml.economics import economic_sample_weights, evaluate_probabilities
from backend.app.ml.features import FeatureBuilder, FeaturePolicy
from backend.app.ml.models import build_pipeline, candidate_catalog, fit_pipeline, positive_raw_scores
from backend.app.ml.sparse_budget import SPARSE_BUDGET_FRACTIONS
from backend.app.ml.trainer import ModelTrainer, TrainerConfig
from backend.app.services.training import TrainingService


def main() -> None:
    settings = get_settings()
    db = Database(settings.database_path)
    service = TrainingService(db, settings)
    rows = [
        row for row in service.samples.list_mature()
        if row.get("feature_schema_version") == FEATURE_SCHEMA_VERSION
        and row.get("label_version") == LabelPolicy().label_version
        and row.get("token_type") in {"new_creation", "near_completion"}
    ]
    frame, _ = service._training_frame(rows)
    features = service.configured_feature_selection()
    dataset = FeatureBuilder(FeaturePolicy(feature_allowlist=features)).prepare(frame)
    trainer = ModelTrainer(TrainerConfig())
    plan = trainer.splitter.build(dataset)
    specs = {spec.name: spec for spec in candidate_catalog(trainer.config.random_state)}
    output = {
        "rows": len(dataset),
        "positive_rate": float(dataset.y.mean()),
        "break_even_precision": 0.25,
        "algorithms": {},
    }
    for name in ("logistic_regression", "catboost", "lightgbm", "flaml_automl"):
        spec = specs[name]
        oos_positions = []
        oos_scores = []
        for fold in plan.development_folds:
            order = trainer._rank_features(dataset, fold.train_indices)
            fold_features = tuple(order)
            estimator = build_pipeline(spec, dataset.X.loc[fold.train_indices, fold_features])
            fit_pipeline(
                estimator,
                dataset.X.loc[fold.train_indices, fold_features],
                dataset.y.iloc[fold.train_indices],
                economic_sample_weights(dataset.economic_slice(fold.train_indices)),
            )
            oos_positions.append(fold.test_indices)
            oos_scores.append(positive_raw_scores(estimator, dataset.X.loc[fold.test_indices, fold_features]))
        positions = np.concatenate(oos_positions)
        scores = np.concatenate(oos_scores)
        order_idx = np.argsort(positions, kind="stable")
        positions = positions[order_idx]
        scores = scores[order_idx]
        labels = dataset.y.iloc[positions].to_numpy(dtype=int)
        calibrator = fit_sigmoid_calibrator(
            scores,
            labels,
            source_indices=positions.tolist(),
            source_start=dataset.timestamps.iloc[positions[0]].isoformat(),
            source_end=dataset.timestamps.iloc[positions[-1]].isoformat(),
        )
        probabilities = calibrator.transform(scores)
        ordered = np.sort(probabilities)[::-1]
        budget_rows = []
        for fraction in SPARSE_BUDGET_FRACTIONS:
            k = min(len(probabilities), max(5, int(np.ceil(len(probabilities) * fraction))))
            budget_threshold = float(ordered[k - 1])
            policy_threshold = max(0.25, budget_threshold)
            metrics = evaluate_probabilities(
                labels,
                probabilities,
                policy_threshold,
                dataset.economic_slice(positions),
            )
            budget_only = evaluate_probabilities(
                labels,
                probabilities,
                budget_threshold,
                dataset.economic_slice(positions),
            )
            budget_rows.append({
                "fraction": fraction,
                "budget_threshold": budget_threshold,
                "global_floor_threshold": policy_threshold,
                "max_probability": float(np.max(probabilities)),
                "with_global_floor": {
                    "trade_count": metrics.trade_count,
                    "precision": metrics.precision,
                    "true_positives": metrics.true_positives,
                    "false_positives": metrics.false_positives,
                    "profit_units_3to1": metrics.profit_units,
                },
                "budget_only": {
                    "trade_count": budget_only.trade_count,
                    "precision": budget_only.precision,
                    "recall": budget_only.recall,
                    "true_positives": budget_only.true_positives,
                    "false_positives": budget_only.false_positives,
                    "profit_units_3to1": budget_only.profit_units,
                    "profit_units_4to1": float(4 * budget_only.true_positives - budget_only.false_positives),
                    "profit_units_5to1": float(5 * budget_only.true_positives - budget_only.false_positives),
                },
            })
        output["algorithms"][name] = budget_rows
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
