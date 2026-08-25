from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.config import get_settings
from backend.app.database import Database, utc_now_iso
from backend.app.services.paper_trading import PaperTradingService

INCIDENT_POSITION_IDS = (
    "rules_only-4ff37ddee6cf43cb",
    "rules_only-30795ca0d9114ec6",
)
INCIDENT_REASON = "collector_incident_delayed_exit_20260824"


def main() -> int:
    settings = get_settings()
    db = Database(settings.database_path)
    db.initialize()
    changed: list[str] = []
    excluded_at = utc_now_iso()
    with db.transaction(immediate=True) as connection:
        for position_id in INCIDENT_POSITION_IDS:
            row = connection.execute(
                "SELECT metadata_json FROM positions WHERE id=?", (position_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"incident position missing: {position_id}")
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if metadata.get("performance_excluded") is True:
                continue
            metadata.update(
                {
                    "performance_excluded": True,
                    "performance_exclusion_reason": INCIDENT_REASON,
                    "performance_excluded_at": excluded_at,
                    "performance_exclusion_source": "artifacts/research/collector_incident_20260824.md",
                    "performance_exclusion_preserves_raw_execution": True,
                }
            )
            connection.execute(
                "UPDATE positions SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
            )
            changed.append(position_id)
    for position_id in changed:
        db.audit(
            category="simulation",
            action="incident_position_excluded_from_performance",
            severity="warning",
            entity_type="position",
            entity_id=position_id,
            details={
                "reason": INCIDENT_REASON,
                "raw_position_and_trade_facts_preserved": True,
            },
        )
    db.set_runtime_state(
        "simulation_performance_exclusions",
        {
            "incident": INCIDENT_REASON,
            "position_ids": list(INCIDENT_POSITION_IDS),
            "updated_at": excluded_at,
        },
    )
    service = PaperTradingService(db, settings)
    service.ensure_account("rules_only")
    status = service.simulation_status()
    account = status["accounts"]["rules_only"]
    print(
        json.dumps(
            {
                "changed": changed,
                "preserved": list(INCIDENT_POSITION_IDS),
                "rules_only_cash_usd": account.get("cash_usd"),
                "rules_only_realized_pnl_usd": account.get("realized_pnl_usd"),
                "rules_only_trade_count": account.get("trade_count"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
