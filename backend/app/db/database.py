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
            
        # Check and add discovery_event_id to token_strategy_matches
        cursor = await db.execute("PRAGMA table_info(token_strategy_matches)")
        columns = await cursor.fetchall()
        column_names = [col[1] for col in columns]
        
        if 'discovery_event_id' not in column_names:
            logger.info("Adding discovery_event_id to token_strategy_matches table")
            await db.execute("ALTER TABLE token_strategy_matches ADD COLUMN discovery_event_id INTEGER")
            await db.commit()
        
        # Check and add discovery_event_id to bandit_observations
        cursor = await db.execute("PRAGMA table_info(bandit_observations)")
        columns = await cursor.fetchall()
        column_names = [col[1] for col in columns]
        
        if 'discovery_event_id' not in column_names:
            logger.info("Adding discovery_event_id to bandit_observations table")
            await db.execute("ALTER TABLE bandit_observations ADD COLUMN discovery_event_id INTEGER")
            await db.commit()
        
    except Exception as e:
        logger.error("Migration failed", error=str(e))


def get_db_sync():
    # helper for synchronous contexts if needed
    return aiosqlite.connect(str(DB_PATH))
