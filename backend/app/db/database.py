import aiosqlite
import asyncio
from pathlib import Path
from ..config import settings
from ..logging_config import logger

DB_PATH = Path(settings.SQLITE_PATH)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


async def init_db(db_path: str | None = None):
    """Initialize and return an aiosqlite connection.

    If db_path is None, uses configured settings.SQLITE_PATH.
    """
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Initializing SQLite DB", path=str(path))
    db = await aiosqlite.connect(str(path))
    # return rows as mapping for convenience
    import sqlite3
    db.row_factory = sqlite3.Row
    # Apply pragmas
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA synchronous=NORMAL;")
    await db.execute("PRAGMA foreign_keys=ON;")
    await db.execute("PRAGMA busy_timeout=5000;")
    await db.commit()
    # load schema
    from importlib import resources
    
    try:
        schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
        await db.executescript(schema)
        await db.commit()
    except Exception as e:
        logger.error("Failed to initialize schema", error=str(e))
    
    # Run migrations for existing databases
    await _run_migrations(db)
    
    return db


async def _run_migrations(db):
    """Run database migrations for existing databases."""
    try:
        # Check and add runtime_settings table
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_settings'"
        )
        if not await cursor.fetchone():
            logger.info("Creating runtime_settings table")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS runtime_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL DEFAULT 'system'
                )
            """)
            await db.commit()

        # Add account_type to system_events if missing
        await _add_column_if_missing(db, 'system_events', 'account_type', "TEXT NOT NULL DEFAULT 'SIM'")
        await _add_index_if_missing(db, 'idx_system_events_level', 'system_events', 'level')
        await _add_index_if_missing(db, 'idx_system_events_category', 'system_events', 'category')
        await _add_index_if_missing(db, 'idx_system_events_account', 'system_events', 'account_type')

        # Add account_type to trade_events if missing
        await _add_column_if_missing(db, 'trade_events', 'account_type', "TEXT NOT NULL DEFAULT 'SIM'")
        await _add_index_if_missing(db, 'idx_trade_events_account', 'trade_events', 'account_type, created_at')
        await _add_index_if_missing(db, 'idx_trade_events_token', 'trade_events', 'token_mint, created_at')

        # Add source_mode to token_metric_snapshots if missing
        await _add_column_if_missing(db, 'token_metric_snapshots', 'source_mode', "TEXT NOT NULL DEFAULT 'MOCK'")

        # Positions migration - add account_type, legacy_config_status, updated_at, next_check_at, pnl_pct
        await _add_column_if_missing(db, 'positions', 'account_type', "TEXT NOT NULL DEFAULT 'SIM'")
        await _add_column_if_missing(db, 'positions', 'legacy_config_status', 'TEXT')
        await _add_column_if_missing(db, 'positions', 'updated_at', 'TEXT')
        await _add_column_if_missing(db, 'positions', 'next_check_at', 'TEXT')
        await _add_column_if_missing(db, 'positions', 'last_checked_at', 'TEXT')
        await _add_column_if_missing(db, 'positions', 'pnl_pct', 'REAL')
        await _add_index_if_missing(db, 'idx_positions_account', 'positions', 'account_type, status')
        await _add_index_if_missing(db, 'idx_positions_token', 'positions', 'token_mint, account_type')
        await _add_index_if_missing(db, 'idx_positions_next_check', 'positions', 'next_check_at, status')
        await _add_index_if_missing(db, 'idx_positions_updated', 'positions', 'updated_at')

        # Backfill account_type for existing positions (is_live=1 -> LIVE, else SIM)
        cursor = await db.execute(
            "SELECT COUNT(*) as c FROM positions WHERE account_type = 'SIM' AND is_live = 1"
        )
        row = await cursor.fetchone()
        if row and row[0] > 0:
            logger.info(f"Backfilling account_type for {row[0]} live positions")
            await db.execute("UPDATE positions SET account_type = 'LIVE' WHERE is_live = 1 AND account_type = 'SIM'")
            await db.commit()

        # Backfill account_type for existing trade_events (is_live=1 -> LIVE, else SIM)
        cursor = await db.execute(
            "SELECT COUNT(*) as c FROM trade_events WHERE account_type = 'SIM' AND is_live = 1"
        )
        row = await cursor.fetchone()
        if row and row[0] > 0:
            logger.info(f"Backfilling account_type for {row[0]} live trade events")
            await db.execute("UPDATE trade_events SET account_type = 'LIVE' WHERE is_live = 1 AND account_type = 'SIM'")
            await db.commit()

        # Mark legacy positions with invalid locked_strategy_config_json
        cursor = await db.execute(
            "SELECT id, locked_strategy_config_json FROM positions WHERE legacy_config_status IS NULL AND locked_strategy_config_json IS NOT NULL AND locked_strategy_config_json != ''"
        )
        rows = await cursor.fetchall()
        import json
        bad_count = 0
        for row in rows:
            pos_id = row[0]
            config = row[1]
            if not config:
                continue
            try:
                json.loads(config)
                await db.execute(
                    "UPDATE positions SET legacy_config_status = 'VALID' WHERE id = ?",
                    (pos_id,)
                )
            except (json.JSONDecodeError, TypeError):
                await db.execute(
                    "UPDATE positions SET legacy_config_status = 'LEGACY_INVALID_CONFIG' WHERE id = ?",
                    (pos_id,)
                )
                bad_count += 1
        if bad_count > 0 or rows:
            await db.commit()
            logger.info(f"Marked {bad_count} legacy positions as LEGACY_INVALID_CONFIG")

        # Check and add discovery_events table
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='discovery_events'"
        )
        if not await cursor.fetchone():
            logger.info("Creating discovery_events table")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS discovery_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_mint TEXT NOT NULL,
                    pool_address TEXT,
                    first_seen_at TEXT NOT NULL,
                    pool_created_at TEXT,
                    t_seconds INTEGER,
                    status TEXT NOT NULL DEFAULT 'DISCOVERED',
                    source_snapshot_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_discovery_events_token ON discovery_events(token_mint, status)")
            await db.commit()

        # Check and add discovery_event_id to positions
        cursor = await db.execute("PRAGMA table_info(positions)")
        columns = await cursor.fetchall()
        column_names = [col[1] for col in columns]

        if 'discovery_event_id' not in column_names:
            logger.info("Adding discovery_event_id to positions table")
            await db.execute("ALTER TABLE positions ADD COLUMN discovery_event_id INTEGER")
            await db.commit()

        if 'discovery_event_id' not in column_names:
            logger.info("Adding discovery_event_id to token_strategy_matches table")
            cursor = await db.execute("PRAGMA table_info(token_strategy_matches)")
            columns = await cursor.fetchall()
            column_names2 = [col[1] for col in columns]
            if 'discovery_event_id' not in column_names2:
                await db.execute("ALTER TABLE token_strategy_matches ADD COLUMN discovery_event_id INTEGER")
                await db.commit()

        if 'discovery_event_id' not in column_names:
            logger.info("Adding discovery_event_id to bandit_observations table")
            cursor = await db.execute("PRAGMA table_info(bandit_observations)")
            columns = await cursor.fetchall()
            column_names3 = [col[1] for col in columns]
            if 'discovery_event_id' not in column_names3:
                await db.execute("ALTER TABLE bandit_observations ADD COLUMN discovery_event_id INTEGER")
                await db.commit()

    except Exception as e:
        logger.error("Migration failed", error=str(e))


async def _add_column_if_missing(db, table: str, column: str, col_def: str):
    """Add a column to a table if it doesn't already exist."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    column_names = [col[1] for col in rows]
    if column not in column_names:
        logger.info(f"Adding {column} to {table}")
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


async def _add_index_if_missing(db, index_name: str, table: str, columns: str):
    """Add an index if it doesn't already exist."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
    )
    if not await cursor.fetchone():
        logger.info(f"Creating index {index_name} on {table}({columns})")
        try:
            await db.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({columns})")
        except Exception as e:
            logger.warning(f"Failed to create index {index_name}: {e}")


def get_db_sync():
    # helper for synchronous contexts if needed
    return aiosqlite.connect(str(DB_PATH))
