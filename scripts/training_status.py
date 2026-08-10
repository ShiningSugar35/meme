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
    parser = argparse.ArgumentParser(description="Show recent persisted model training runs.")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    database = Database(PROJECT_ROOT / "data" / "meme_quant.db")
    database.initialize()
    service = TrainingService(database)
    champion = service.models.champion()
    if champion:
        print(
            f"champion={champion['id']} algorithm={champion['algorithm']} "
            f"features={len(champion.get('feature_names') or [])} early_stage={champion['early_stage']}"
        )
    else:
        print("champion=none")
    for run in service.list_runs(limit=max(1, args.limit)):
        summary = run.get("summary") or {}
        promotion = summary.get("promotion") or {}
        print(
            json.dumps(
                {
                    "id": run["id"],
                    "trigger": run["trigger"],
                    "status": run["status"],
                    "retry_count": run.get("retry_count", 0),
                    "scheduled_for": run.get("scheduled_for"),
                    "features": (run.get("request") or {}).get("feature_names", []),
                    "candidate_model_id": run.get("candidate_model_id"),
                    "promoted": run.get("promoted"),
                    "promotion_eligible": promotion.get("eligible", promotion.get("promotion_eligible")),
                    "promotion_blockers": promotion.get("blockers", []),
                    "incumbent_recipe_rebuilt": promotion.get("incumbent_recipe_rebuilt", False),
                    "error": run.get("error_message"),
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
