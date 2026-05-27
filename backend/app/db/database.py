import aiosqlite
from pathlib import Path

from ..config import settings
from ..logging_config import logger

DB_PATH = Path(settings.SQLITE_PATH)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


async def init_db(db_path: str | None = None):
    """Initialize and return an aiosqlite connection."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Initializing SQLite DB", path=str(path))

    db = await aiosqlite.connect(str(path))

    import sqlite3
    db.row_factory = sqlite3.Row

    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA synchronous=NORMAL;")
    await db.execute("PRAGMA foreign_keys=ON;")
    await db.execute("PRAGMA busy_timeout=5000;")
    await db.commit()

    try:
        schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
        await db.executescript(schema)
        await db.commit()
    except Exception as e:
        logger.error("Failed to initialize schema", error=str(e))
        try:
            await db.rollback()
        except Exception:
            pass
        raise

    await _run_migrations(db)
    return db


async def _run_migrations(db: aiosqlite.Connection):
    """Run additive migrations for existing SQLite databases."""
    try:
        await _ensure_table_exists(db, "runtime_settings", """
            CREATE TABLE IF NOT EXISTS runtime_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              updated_by TEXT NOT NULL DEFAULT 'system'
            )
        """)

        await _ensure_table_exists(db, "system_events", """
            CREATE TABLE IF NOT EXISTS system_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              level TEXT NOT NULL,
              category TEXT NOT NULL,
              message TEXT NOT NULL,
              context_json TEXT,
              account_type TEXT NOT NULL DEFAULT 'SIM',
              created_at TEXT NOT NULL
            )
        """)

        await _ensure_table_exists(db, "trade_events", """
            CREATE TABLE IF NOT EXISTS trade_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              position_id INTEGER,
              token_mint TEXT NOT NULL,
              strategy_id INTEGER,
              is_live INTEGER NOT NULL,
              account_type TEXT NOT NULL DEFAULT 'SIM',
              side TEXT NOT NULL,
              event_type TEXT NOT NULL,
              status TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
        """)

        await _ensure_table_exists(db, "discovery_events", """
            CREATE TABLE IF NOT EXISTS discovery_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              token_mint TEXT NOT NULL,
              pool_address TEXT NOT NULL DEFAULT '',
              first_seen_at TEXT NOT NULL,
              pool_created_at TEXT,
              t_seconds INTEGER,
              status TEXT NOT NULL DEFAULT 'DISCOVERED',
              source_snapshot_id INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
        """)

        await _migrate_system_events(db)
        await _migrate_trade_events(db)
        await _migrate_token_metric_snapshots(db)
        await _migrate_positions(db)
        await _migrate_discovery_events(db)
        await _migrate_token_strategy_matches(db)
        await _migrate_bandit_observations(db)
        await _migrate_tokens(db)
        await _migrate_kline_snapshots(db)
        await _migrate_tick_snapshots(db)
        await _migrate_strategy_groups(db)

        await db.commit()

    except Exception as e:
        logger.error("Migration failed", error=str(e))
        try:
            await db.rollback()
        except Exception:
            pass
        raise


async def _ensure_table_exists(db: aiosqlite.Connection, table: str, ddl: str):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    row = await cursor.fetchone()
    if not row:
        logger.info("Creating missing table", table=table)
        await db.execute(ddl)


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return {row[1] for row in rows}


async def _index_exists(db: aiosqlite.Connection, index_name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    )
    return await cursor.fetchone() is not None


async def _add_column_if_missing(
    db: aiosqlite.Connection,
    table: str,
    column: str,
    col_def: str,
):
    columns = await _table_columns(db, table)
    if column not in columns:
        logger.info("Adding missing column", table=table, column=column)
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


async def _add_index_if_missing(
    db: aiosqlite.Connection,
    index_name: str,
    table: str,
    columns: str,
    where: str | None = None,
    unique: bool = False,
):
    if await _index_exists(db, index_name):
        return

    logger.info("Creating missing index", index=index_name, table=table)
    unique_sql = "UNIQUE " if unique else ""
    where_sql = f" WHERE {where}" if where else ""
    await db.execute(
        f"CREATE {unique_sql}INDEX IF NOT EXISTS {index_name} "
        f"ON {table}({columns}){where_sql}"
    )


async def _rename_column(db: aiosqlite.Connection, table: str, old_name: str, new_name: str):
    """Rename a column using SQLite 3.25+ ALTER TABLE RENAME COLUMN."""
    columns = await _table_columns(db, table)
    if old_name in columns and new_name not in columns:
        logger.info("Renaming column", table=table, old=old_name, new=new_name)
        await db.execute(f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}")


async def _drop_index_if_exists(db: aiosqlite.Connection, index_name: str):
    if await _index_exists(db, index_name):
        logger.info("Dropping obsolete index", index=index_name)
        await db.execute(f"DROP INDEX IF EXISTS {index_name}")


async def _drop_column_if_exists(db: aiosqlite.Connection, table: str, column: str):
    columns = await _table_columns(db, table)
    if column in columns:
        logger.info("Dropping obsolete column", table=table, column=column)
        await db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


async def _migrate_system_events(db: aiosqlite.Connection):
    await _add_column_if_missing(db, "system_events", "account_type", "TEXT NOT NULL DEFAULT 'SIM'")
    await _add_index_if_missing(db, "idx_system_events_level", "system_events", "level")
    await _add_index_if_missing(db, "idx_system_events_category", "system_events", "category")
    await _add_index_if_missing(db, "idx_system_events_account", "system_events", "account_type, created_at")


async def _migrate_trade_events(db: aiosqlite.Connection):
    additions = {
        "position_id": "INTEGER",
        "strategy_id": "INTEGER",
        "account_type": "TEXT NOT NULL DEFAULT 'SIM'",
        "requested_pct": "REAL",
        "requested_sol_amount": "REAL",
        "requested_token_amount": "REAL",
        "executed_sol_amount": "REAL",
        "executed_token_amount": "REAL",
        "price_usd": "REAL",
        "price_sol": "REAL",
        "slippage_bps": "INTEGER",
        "price_impact_pct": "REAL",
        "quote_json": "TEXT",
        "route_plan_json": "TEXT",
        "jito_tip_lamports": "INTEGER",
        "priority_fee_lamports": "INTEGER",
        "tx_signature": "TEXT",
        "bundle_id": "TEXT",
        "error_code": "TEXT",
        "error_message": "TEXT",
        "provider": "TEXT",
        "latency_ms": "INTEGER",
    }
    for column, definition in additions.items():
        await _add_column_if_missing(db, "trade_events", column, definition)

    await _add_index_if_missing(db, "uq_trade_idempotency", "trade_events", "idempotency_key", unique=True)
    await _add_index_if_missing(db, "idx_trade_events_account", "trade_events", "account_type, created_at")
    await _add_index_if_missing(db, "idx_trade_events_token", "trade_events", "token_mint, created_at")
    await _add_index_if_missing(db, "idx_trade_events_position", "trade_events", "position_id, created_at")

    await db.execute(
        "UPDATE trade_events SET account_type = 'LIVE' "
        "WHERE is_live = 1 AND (account_type IS NULL OR account_type = 'SIM')"
    )
    await db.execute(
        "UPDATE trade_events SET account_type = 'SIM' "
        "WHERE is_live = 0 AND account_type IS NULL"
    )


async def _migrate_token_metric_snapshots(db: aiosqlite.Connection):
    await _add_column_if_missing(db, "token_metric_snapshots", "source_mode", "TEXT NOT NULL DEFAULT 'MOCK'")
    await _add_column_if_missing(db, "token_metric_snapshots", "pool_address", "TEXT")
    await _add_column_if_missing(db, "token_metric_snapshots", "platform", "TEXT")
    await _add_column_if_missing(db, "token_metric_snapshots", "launchpad", "TEXT")
    await _add_column_if_missing(db, "token_metric_snapshots", "burn_status", "TEXT")
    await _add_index_if_missing(
        db,
        "idx_token_metric_snapshots_token_time",
        "token_metric_snapshots",
        "token_mint, observed_at",
    )
    await _add_index_if_missing(
        db,
        "idx_token_metric_snapshots_type_time",
        "token_metric_snapshots",
        "type, observed_at",
    )


async def _migrate_positions(db: aiosqlite.Connection):
    additions = {
        "pool_address": "TEXT",
        "discovery_event_id": "INTEGER",
        "account_type": "TEXT NOT NULL DEFAULT 'SIM'",
        "legacy_config_status": "TEXT",
        "updated_at": "TEXT",
        "next_check_at": "TEXT",
        "last_checked_at": "TEXT",
        "pnl_pct": "REAL",
        "last_risk_check_at": "TEXT",
        "next_risk_check_at": "TEXT",
        "risk_check_interval_seconds": "INTEGER",
        "executed_exit_rules_json": "TEXT NOT NULL DEFAULT '[]'",
        "last_exit_reason": "TEXT",
    }
    for column, definition in additions.items():
        await _add_column_if_missing(db, "positions", column, definition)

    await _add_index_if_missing(db, "idx_positions_status", "positions", "status, account_type")
    await _add_index_if_missing(db, "idx_positions_account", "positions", "account_type, status")
    await _add_index_if_missing(db, "idx_positions_token", "positions", "token_mint, account_type")
    await _add_index_if_missing(db, "idx_positions_next_check", "positions", "next_check_at, status")
    await _add_index_if_missing(db, "idx_positions_next_risk_check", "positions", "next_risk_check_at, status, account_type")
    await _add_index_if_missing(db, "idx_positions_updated", "positions", "updated_at")

    await db.execute(
        "UPDATE positions SET account_type = 'LIVE' "
        "WHERE is_live = 1 AND (account_type IS NULL OR account_type = 'SIM')"
    )
    await db.execute(
        "UPDATE positions SET account_type = 'SIM' "
        "WHERE is_live = 0 AND account_type IS NULL"
    )
    await db.execute(
        "UPDATE positions SET updated_at = COALESCE(updated_at, opened_at)"
    )
    await db.execute(
        "UPDATE positions SET executed_exit_rules_json = '[]' "
        "WHERE executed_exit_rules_json IS NULL OR executed_exit_rules_json = ''"
    )

    await _mark_legacy_position_configs(db)


async def _mark_legacy_position_configs(db: aiosqlite.Connection):
    cursor = await db.execute(
        "SELECT id, locked_strategy_config_json "
        "FROM positions "
        "WHERE legacy_config_status IS NULL "
        "AND locked_strategy_config_json IS NOT NULL "
        "AND locked_strategy_config_json != ''"
    )
    rows = await cursor.fetchall()

    import json

    bad_count = 0
    for row in rows:
        pos_id = row[0]
        config_text = row[1]
        try:
            json.loads(config_text)
            await db.execute(
                "UPDATE positions SET legacy_config_status = 'VALID' WHERE id = ?",
                (pos_id,),
            )
        except (json.JSONDecodeError, TypeError):
            await db.execute(
                "UPDATE positions SET legacy_config_status = 'LEGACY_INVALID_CONFIG' WHERE id = ?",
                (pos_id,),
            )
            bad_count += 1

    if rows:
        logger.info("Checked legacy position configs", total=len(rows), bad=bad_count)


async def _migrate_discovery_events(db: aiosqlite.Connection):
    additions = {
        "pool_address": "TEXT NOT NULL DEFAULT ''",
        "strategy_id": "INTEGER",
        "strategy_config_version": "INTEGER",
        "initial_snapshot_id": "INTEGER",
        "recheck_snapshot_id": "INTEGER",
        "initial_match_id": "INTEGER",
        "recheck_match_id": "INTEGER",
        "entry_position_id": "INTEGER",
        "last_error": "TEXT",
        "fail_reason_json": "TEXT",
        "feature_vector_json": "TEXT",
    }
    for column, definition in additions.items():
        await _add_column_if_missing(db, "discovery_events", column, definition)

    await db.execute(
        "UPDATE discovery_events SET pool_address = '' WHERE pool_address IS NULL"
    )

    # Old unique index is too coarse: it prevents one token/pool/snapshot from
    # creating one discovery event per strategy.
    await _drop_index_if_exists(db, "ux_discovery_snapshot_token_pool")

    await _add_index_if_missing(db, "idx_discovery_events_token", "discovery_events", "token_mint, status")
    await _add_index_if_missing(db, "idx_discovery_events_strategy", "discovery_events", "strategy_id, status, updated_at")
    await _add_index_if_missing(db, "idx_discovery_events_token_strategy", "discovery_events", "token_mint, pool_address, strategy_id, status")

    await _add_index_if_missing(
        db,
        "ux_discovery_snapshot_token_pool_strategy",
        "discovery_events",
        "source_snapshot_id, token_mint, pool_address, strategy_id",
        where="source_snapshot_id IS NOT NULL AND strategy_id IS NOT NULL",
        unique=True,
    )


async def _migrate_token_strategy_matches(db: aiosqlite.Connection):
    await _add_column_if_missing(db, "token_strategy_matches", "discovery_event_id", "INTEGER")
    await _add_index_if_missing(
        db,
        "idx_strategy_matches_token_stage",
        "token_strategy_matches",
        "token_mint, stage, created_at",
    )
    await _add_index_if_missing(
        db,
        "idx_strategy_matches_discovery",
        "token_strategy_matches",
        "discovery_event_id, stage",
    )


async def _migrate_bandit_observations(db: aiosqlite.Connection):
    await _add_column_if_missing(db, "bandit_observations", "discovery_event_id", "INTEGER")


async def _migrate_tokens(db: aiosqlite.Connection):
    await _add_index_if_missing(db, "idx_tokens_updated", "tokens", "updated_at")
    await _add_index_if_missing(db, "idx_tokens_type", "tokens", "latest_type")


async def _migrate_kline_snapshots(db: aiosqlite.Connection):
    await _add_index_if_missing(
        db,
        "idx_kline_token_interval_time",
        "kline_snapshots",
        "token_mint, interval, open_time",
    )


async def _migrate_tick_snapshots(db: aiosqlite.Connection):
    await _add_index_if_missing(
        db,
        "idx_tick_token_time",
        "tick_snapshots",
        "token_mint, observed_at",
    )


async def _migrate_strategy_groups(db: aiosqlite.Connection):
    await _rename_column(db, "strategy_groups", "t_seconds", "min_created")
    await _rename_column(db, "discovery_events", "t_seconds", "min_created")
    # Drop columns that were used by the now-removed second_filter runner.
    await _drop_column_if_exists(db, "discovery_events", "next_second_check_at")
    await _drop_column_if_exists(db, "discovery_events", "second_filter_checked_at")
    await _drop_column_if_exists(db, "discovery_events", "second_filter_match_id")
    await _drop_index_if_exists(db, "idx_discovery_events_status_next")


def get_db_sync():
    return aiosqlite.connect(str(DB_PATH))