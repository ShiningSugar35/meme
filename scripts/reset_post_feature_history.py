from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.collector.constants import FEATURE_SCHEMA_VERSION
from backend.app.config import PROJECT_ROOT, get_settings
from backend.app.database import Database, utc_now_iso


MODEL_RUNTIME_PREFIXES = (
    "model_",
    "training_",
    "prediction_",
    "adaptive_",
    "simulation_",
    "paper_",
    "portfolio_strategy:",
    "portfolio_account:",
    "confirmation:",
    "position_monitor_",
    "reconciliation_",
    "liquidation_",
)
MODEL_RUNTIME_KEYS = (
    "last_training_completed_at",
    "last_model_activation_at",
    "last_degraded_training_requested_at",
    "modeling_readiness",
    "collector_events",
    "collector_status",
    "new_entries_paused",
    "new_entries_pause_reason",
    "live_trading_enabled",
    "scheduler_status",
    "sol_usd_price_status",
    "initial_csv_import",
)


def _count(database: Database, table: str) -> int:
    row = database.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") or {"n": 0}
    return int(row.get("n") or 0)


def _delete_model_artifacts(model_dir: Path) -> int:
    removed = 0
    if not model_dir.exists():
        return removed
    for path in model_dir.glob("*.joblib"):
        if path.is_file():
            path.unlink()
            removed += 1
    return removed


def _purge_backup_databases() -> int:
    backup_dir = PROJECT_ROOT / "data" / "backups"
    removed = 0
    if not backup_dir.exists():
        return removed
    for path in backup_dir.iterdir():
        if path.is_file() and (
            path.suffix in {".db", ".sqlite", ".sqlite3", ".wal", ".shm"}
            or path.name.endswith((".db-wal", ".db-shm"))
        ):
            path.unlink()
            removed += 1
    return removed


def reset_history(*, purge_backups: bool, purge_legacy_db: bool) -> dict[str, object]:
    settings = get_settings()
    database = Database(settings.database_path)
    database.initialize()

    before = {
        "samples": _count(database, "samples"),
        "models": _count(database, "models"),
        "active_model_slots": _count(database, "active_model_slots"),
        "predictions": _count(database, "predictions"),
        "simulation_sessions": _count(database, "simulation_sessions"),
        "positions": _count(database, "positions"),
        "trades": _count(database, "trades"),
        "training_runs": _count(database, "training_runs"),
        "collector_cycle_snapshots": _count(database, "collector_cycle_snapshots"),
    }

    with database.transaction(immediate=True) as connection:
        # Dependency order matters because several historical tables reference
        # models, predictions, positions, or samples without ON DELETE CASCADE.
        connection.execute("DELETE FROM adaptive_policy_feedback")
        connection.execute("DELETE FROM adaptive_policy_decisions")
        connection.execute("DELETE FROM trades")
        connection.execute("DELETE FROM positions")
        connection.execute("DELETE FROM simulation_sessions")
        connection.execute("DELETE FROM predictions")
        connection.execute("DELETE FROM active_model_slots")
        connection.execute("DELETE FROM training_runs")
        connection.execute("DELETE FROM models")
        connection.execute("DELETE FROM asset_usd_prices")
        connection.execute("DELETE FROM agent_proposals")
        connection.execute("DELETE FROM collector_cycle_snapshots")
        connection.execute(
            "DELETE FROM samples WHERE feature_schema_version<>?",
            (FEATURE_SCHEMA_VERSION,),
        )
        connection.execute("DELETE FROM audit_logs")

        clauses = ["key=?" for _ in MODEL_RUNTIME_KEYS]
        parameters: list[str] = list(MODEL_RUNTIME_KEYS)
        for prefix in MODEL_RUNTIME_PREFIXES:
            clauses.append("key LIKE ?")
            parameters.append(f"{prefix}%")
        connection.execute(
            f"DELETE FROM runtime_state WHERE {' OR '.join(clauses)}",
            tuple(parameters),
        )

    removed_artifacts = _delete_model_artifacts(settings.model_directory)
    removed_backups = _purge_backup_databases() if purge_backups else 0
    legacy_db = PROJECT_ROOT / "data" / "trading_bot.sqlite3"
    removed_legacy_db = False
    if purge_legacy_db and legacy_db.exists():
        legacy_db.unlink()
        removed_legacy_db = True

    # Explicitly restore a safe clean runtime baseline after the historical purge.
    database.set_runtime_state("live_trading_enabled", False)
    database.set_runtime_state("model_entries_paused_for_rollover", False)
    mature_after = int(
        (
            database.fetch_one(
                """
                SELECT COUNT(*) AS n FROM samples
                WHERE feature_schema_version=? AND label_status='mature' AND tag IN (0,1)
                """,
                (FEATURE_SCHEMA_VERSION,),
            )
            or {"n": 0}
        )["n"]
    )
    ready_after = mature_after >= int(settings.modeling_min_mature_samples)
    database.set_runtime_state(
        "modeling_readiness",
        {
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "mature_samples": mature_after,
            "min_mature_samples": settings.modeling_min_mature_samples,
            "ready": ready_after,
            "mode": "modeling_enabled" if ready_after else "data_collection_rules_only",
            "reason": (
                "post_feature_history_reset_ready"
                if ready_after
                else f"post_feature_history_reset:{mature_after}/{settings.modeling_min_mature_samples}"
            ),
            "updated_at": utc_now_iso(),
        },
    )
    database.audit(
        category="runtime",
        action="post_feature_history_reset",
        severity="warning",
        details={
            "retained_feature_schema_version": FEATURE_SCHEMA_VERSION,
            "removed_model_artifacts": removed_artifacts,
            "purged_backup_databases": removed_backups,
            "removed_unused_legacy_db": removed_legacy_db,
        },
    )

    after = {
        "samples": _count(database, "samples"),
        "models": _count(database, "models"),
        "active_model_slots": _count(database, "active_model_slots"),
        "predictions": _count(database, "predictions"),
        "simulation_sessions": _count(database, "simulation_sessions"),
        "positions": _count(database, "positions"),
        "trades": _count(database, "trades"),
        "training_runs": _count(database, "training_runs"),
        "collector_cycle_snapshots": _count(database, "collector_cycle_snapshots"),
    }
    generations = database.fetch_all(
        """
        SELECT feature_schema_version,label_status,COUNT(*) AS n
        FROM samples
        GROUP BY feature_schema_version,label_status
        ORDER BY feature_schema_version,label_status
        """
    )
    return {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "before": before,
        "after": after,
        "generations": generations,
        "removed_model_artifacts": removed_artifacts,
        "purged_backup_databases": removed_backups,
        "removed_unused_legacy_db": removed_legacy_db,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Purge pre-current-generation samples and all stale model/trading history."
    )
    parser.add_argument(
        "--purge-backups",
        action="store_true",
        help="also delete historical SQLite backup databases under data/backups",
    )
    parser.add_argument(
        "--purge-unused-legacy-db",
        action="store_true",
        help="also delete the unreferenced data/trading_bot.sqlite3 legacy database",
    )
    args = parser.parse_args()
    print(
        reset_history(
            purge_backups=args.purge_backups,
            purge_legacy_db=args.purge_unused_legacy_db,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
