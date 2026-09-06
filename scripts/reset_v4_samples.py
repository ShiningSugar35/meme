from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = PROJECT_ROOT / "data" / "meme_quant.db"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
MARKER_KEY = "v4_sample_reset_completed"


def _runtime_state(connection: sqlite3.Connection, key: str) -> Any:
    row = connection.execute("SELECT value_json FROM runtime_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _put_runtime_state(connection: sqlite3.Connection, key: str, value: Any) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO runtime_state(key,value_json,updated_at)
        VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at
        """,
        (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), now),
    )


def _verify_database(path: Path, expected_samples: int | None = None) -> dict[str, Any]:
    connection = sqlite3.connect(path, timeout=60.0)
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "samples" not in tables:
            raise RuntimeError("backup_missing_samples_table")
        samples = int(connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0])
        if expected_samples is not None and samples != int(expected_samples):
            raise RuntimeError(f"backup_sample_count_mismatch:{samples}!={expected_samples}")
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check.lower() != "ok":
            raise RuntimeError(f"backup_quick_check_failed:{quick_check}")
        fk = connection.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise RuntimeError(f"backup_foreign_key_violations:{len(fk)}")
        return {"samples": samples, "quick_check": quick_check, "foreign_key_violations": 0}
    finally:
        connection.close()


def _make_backup(source_path: Path) -> tuple[Path, dict[str, Any]]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_path = BACKUP_DIR / f"meme_quant_before_v4_sample_reset_{stamp}.db"
    partial_path = final_path.with_suffix(final_path.suffix + ".partial")
    partial_path.unlink(missing_ok=True)

    source = sqlite3.connect(source_path, timeout=60.0)
    destination = sqlite3.connect(partial_path, timeout=60.0)
    try:
        source_samples = int(source.execute("SELECT COUNT(*) FROM samples").fetchone()[0])
        source.backup(destination, pages=4096, sleep=0.02)
        destination.commit()
    finally:
        destination.close()
        source.close()

    try:
        verification = _verify_database(partial_path, expected_samples=source_samples)
        partial_path.replace(final_path)
        return final_path, verification
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise


def _reset_active_samples(database_path: Path, backup_path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(database_path, timeout=60.0, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        existing_marker = _runtime_state(connection, MARKER_KEY)
        if existing_marker:
            raise RuntimeError("v4_sample_reset_already_completed")

        before = {
            "samples": int(connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0]),
            "predictions": int(connection.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]),
            "positions": int(connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0]),
            "trades": int(connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0]),
            "open_positions": int(connection.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]),
            "position_sample_refs": int(connection.execute("SELECT COUNT(*) FROM positions WHERE sample_id IS NOT NULL").fetchone()[0]),
            "position_prediction_refs": int(connection.execute("SELECT COUNT(*) FROM positions WHERE prediction_id IS NOT NULL").fetchone()[0]),
        }
        if before["open_positions"] != 0:
            raise RuntimeError(f"open_positions_block_reset:{before['open_positions']}")

        connection.execute("BEGIN IMMEDIATE")
        try:
            # Preserve immutable closed-position/trade accounting while removing
            # active sample-derived identity links. predictions then disappear by
            # samples ON DELETE CASCADE; adaptive feedback cascades from predictions.
            connection.execute(
                "UPDATE positions SET sample_id=NULL,prediction_id=NULL "
                "WHERE sample_id IS NOT NULL OR prediction_id IS NOT NULL"
            )
            connection.execute("DELETE FROM samples")
            in_transaction = {
                "samples": int(connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0]),
                "predictions": int(connection.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]),
                "positions": int(connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0]),
                "trades": int(connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0]),
                "position_sample_refs": int(connection.execute("SELECT COUNT(*) FROM positions WHERE sample_id IS NOT NULL").fetchone()[0]),
                "position_prediction_refs": int(connection.execute("SELECT COUNT(*) FROM positions WHERE prediction_id IS NOT NULL").fetchone()[0]),
            }
            if in_transaction["samples"] != 0 or in_transaction["predictions"] != 0:
                raise RuntimeError("active_sample_reset_not_empty")
            if in_transaction["positions"] != before["positions"] or in_transaction["trades"] != before["trades"]:
                raise RuntimeError("historical_accounting_count_changed")
            if in_transaction["position_sample_refs"] != 0 or in_transaction["position_prediction_refs"] != 0:
                raise RuntimeError("historical_position_links_not_cleared")
            fk = connection.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                raise RuntimeError(f"post_reset_foreign_key_violations:{len(fk)}")
            marker = {
                "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "backup": str(backup_path.relative_to(PROJECT_ROOT)),
                "before": before,
                "transaction_zero_point": in_transaction,
            }
            _put_runtime_state(connection, MARKER_KEY, marker)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        fk_count = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        after_commit = {
            "samples": int(connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0]),
            "predictions": int(connection.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]),
            "positions": int(connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0]),
            "trades": int(connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0]),
            "quick_check": quick_check,
            "foreign_key_violations": fk_count,
        }
        if quick_check.lower() != "ok" or fk_count:
            raise RuntimeError("post_reset_database_integrity_failed")
        return {
            "backup": str(backup_path.relative_to(PROJECT_ROOT)),
            "before": before,
            "transaction_zero_point": in_transaction,
            "after_commit": after_commit,
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"status": "dry", "database": str(DATABASE_PATH.relative_to(PROJECT_ROOT))}))
        return 0

    # Never reset an already-reset database, even if a second backup could be made.
    guard = sqlite3.connect(DATABASE_PATH, timeout=30.0)
    try:
        if _runtime_state(guard, MARKER_KEY):
            raise RuntimeError("v4_sample_reset_already_completed")
    finally:
        guard.close()

    backup_path, backup_verification = _make_backup(DATABASE_PATH)
    result = _reset_active_samples(DATABASE_PATH, backup_path)
    result["backup_verification"] = backup_verification
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
