from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.collector.constants import FEATURE_SCHEMA_VERSION, LabelPolicy
from backend.app.config import get_settings
from backend.app.database import Database
from backend.app.ml.features import FeatureBuilder, FeaturePolicy
from backend.app.ml.models import candidate_catalog
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
        "feature_count": len(dataset.feature_names),
        "features": list(dataset.feature_names),
        "algorithms": {},
    }
    for name in ("logistic_regression", "catboost", "lightgbm", "flaml_automl"):
        spec = specs[name]
        sizes = trainer._feature_subset_sizes(len(dataset.feature_names), spec)
        probes = sorted({sizes[0], sizes[len(sizes) // 2], sizes[-1]}) if sizes else []
        details = []
        for size in probes:
            try:
                result = trainer._evaluate_development_candidate(dataset, plan, spec, size)
                details.append({
                    "feature_count": size,
                    "status": "ok",
                    "threshold": result.threshold,
                    "trade_count": result.development_metrics.trade_count if result.development_metrics else None,
                    "precision": result.development_metrics.precision if result.development_metrics else None,
                    "profit_units": result.development_metrics.profit_units if result.development_metrics else None,
                    "features": list(result.feature_names),
                })
            except Exception as exc:
                details.append({
                    "feature_count": size,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })
        output["algorithms"][name] = details
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
