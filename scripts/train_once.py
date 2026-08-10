from __future__ import annotations

import argparse
import sys
from pathlib import Path
import json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import Database
from backend.app.services.training import TrainingService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one persisted meme model training job.")
    parser.add_argument(
        "--feature",
        action="append",
        dest="features",
        help="Select one model feature. Repeat this option to choose multiple features.",
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
    champion = service.models.champion()
    print(f"run_id={run_id}")
    print(f"status={run.get('status')}")
    print(f"promoted={bool(run.get('promoted'))}")
    print(f"candidate_model_id={run.get('candidate_model_id')}")
    print(f"error={run.get('error_message') or ''}")
    try:
        summary = json.loads(run.get("summary_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        summary = {}
    promotion = summary.get("promotion") or {}
    print(f"promotion={json.dumps(promotion, ensure_ascii=False)}")
    if champion:
        print(f"champion_id={champion['id']}")
        print(f"algorithm={champion['algorithm']}")
        print(f"early_stage={champion['early_stage']}")
        print(f"feature_count={len(champion.get('feature_names') or [])}")
        print(f"thresholds={champion.get('thresholds')}")
    return 0 if run.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
