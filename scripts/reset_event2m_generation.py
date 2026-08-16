from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.collector.constants import FEATURE_SCHEMA_VERSION
from backend.app.database import Database
from backend.app.services.training import TrainingService


def main() -> int:
    db_path = PROJECT_ROOT / "data" / "meme_quant.db"
    backup_dir = PROJECT_ROOT / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"meme_quant_pre_event2m_{stamp}.db"

    database = Database(db_path)
    database.initialize()

    source = sqlite3.connect(db_path, timeout=30)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    current = database.fetch_one(
        "SELECT COUNT(*) AS n FROM samples WHERE feature_schema_version=?",
        (FEATURE_SCHEMA_VERSION,),
    ) or {}
    current_count = int(current.get("n") or 0)
    linked = database.fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM positions p JOIN samples s ON s.id=p.sample_id
        WHERE s.feature_schema_version=?
        """,
        (FEATURE_SCHEMA_VERSION,),
    ) or {}
    linked_positions = int(linked.get("n") or 0)
    if linked_positions:
        raise RuntimeError(
            f"Refusing to reset {FEATURE_SCHEMA_VERSION}: {linked_positions} linked positions already exist"
        )

    with database.transaction(immediate=True) as connection:
        connection.execute(
            "DELETE FROM samples WHERE feature_schema_version=?",
            (FEATURE_SCHEMA_VERSION,),
        )
        connection.execute("DELETE FROM adaptive_policy_feedback")
        connection.execute("DELETE FROM adaptive_policy_decisions")
        connection.execute("DELETE FROM market_regime_snapshots")
        connection.execute("DELETE FROM collector_cycle_snapshots")
        for key in (
            "adaptive_policy_ready",
            "adaptive_exploration_ready",
            "adaptive_policy_evidence",
            "gmgn_market_regime_feed",
            "market_regime_worker_status",
            TrainingService.FEATURE_SELECTION_STATE_KEY,
        ):
            connection.execute("DELETE FROM runtime_state WHERE key=?", (key,))

    database.set_runtime_state("adaptive_policy_ready", False)
    database.set_runtime_state("adaptive_exploration_ready", False)
    report = {
        "backup": str(backup_path.relative_to(PROJECT_ROOT)),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "pre_reset_current_generation_samples": current_count,
        "post_reset_current_generation_samples": int(
            (database.fetch_one(
                "SELECT COUNT(*) AS n FROM samples WHERE feature_schema_version=?",
                (FEATURE_SCHEMA_VERSION,),
            ) or {}).get("n") or 0
        ),
        "legacy_samples_preserved": int(
            (database.fetch_one(
                "SELECT COUNT(*) AS n FROM samples WHERE feature_schema_version<>?",
                (FEATURE_SCHEMA_VERSION,),
            ) or {}).get("n") or 0
        ),
        "historical_audit_preserved": True,
        "adaptive_policy_ready": False,
        "adaptive_exploration_ready": False,
    }
    artifact = PROJECT_ROOT / "artifacts" / "event2m_generation_reset.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
