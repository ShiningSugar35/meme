from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.collector import (
    ApiKeyRoles,
    AsyncRateLimiter,
    CollectedSample,
    CollectorEndpoints,
    GMGNDataClient,
    GMGNEnrichmentProvider,
    HttpxTransport,
    LabelFinalizer,
    LabelPolicy,
)
from backend.app.config import PROJECT_ROOT, get_settings
from backend.app.database import Database, utc_now_iso

MIGRATION_KEY = "h1_no_completed_migration_v1"


def _env_values() -> dict[str, str]:
    values = dotenv_values(PROJECT_ROOT / ".env")
    return {str(key): str(value) for key, value in values.items() if value not in (None, "")}


def _backup_database(path: Path) -> Path:
    backup_dir = PROJECT_ROOT / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"meme_quant_pre_h1_no_completed_{stamp}.db"
    source = sqlite3.connect(path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _build_provider() -> tuple[GMGNEnrichmentProvider, HttpxTransport]:
    env = _env_values()
    secrets = [env.get(f"GMGN_API_KEY_{index}", "") for index in range(1, 13)]
    roles = ApiKeyRoles.from_secrets(secrets)
    endpoints = CollectorEndpoints(
        trenches=env.get("GMGN_TRENCHES_PATH", "/v1/trenches"),
        token_info=env.get("GMGN_TOKEN_INFO_PATH", "/v1/token/info"),
        token_security=env.get("GMGN_TOKEN_SECURITY_PATH", "/v1/token/security"),
        token_pool_info=env.get("GMGN_TOKEN_POOL_INFO_PATH", "/v1/token/pool_info"),
        top_holders=env.get("GMGN_TOKEN_HOLDERS_PATH", "/v1/market/token_top_holders"),
        kline=env.get("GMGN_KLINE_PATH", "/v1/market/token_kline"),
        trending=env.get("GMGN_TRENDING_PATH", "/v1/market/rank"),
        created_tokens=env.get("GMGN_PORTFOLIO_CREATED_TOKENS_PATH", "/v1/user/created_tokens"),
    )
    transport = HttpxTransport()
    client = GMGNDataClient(
        base_url=env.get("GMGN_API_BASE_URL", ""),
        transport=transport,
        limiter=AsyncRateLimiter(2.0),
        endpoints=endpoints,
    )
    return GMGNEnrichmentProvider(client, roles), transport


def _prepare(database: Database) -> dict[str, Any]:
    settings = get_settings()
    backup = _backup_database(Path(settings.database_path))
    database.initialize()
    policy = LabelPolicy()
    now = utc_now_iso()

    with database.transaction(immediate=True) as connection:
        before = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN token_type='completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN token_type IN ('new_creation','near_completion') AND label_status='mature' AND tag=0 THEN 1 ELSE 0 END) AS negatives,
                   SUM(CASE WHEN token_type IN ('new_creation','near_completion') AND label_status='mature' AND tag=1 THEN 1 ELSE 0 END) AS positives
            FROM samples
            """
        ).fetchone()

        completed_prediction_ids = connection.execute(
            """
            SELECT p.id
            FROM predictions p
            JOIN samples s ON s.id=p.sample_id
            WHERE s.token_type='completed'
            """
        ).fetchall()
        prediction_ids = [int(row[0]) for row in completed_prediction_ids]
        if prediction_ids:
            placeholders = ",".join("?" for _ in prediction_ids)
            connection.execute(
                f"UPDATE positions SET prediction_id=NULL WHERE prediction_id IN ({placeholders})",
                tuple(prediction_ids),
            )
        connection.execute(
            """
            UPDATE positions SET sample_id=NULL
            WHERE sample_id IN (SELECT id FROM samples WHERE token_type='completed')
            """
        )
        removed_completed = connection.execute(
            "DELETE FROM samples WHERE token_type='completed'"
        ).rowcount

        inferred_negatives = connection.execute(
            """
            UPDATE samples
            SET label_version=?, label_source='h1_migration_monotonic_negative',
                return_source='h1_migration_monotonic_negative',
                exit_reason='h1_negative_inferred_from_h2_negative',
                first_take_profit_at=NULL, first_stop_loss_at=NULL, same_bar_conflict=0,
                gross_return_rate=-0.10, terminal_return_estimated=0,
                price_1h_max_ratio=NULL, price_1h_min_ratio=NULL, final_1h_close_ratio=NULL,
                updated_at=?
            WHERE token_type IN ('new_creation','near_completion')
              AND label_status='mature' AND tag=0
            """,
            (policy.label_version, now),
        ).rowcount

        positives_to_refetch = connection.execute(
            """
            UPDATE samples
            SET tag=NULL, label_status='pending', label_version=?,
                label_source='h1_migration_pending', return_source=NULL,
                exit_reason=NULL, first_take_profit_at=NULL, first_stop_loss_at=NULL,
                same_bar_conflict=0, gross_return_rate=NULL,
                price_1h_max_ratio=NULL, price_1h_min_ratio=NULL, final_1h_close_ratio=NULL,
                terminal_return_estimated=0, updated_at=?
            WHERE token_type IN ('new_creation','near_completion')
              AND label_status='mature' AND tag=1
            """,
            (policy.label_version, now),
        ).rowcount

        connection.execute(
            """
            UPDATE samples
            SET label_version=?, price_1h_max_ratio=NULL, price_1h_min_ratio=NULL,
                final_1h_close_ratio=NULL, updated_at=?
            WHERE token_type IN ('new_creation','near_completion')
              AND label_status='pending'
              AND label_source NOT IN ('h1_migration_pending','h1_migration_retry')
            """,
            (policy.label_version, now),
        )

    state = {
        "status": "in_progress",
        "prepared_at": now,
        "backup_path": str(backup.relative_to(PROJECT_ROOT)),
        "before_total": int(before["total"] or 0),
        "before_completed": int(before["completed"] or 0),
        "before_negatives": int(before["negatives"] or 0),
        "before_positives": int(before["positives"] or 0),
        "removed_completed": int(removed_completed),
        "detached_completed_predictions": len(prediction_ids),
        "inferred_h1_negatives": int(inferred_negatives),
        "positives_to_refetch": int(positives_to_refetch),
        "refetched": 0,
        "refetched_h1_positive": 0,
        "refetched_h1_negative": 0,
        "refetch_errors": 0,
        "label_version": policy.label_version,
    }
    database.set_runtime_state(MIGRATION_KEY, state)
    database.audit(
        category="data_migration",
        action="h1_no_completed_prepared",
        severity="warning",
        details=state,
    )
    return state


def _row_to_sample(row: dict[str, Any]) -> CollectedSample:
    try:
        features = json.loads(row.get("features_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        features = {}
    try:
        source = json.loads(row.get("raw_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        source = {}
    return CollectedSample(
        address=str(row["address"]),
        token_type=str(row.get("token_type") or "unknown"),
        entry_time=int(row["entry_time"]),
        entry_price=float(row["entry_price"]),
        launchpad=str(row.get("launchpad") or "unknown"),
        liquidity=float(row.get("liquidity") or 0.0),
        features=features if isinstance(features, dict) else {},
        source=source if isinstance(source, dict) else {},
    )


def _save_result(database: Database, sample_id: int, result: Any) -> None:
    row = database.fetch_one("SELECT features_json FROM samples WHERE id=?", (sample_id,)) or {}
    try:
        features = json.loads(row.get("features_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        features = {}
    if not isinstance(features, dict):
        features = {}
    if features.get("price_change_1h") is None and result.price_change_1h is not None:
        features["price_change_1h"] = result.price_change_1h
    if features.get("price_change_5m") is None and result.price_change_5m is not None:
        features["price_change_5m"] = result.price_change_5m
    database.execute(
        """
        UPDATE samples
        SET tag=?, price_1h_max_ratio=?, price_1h_min_ratio=?, final_1h_close_ratio=?,
            first_take_profit_at=?, first_stop_loss_at=?, exit_reason=?, same_bar_conflict=?,
            gross_return_rate=?, return_source='h1_migration_gmgn',
            label_status='mature', label_version=?, label_source='h1_migration_gmgn',
            terminal_return_estimated=0, features_json=?, updated_at=?
        WHERE id=? AND token_type IN ('new_creation','near_completion')
        """,
        (
            int(result.tag),
            float(result.max_price_ratio),
            float(result.min_price_ratio),
            float(result.final_close_ratio),
            result.first_take_profit_at,
            result.first_stop_loss_at,
            str(result.exit_reason),
            int(result.first_take_profit_at is not None and result.first_take_profit_at == result.first_stop_loss_at),
            0.60 if int(result.tag) == 1 else -0.10,
            str(result.label_version),
            json.dumps(features, ensure_ascii=False, separators=(",", ":")),
            utc_now_iso(),
            sample_id,
        ),
    )


async def _refetch_batch(database: Database, limit: int) -> dict[str, Any]:
    policy = LabelPolicy()
    finalizer = LabelFinalizer(policy)
    rows = database.fetch_all(
        """
        SELECT * FROM samples
        WHERE token_type IN ('new_creation','near_completion')
          AND label_status='pending'
          AND label_source IN ('h1_migration_pending','h1_migration_retry')
        ORDER BY CASE label_source WHEN 'h1_migration_pending' THEN 0 ELSE 1 END,
                 entry_time, id
        LIMIT ?
        """,
        (limit,),
    )
    if not rows:
        return {"attempted": 0, "positive": 0, "negative": 0, "errors": 0}

    provider, transport = _build_provider()
    positive = negative = errors = 0
    try:
        for index, row in enumerate(rows, start=1):
            sample = _row_to_sample(row)
            try:
                klines = await provider.klines(
                    sample.address,
                    sample.entry_time - policy.history_seconds,
                    sample.entry_time + policy.window_seconds,
                )
                result = finalizer.finalize(sample, klines)
                _save_result(database, int(row["id"]), result)
                positive += int(result.tag == 1)
                negative += int(result.tag == 0)
                if index % 10 == 0 or index == len(rows):
                    print(
                        f"refetch progress {index}/{len(rows)}; "
                        f"h1_positive={positive}; h1_negative={negative}; errors={errors}",
                        flush=True,
                    )
            except Exception as exc:
                errors += 1
                database.execute(
                    """
                    UPDATE samples SET label_source='h1_migration_retry', updated_at=?
                    WHERE id=? AND label_status='pending'
                    """,
                    (utc_now_iso(), int(row["id"])),
                )
                database.audit(
                    category="data_migration",
                    action="h1_positive_refetch_failed",
                    severity="warning",
                    entity_type="sample",
                    entity_id=str(row["id"]),
                    details={"error": f"{type(exc).__name__}: {exc}"[:500]},
                )
    finally:
        await transport.close()
    return {
        "attempted": len(rows),
        "positive": positive,
        "negative": negative,
        "errors": errors,
    }


def _finish_state(database: Database, batch: dict[str, Any]) -> dict[str, Any]:
    state = database.get_runtime_state(MIGRATION_KEY, {})
    if not isinstance(state, dict):
        state = {}
    state["refetched"] = int(state.get("refetched") or 0) + int(batch["positive"]) + int(batch["negative"])
    state["refetched_h1_positive"] = int(state.get("refetched_h1_positive") or 0) + int(batch["positive"])
    state["refetched_h1_negative"] = int(state.get("refetched_h1_negative") or 0) + int(batch["negative"])
    state["refetch_errors"] = int(state.get("refetch_errors") or 0) + int(batch["errors"])
    remaining = int((database.fetch_one(
        """
        SELECT COUNT(*) AS count FROM samples
        WHERE token_type IN ('new_creation','near_completion')
          AND label_status='pending'
          AND label_source IN ('h1_migration_pending','h1_migration_retry')
        """
    ) or {"count": 0})["count"])
    completed = int((database.fetch_one(
        "SELECT COUNT(*) AS count FROM samples WHERE token_type='completed'"
    ) or {"count": 0})["count"])
    state["remaining_positive_refetch"] = remaining
    state["completed_rows_remaining"] = completed
    state["updated_at"] = utc_now_iso()
    if remaining == 0 and completed == 0:
        state["status"] = "completed"
        state["completed_at"] = state["updated_at"]
    else:
        state["status"] = "in_progress"
    database.set_runtime_state(MIGRATION_KEY, state)
    database.audit(
        category="data_migration",
        action="h1_no_completed_batch",
        details={**batch, "remaining_positive_refetch": remaining, "status": state["status"]},
    )
    return state


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-refetch", type=int, default=100)
    parser.add_argument("--force-prepare", action="store_true")
    args = parser.parse_args()

    database = Database()
    state = database.get_runtime_state(MIGRATION_KEY, {})
    if args.force_prepare or not isinstance(state, dict) or not state.get("prepared_at"):
        state = _prepare(database)
    else:
        database.initialize()

    if state.get("status") == "completed":
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return

    batch = await _refetch_batch(database, max(1, int(args.max_refetch)))
    state = _finish_state(database, batch)
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
