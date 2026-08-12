from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import Database
from backend.app.repositories.models import ModelRepository
from backend.app.repositories.samples import SampleRepository
from backend.app.services.paper_trading import PaperTradingService
from backend.app.services.training import TrainingService


def main() -> int:
    parser = argparse.ArgumentParser(description="Show persisted system/database status.")
    parser.add_argument(
        "--db",
        default="data/meme_quant.db",
        help="Database path relative to the project root, or an absolute path.",
    )
    args = parser.parse_args()
    requested = Path(args.db)
    database_path = requested if requested.is_absolute() else PROJECT_ROOT / requested
    database = Database(database_path)
    database.initialize()
    schema = database.fetch_one("SELECT value FROM schema_meta WHERE key='schema_version'") or {}
    samples = SampleRepository(database).statistics()
    models = ModelRepository(database)
    active_models = models.active_models()
    training = TrainingService(database)
    paper = PaperTradingService(database)
    simulation = paper.simulation_status()
    sessions = paper.simulation_history(limit=5)
    proposal_counts = database.fetch_all(
        "SELECT status,COUNT(*) AS count FROM agent_proposals GROUP BY status ORDER BY status"
    )
    model_counts = database.fetch_all(
        "SELECT status,COUNT(*) AS count FROM models GROUP BY status ORDER BY status"
    )
    payload = {
        "database_path": str(database_path),
        "schema_version": schema.get("value"),
        "samples": {
            "total": samples.get("total"),
            "mature": samples.get("mature"),
            "pending": samples.get("pending"),
            "positives": samples.get("positives"),
        },
        "active_models": [
            {
                "slot": model.get("active_slot"),
                "id": model["id"],
                "algorithm": model["algorithm"],
                "early_stage": model["early_stage"],
                "features": len(model.get("feature_names") or []),
                "threshold": model.get("active_threshold")
                or model.get("thresholds", {}).get("decision"),
                "composite_score": model.get("active_composite_score")
                or model.get("metrics", {}).get("composite_score"),
            }
            for model in active_models
        ],
        "model_counts": model_counts,
        "recent_training_runs": [
            {
                "id": item["id"],
                "trigger": item["trigger"],
                "status": item["status"],
                "top3_updated": item["promoted"],
            }
            for item in training.list_runs(limit=5)
        ],
        "simulation": {
            "session": simulation["session"],
            "history_count": len(sessions),
            "accounts": {
                key: {
                    "cash_usd": value.get("cash_usd"),
                    "invested_usd": value.get("invested_usd"),
                    "open_positions": value.get("open_positions"),
                    "closed_positions": value.get("closed_positions"),
                    "realized_pnl_usd": value.get("realized_pnl_usd"),
                    "total_fees_usd": value.get("total_fees_usd"),
                    "platform_fee_usd": value.get("platform_fee_usd"),
                    "network_fee_usd": value.get("network_fee_usd"),
                    "network_fee_sol": value.get("network_fee_sol"),
                    "slippage_cost_usd": value.get("slippage_cost_usd"),
                }
                for key, value in simulation["accounts"].items()
            },
        },
        "agent_proposals": proposal_counts,
        "runtime": {
            "training_worker": database.get_runtime_state("training_worker_status", {"state": "stopped"}),
            "prediction_worker": database.get_runtime_state("prediction_worker_status", {"state": "stopped"}),
            "scheduler": database.get_runtime_state("scheduler_status", {"state": "stopped"}),
            "model_health": database.get_runtime_state("model_health_status", {"state": "not_evaluated"}),
            "model_health_worker": database.get_runtime_state("model_health_worker_status", {"state": "stopped"}),
            "collector": database.get_runtime_state("collector_status", {"state": "stopped"}),
            "paper_monitor": database.get_runtime_state("paper_monitor_status", {"state": "stopped"}),
            "live_trading_enabled": bool(database.get_runtime_state("live_trading_enabled", False)),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
