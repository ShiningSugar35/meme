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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one persisted Top-3 meme model training job.")
    parser.add_argument(
        "--feature",
        action="append",
        dest="features",
        help="Select one candidate feature. Repeat to choose multiple features.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database = Database(PROJECT_ROOT / "data" / "meme_quant.db")
    database.initialize()
    service = TrainingService(database)
    run_id = service.create_run("manual", feature_names=args.features or None)
    service.run(run_id)
    run = database.fetch_one("SELECT * FROM training_runs WHERE id=?", (run_id,)) or {}
    print(f"run_id={run_id}")
    print(f"status={run.get('status')}")
    print(f"top3_updated={bool(run.get('promoted'))}")
    print(f"error={run.get('error_message') or ''}")
    try:
        summary = json.loads(run.get("summary_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        summary = {}
    top_models = summary.get("top_models") or []
    print(f"rule_baseline={json.dumps(summary.get('rule_baseline_final') or {}, ensure_ascii=False)}")
    for model in top_models:
        print(
            "top_model="
            + json.dumps(
                {
                    "rank": model.get("rank"),
                    "id": model.get("id"),
                    "algorithm": model.get("algorithm"),
                    "feature_count": len(model.get("feature_names") or []),
                    "threshold": model.get("threshold"),
                    "economic_score": model.get("economic_score"),
                    "generalization_score": model.get("generalization_score"),
                    "composite_score": model.get("composite_score"),
                },
                ensure_ascii=False,
            )
        )
    return 0 if run.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
