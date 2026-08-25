from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.collector.constants import FEATURE_SCHEMA_VERSION, LabelPolicy
from backend.app.collector.labels import LabelFinalizer
from backend.app.collector.models import CollectedSample, Kline
from backend.app.config import get_settings
from backend.app.database import utc_now_iso

CACHE = PROJECT_ROOT / "artifacts" / "research" / "kline_cache_v3_2h.json.gz"


def backup_database(source: Path) -> Path:
    target_dir = PROJECT_ROOT / "data" / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"meme_quant_pre_phase16_v5_relabel_{stamp}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    return target


def _load_cache() -> dict[str, dict]:
    if not CACHE.exists():
        raise RuntimeError(f"Kline cache missing: {CACHE}")
    with gzip.open(CACHE, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    items = payload.get("items")
    if not isinstance(items, dict):
        raise RuntimeError("Kline cache has no items mapping")
    return items


def _klines(item: dict) -> list[Kline]:
    rows: list[Kline] = []
    for bar in item.get("bars") or ():
        if not isinstance(bar, (list, tuple)) or len(bar) < 5:
            continue
        rows.append(
            Kline(
                timestamp=int(bar[0]),
                open=float(bar[1]) if bar[1] is not None else None,
                high=float(bar[2]) if bar[2] is not None else None,
                low=float(bar[3]) if bar[3] is not None else None,
                close=float(bar[4]) if bar[4] is not None else None,
            )
        )
    return rows


def _snapshot(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return connection.execute(
        """
        SELECT id,address,token_type,entry_time,entry_price,launchpad,liquidity,
               label_status,tag,label_version
        FROM samples
        WHERE feature_schema_version=?
          AND token_type IN ('new_creation','near_completion')
          AND label_status='mature' AND tag IN (0,1)
        ORDER BY entry_time,id
        """,
        (FEATURE_SCHEMA_VERSION,),
    ).fetchall()


def _validate(connection: sqlite3.Connection, expected_mature: int, items: dict[str, dict]) -> dict[str, object]:
    rows = _snapshot(connection)
    current_ids = {str(row["id"]) for row in rows}
    violations = connection.execute(
        """
        SELECT COUNT(*) FROM samples
        WHERE feature_schema_version=?
          AND token_type IN ('new_creation','near_completion')
          AND label_status='mature' AND tag IN (0,1)
          AND label_version<>?
        """,
        (FEATURE_SCHEMA_VERSION, LabelPolicy().label_version),
    ).fetchone()[0]
    tag_violations = connection.execute(
        """
        SELECT COUNT(*) FROM samples
        WHERE feature_schema_version=?
          AND token_type IN ('new_creation','near_completion')
          AND label_status='mature' AND (tag NOT IN (0,1) OR tag IS NULL)
        """,
        (FEATURE_SCHEMA_VERSION,),
    ).fetchone()[0]
    generic_violations = connection.execute(
        """
        SELECT COUNT(*) FROM samples
        WHERE feature_schema_version=?
          AND token_type IN ('new_creation','near_completion')
          AND label_status='mature' AND tag IN (0,1)
          AND (label_max_price_ratio IS NULL OR label_min_price_ratio IS NULL
               OR label_final_close_ratio IS NULL OR label_window_seconds<>?)
        """,
        (FEATURE_SCHEMA_VERSION, LabelPolicy().window_seconds),
    ).fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    return {
        "mature_rows": len(rows),
        "row_count_matches_snapshot": len(rows) == expected_mature,
        "label_version_violations": int(violations),
        "tag_violations": int(tag_violations),
        "generic_barrier_violations": int(generic_violations),
        "kline_covered": sum(1 for sample_id in current_ids if sample_id in items),
        "kline_expected": len(current_ids),
        "kline_coverage": (sum(1 for sample_id in current_ids if sample_id in items) / len(current_ids)) if current_ids else 1.0,
        "foreign_key_violations": len(foreign_keys),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase16 v5 production relabel using a complete frozen 2h Kline cache")
    parser.add_argument("--apply", action="store_true", help="Apply to production DB after all prechecks")
    args = parser.parse_args()
    settings = get_settings()
    db_path = Path(settings.database_path)
    items = _load_cache()
    policy = LabelPolicy()
    finalizer = LabelFinalizer(policy)

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        rows = _snapshot(connection)
        before_total = int(connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0])
        missing = [int(row["id"]) for row in rows if str(row["id"]) not in items]
        precheck = {
            "current_generation_mature": len(rows),
            "cache_items": len(items),
            "missing_kline_ids": missing,
            "coverage": (len(rows) - len(missing)) / len(rows) if rows else 1.0,
        }
        print(json.dumps({"precheck": precheck}, ensure_ascii=False), flush=True)
        if missing:
            raise RuntimeError(f"Kline coverage is not 100%; missing={len(missing)}")
        if not args.apply:
            print(json.dumps({"dry_run": True, "would_relabel": len(rows)}, ensure_ascii=False))
            return 0

        backup = backup_database(db_path)
        now = utc_now_iso()
        results: list[tuple[sqlite3.Row, object]] = []
        for row in rows:
            sample = CollectedSample(
                address=str(row["address"]),
                token_type=str(row["token_type"]),
                entry_time=int(row["entry_time"]),
                entry_price=float(row["entry_price"]),
                launchpad=str(row["launchpad"] or "unknown"),
                liquidity=float(row["liquidity"] or 0.0),
                features={},
            )
            result = finalizer.finalize(sample, _klines(items[str(row["id"])]))
            results.append((row, result))

        connection.execute("BEGIN IMMEDIATE")
        for row, result in results:
            connection.execute(
                """
                UPDATE samples
                SET tag=?,label_max_price_ratio=?,label_min_price_ratio=?,label_final_close_ratio=?,
                    label_window_seconds=?,first_take_profit_at=?,first_stop_loss_at=?,exit_reason=?,
                    same_bar_conflict=?,gross_return_rate=?,return_source='phase16_v5_kline_relabel',
                    label_status='mature',label_version=?,label_source='collector',
                    terminal_return_estimated=0,updated_at=?
                WHERE id=?
                """,
                (
                    result.tag,
                    result.max_price_ratio,
                    result.min_price_ratio,
                    result.final_close_ratio,
                    policy.window_seconds,
                    result.first_take_profit_at,
                    result.first_stop_loss_at,
                    result.exit_reason,
                    int(result.first_take_profit_at is not None and result.first_take_profit_at == result.first_stop_loss_at),
                    policy.take_profit_ratio - 1.0 if result.tag == 1 else policy.stop_loss_ratio - 1.0,
                    policy.label_version,
                    now,
                    int(row["id"]),
                ),
            )
        # Pending current-generation rows keep their status/tag untouched. Only
        # the contract marker is advanced so normal Collector finalization writes
        # the same v5 policy when they reach T+90m.
        connection.execute(
            """
            UPDATE samples SET label_version=?,updated_at=?
            WHERE feature_schema_version=?
              AND token_type IN ('new_creation','near_completion')
              AND label_status='pending'
            """,
            (policy.label_version, now, FEATURE_SCHEMA_VERSION),
        )
        details = {
            "label_version": policy.label_version,
            "stop_loss_ratio": policy.stop_loss_ratio,
            "take_profit_ratio": policy.take_profit_ratio,
            "window_seconds": policy.window_seconds,
            "mature_relabelled": len(results),
            "kline_coverage": 1.0,
            "backup": str(backup),
        }
        connection.execute(
            """
            INSERT INTO audit_logs(category,action,severity,entity_type,entity_id,details_json,created_at)
            VALUES('labeling','phase16_v5_relabel','warning','dataset','samples',?,?)
            """,
            (json.dumps(details, ensure_ascii=False, separators=(",", ":")), now),
        )
        after_total = int(connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0])
        validation = _validate(connection, len(rows), items)
        valid = bool(
            before_total == after_total
            and validation["row_count_matches_snapshot"]
            and validation["label_version_violations"] == 0
            and validation["tag_violations"] == 0
            and validation["generic_barrier_violations"] == 0
            and validation["kline_coverage"] == 1.0
            and validation["foreign_key_violations"] == 0
        )
        if not valid:
            connection.rollback()
            raise RuntimeError(
                f"pre-commit migration validation failed: row_count={before_total}->{after_total}; {validation}"
            )
        connection.commit()
        validation = _validate(connection, len(rows), items)
        positives = int(connection.execute(
            """SELECT COUNT(*) FROM samples WHERE feature_schema_version=? AND token_type IN ('new_creation','near_completion') AND label_status='mature' AND tag=1""",
            (FEATURE_SCHEMA_VERSION,),
        ).fetchone()[0])
        print(json.dumps({
            "applied": True,
            "backup": str(backup),
            "relabelled": len(results),
            "positives": positives,
            "positive_rate": positives / len(results) if results else 0.0,
            "validation": validation,
        }, ensure_ascii=False), flush=True)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
