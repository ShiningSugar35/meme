from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import Database
from backend.app.services.training import TrainingService


def main() -> int:
    parser = argparse.ArgumentParser(description="Show active Top-3 models and recent training runs.")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    database = Database(PROJECT_ROOT / "data" / "meme_quant.db")
    database.initialize()
    service = TrainingService(database)
    active = service.models.active_models()
    if not active:
        print("top3=none")
    for model in active:
        print(
            json.dumps(
                {
                    "slot": model.get("active_slot"),
                    "id": model["id"],
                    "algorithm": model["algorithm"],
                    "features": len(model.get("feature_names") or []),
                    "threshold": model.get("active_threshold") or model.get("thresholds", {}).get("decision"),
                    "composite_score": model.get("active_composite_score") or model.get("metrics", {}).get("composite_score"),
                    "early_stage": model["early_stage"],
                },
                ensure_ascii=False,
            )
        )
    for run in service.list_runs(limit=max(1, args.limit)):
        summary = run.get("summary") or {}
        print(
            json.dumps(
                {
                    "id": run["id"],
                    "trigger": run["trigger"],
                    "status": run["status"],
                    "retry_count": run.get("retry_count", 0),
                    "scheduled_for": run.get("scheduled_for"),
                    "requested_features": (run.get("request") or {}).get("feature_names", []),
                    "top_models": [item.get("algorithm") for item in summary.get("top_models", [])],
                    "top3_updated": run.get("promoted"),
                    "error": run.get("error_message"),
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
