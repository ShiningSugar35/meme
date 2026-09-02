from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.collector.discovery_experiment import DiscoveryExperimentManager
from backend.app.config import get_settings
from backend.app.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Show the current/selected 24h discovery experiment status.")
    parser.add_argument("--experiment-id", default=None)
    args = parser.parse_args()
    settings = get_settings()
    database = Database(settings.database_path)
    database.initialize()
    manager = DiscoveryExperimentManager(database)
    experiment_id = args.experiment_id
    if experiment_id is None and manager.current() is None:
        latest = database.fetch_one(
            "SELECT id FROM discovery_experiments ORDER BY started_at DESC LIMIT 1"
        )
        experiment_id = str(latest["id"]) if latest else None
    print(json.dumps(manager.summary(experiment_id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
