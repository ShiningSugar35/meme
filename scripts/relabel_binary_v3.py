from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.collector.constants import LabelPolicy
from backend.app.config import get_settings
from backend.app.database import utc_now_iso
from backend.app.services.csv_importer import LEGACY_LABEL_VERSION


def backup_database(source: Path) -> Path:
    backup_dir = PROJECT_ROOT / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"meme_quant_pre_binary_v3_{stamp}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    return target


def main() -> int:
    settings = get_settings()
    path = settings.database_path
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    backup = backup_database(path)
    policy = LabelPolicy()
    now = utc_now_iso()

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute(
            "SELECT COUNT(*), SUM(tag=1), SUM(tag=2) FROM samples WHERE label_status='mature'"
        ).fetchone()

        connection.execute(
            """
            UPDATE samples
            SET tag=CASE WHEN tag=2 THEN 0 ELSE tag END,
                gross_return_rate=CASE WHEN tag=1 THEN 0.60 ELSE -0.10 END,
                terminal_return_estimated=0,
                return_source=CASE
                    WHEN label_source='legacy_csv_migration' AND tag=1 THEN 'legacy_label_rule'
                    WHEN label_source='legacy_csv_migration' THEN 'legacy_binary_rule'
                    WHEN tag=1 THEN 'first_touch_binary_v3'
                    ELSE 'binary_timeout_or_stop'
                END,
                exit_reason=CASE
                    WHEN label_source='legacy_csv_migration' AND tag=1 THEN 'legacy_take_profit'
                    WHEN label_source='legacy_csv_migration' THEN 'legacy_negative_or_timeout'
                    WHEN tag=1 THEN 'take_profit_first'
                    WHEN first_stop_loss_at IS NOT NULL
                         AND (first_take_profit_at IS NULL OR first_stop_loss_at<=first_take_profit_at)
                        THEN 'stop_loss_first'
                    ELSE 'window_timeout_negative'
                END,
                label_version=CASE
                    WHEN label_source='legacy_csv_migration' THEN ?
                    ELSE ?
                END,
                updated_at=?
            WHERE label_status='mature' AND tag IS NOT NULL
            """,
            (LEGACY_LABEL_VERSION, policy.label_version, now),
        )
        connection.execute(
            """
            UPDATE samples
            SET label_version=?, updated_at=?
            WHERE label_source='collector' AND label_status='pending'
            """,
            (policy.label_version, now),
        )
        connection.execute(
            """
            UPDATE models
            SET status='rejected',
                rejection_reason='invalidated_by_binary_label_v3'
            WHERE status IN ('champion','candidate','retired')
            """
        )
        connection.execute(
            "DELETE FROM runtime_state WHERE key IN ('model_health_status','model_health_worker_status')"
        )
        connection.execute(
            """
            INSERT INTO audit_logs(category,action,severity,entity_type,entity_id,details_json,created_at)
            VALUES('labeling','binary_v3_relabel','warning','dataset','samples',?,?)
            """,
            (
                '{"policy":"sl090_tp160_h2_binary_v3","old_tag2_folded_into_tag0":true}',
                now,
            ),
        )
        connection.commit()
        after = connection.execute(
            "SELECT COUNT(*), SUM(tag=1), SUM(tag=2) FROM samples WHERE label_status='mature'"
        ).fetchone()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(f"backup={backup}")
    print(f"before mature={before[0]} tag1={before[1] or 0} tag2={before[2] or 0}")
    print(f"after mature={after[0]} tag1={after[1] or 0} tag2={after[2] or 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
