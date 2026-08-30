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
from backend.app.ml.models import candidate_catalog
from backend.app.ml.trainer import ModelTrainer, TrainerConfig
from backend.app.services.training import TrainingService


def evaluate(dataset, names):
    trainer = ModelTrainer(TrainerConfig())
    plan = trainer.splitter.build(dataset)
    specs = {spec.name: spec for spec in candidate_catalog(trainer.config.random_state)}
    out = {}
    for name in names:
        spec = specs[name]
        try:
            result = trainer._evaluate_development_candidate(
                dataset, plan, spec, len(dataset.feature_names)
            )
            out[name] = {
                "status": "ok",
                "threshold": result.threshold,
                "trade_count": result.development_metrics.trade_count,
                "precision": result.development_metrics.precision,
                "profit_units": result.development_metrics.profit_units,
            }
        except Exception as exc:
            out[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    return out


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
    current_features = service.configured_feature_selection()
    current = FeatureBuilder(FeaturePolicy(feature_allowlist=current_features)).prepare(frame)

    legacy_frame = frame.copy()
    liquidity = np.asarray(legacy_frame["liquidity_usd"], dtype=float)
    legacy_frame["ln(liquidity_usd)"] = np.where(liquidity > 0, np.log(liquidity), np.nan)
    legacy_features = tuple(current_features) + ("ln(liquidity_usd)",)
    with_absolute_liquidity = FeatureBuilder(
        FeaturePolicy(feature_allowlist=legacy_features)
    ).prepare(legacy_frame)

    names = ("logistic_regression", "catboost", "lightgbm", "flaml_automl")
    print(json.dumps({
        "rows": len(current),
        "positive_rate": float(current.y.mean()),
        "without_ln_liquidity": evaluate(current, names),
        "with_ln_liquidity": evaluate(with_absolute_liquidity, names),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
