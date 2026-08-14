from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import Database
from backend.app.ml.features import DEFAULT_MODEL_TRAINING_FEATURES, FeatureBuilder, FeaturePolicy
from backend.app.ml.trainer import ModelTrainer, TrainerConfig
from backend.app.repositories.samples import SampleRepository
from backend.app.services.training import TrainingService


def main() -> int:
    database = Database(PROJECT_ROOT / "data" / "meme_quant.db")
    rows = SampleRepository(database).list_mature()
    frame, data_hash = TrainingService(database)._training_frame(rows)
    dataset = FeatureBuilder(
        FeaturePolicy(feature_allowlist=DEFAULT_MODEL_TRAINING_FEATURES)
    ).prepare(frame)
    result = ModelTrainer(TrainerConfig()).train(dataset)
    candidates = []
    for item in result.candidates:
        candidates.append(
            {
                "algorithm": item.algorithm,
                "status": item.status,
                "skip_reason": item.skip_reason,
                "feature_count": len(item.feature_names),
                "feature_names": list(item.feature_names),
                "threshold": item.threshold,
                "economic_score": item.economic_score,
                "generalization": asdict(item.generalization) if item.generalization else None,
                "composite_score": item.composite_score,
                "score_standard_error": item.score_standard_error,
                "development": asdict(item.development_metrics) if item.development_metrics else None,
                "final": asdict(item.final_metrics) if item.final_metrics else None,
            }
        )
    output = {
        "data_hash": data_hash,
        "rows": len(dataset),
        "positives": int(dataset.y.sum()),
        "positive_rate": float(dataset.y.mean()),
        "feature_pool_count": len(dataset.feature_names),
        "top3": list(result.top_algorithms),
        "rule_baseline_final": asdict(result.rule_baseline),
        "candidates": sorted(
            candidates,
            key=lambda item: (
                item["status"] != "ok",
                -(float(item["composite_score"]) if item["composite_score"] is not None else -999.0),
                item["algorithm"],
            ),
        ),
        "formula": {
            "fixed_payoff_units": "5*TP-FP",
            "precision_recall_identity": "N_positive*recall*(6-1/precision)",
            "economic": "mean_clip((5*TP-FP)/(5*N_positive),-1,1)",
            "generalization": "0.60*AP_skill_mean + 0.20*stability + 0.20*decay",
            "composite": "0.60*economic + 0.40*generalization",
            "occam": "adaptive feature-count search; fold-train-only ranking; smallest subset within one standard error of algorithm best",
        },
        "warnings": list(result.warnings),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
