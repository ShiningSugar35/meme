import aiosqlite
import asyncio
import json
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta

from .database import init_db
from ..logging_config import logger


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_after_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def iso_before_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class Repositories:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        self._owned_db = False
        self._closed = False
        self._write_lock = asyncio.Lock()

    @classmethod
    async def create(cls, db_path: str | None = None):
        db = await init_db(db_path)
        inst = cls(db)
        inst._owned_db = True
        return inst

    async def close(self):
        if getattr(self, "_closed", False):
            return
        try:
            await self.db.close()
        except Exception as e:
            logger.exception("Failed to close DB connection", error=str(e))
        finally:
            self._closed = True

    async def _write_txn(self, coro):
        async with self._write_lock:
            try:
                await self.db.execute("BEGIN IMMEDIATE")
                result = await coro
                await self.db.commit()
                return result
            except Exception:
                try:
                    await self.db.rollback()
                except Exception:
                    pass
                raise

    def _safe_log(self, msg: str, **ctx):
        try:
            logger.error(msg, **ctx)
        except Exception:
            pass

    # runtime_settings

    async def get_runtime_setting(self, key: str) -> Optional[str]:
        async with self.db.execute("SELECT value FROM runtime_settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_runtime_setting(self, key: str, value: str, updated_by: str = "system"):
        updated_at = utc_now_iso()

        async def _do():
            await self.db.execute(
                "INSERT OR REPLACE INTO runtime_settings(key, value, updated_at, updated_by) VALUES(?,?,?,?)",
                (key, value, updated_at, updated_by),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("set_runtime_setting failed", key=key, error=str(e))
            raise

    async def get_all_runtime_settings(self) -> Dict[str, str]:
        async with self.db.execute("SELECT key, value FROM runtime_settings") as cur:
            rows = await cur.fetchall()
        return {row[0]: row[1] for row in rows}

    # system_events

    async def append_system_event(
        self,
        level: str,
        category: str,
        message: str,
        context_json: Optional[str] = None,
        account_type: str = "SIM",
    ):
        created_at = utc_now_iso()

        async def _do():
            await self.db.execute(
                "INSERT INTO system_events(level, category, message, context_json, account_type, created_at) VALUES(?,?,?,?,?,?)",
                (level, category, message, context_json, account_type, created_at),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("append_system_event failed", error=str(e))

    async def list_recent_system_events(
        self,
        limit: int = 100,
        level: Optional[str] = None,
        category: Optional[str] = None,
        account_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT id, level, category, message, context_json, account_type, created_at FROM system_events"
        clauses = []
        params: List[Any] = []

        if level:
            clauses.append("level = ?")
            params.append(level)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if account_type:
            clauses.append("account_type = ?")
            params.append(account_type)

        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        async with self.db.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # provider_requests

    async def append_provider_request(
        self,
        provider: str,
        endpoint: str,
        method: str,
        status_code: Optional[int],
        latency_ms: Optional[int],
        ok: bool,
        error_code: Optional[str],
        error_summary: Optional[str],
        request_summary_json: Optional[str],
        response_summary_json: Optional[str],
    ):
        created_at = utc_now_iso()

        async def _do():
            await self.db.execute(
                "INSERT INTO provider_requests(provider, endpoint, method, status_code, latency_ms, ok, error_code, error_summary, request_summary_json, response_summary_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    provider,
                    endpoint,
                    method,
                    status_code,
                    latency_ms,
                    1 if ok else 0,
                    error_code,
                    error_summary,
                    request_summary_json,
                    response_summary_json,
                    created_at,
                ),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("append_provider_request failed", error=str(e))

    async def list_provider_requests(self, limit: int = 100) -> List[Dict[str, Any]]:
        async with self.db.execute("SELECT * FROM provider_requests ORDER BY id DESC LIMIT ?", (limit,)) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # strategy_groups

    async def create_strategy_group(
        self,
        name: str,
        x: float,
        y: float,
        t_seconds: int,
        is_live: bool = False,
        priority: int = 100,
        raw_config_json: str = "{}",
    ) -> int:
        created_at = utc_now_iso()

        async def _do():
            cur = await self.db.execute(
                "INSERT INTO strategy_groups(name, enabled, is_live, priority, config_version, x, y, t_seconds, raw_config_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    name,
                    1,
                    1 if is_live else 0,
                    priority,
                    1,
                    x,
                    y,
                    t_seconds,
                    raw_config_json,
                    created_at,
                    created_at,
                ),
            )
            return cur.lastrowid

        try:
            return await self._write_txn(_do())
        except Exception as e:
            self._safe_log("create_strategy_group failed", name=name, error=str(e))
            raise

    async def list_strategy_groups(self) -> List[Dict[str, Any]]:
        async with self.db.execute("SELECT * FROM strategy_groups ORDER BY priority ASC, id ASC") as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def get_strategy_group(self, strategy_id: int) -> Optional[Dict[str, Any]]:
        async with self.db.execute("SELECT * FROM strategy_groups WHERE id = ?", (strategy_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_strategy_group(self, id: int, updates: Dict[str, Any]):
        if not updates:
            return

        allowed = {
            "name", "enabled", "is_live", "priority", "config_version",
            "x", "y", "t_seconds",
            "buy_slippage_cap_bps", "sell_slippage_cap_bps",
            "emergency_slippage_cap_bps", "price_impact_hard_cap_pct",
            "raw_config_json",
        }

        cols = []
        params: List[Any] = []

        for k, v in updates.items():
            if k not in allowed:
                raise ValueError(f"Unsupported strategy_groups update column: {k}")
            cols.append(f"{k} = ?")
            params.append(v)

        now = utc_now_iso()
        sql = f"UPDATE strategy_groups SET {', '.join(cols)}, updated_at = ? WHERE id = ?"
        params.extend([now, id])

        async def _do():
            await self.db.execute(sql, tuple(params))

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("update_strategy_group failed", id=id, error=str(e))
            raise


    async def delete_strategy_group(self, id: int):
        """Delete a strategy group from the editable runtime list.

        Historical discovery/match/position rows intentionally keep their numeric
        strategy_id as an audit trail; there are no foreign-key constraints on this
        table in schema.sql.
        """
        async def _do():
            await self.db.execute("DELETE FROM strategy_groups WHERE id = ?", (id,))

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("delete_strategy_group failed", id=id, error=str(e))
            raise

    async def get_enabled_strategy_groups(self) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM strategy_groups WHERE enabled = 1 ORDER BY priority ASC, id ASC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def get_live_strategy_groups(self) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM strategy_groups WHERE enabled = 1 AND is_live = 1 ORDER BY priority ASC, id ASC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def increment_config_version(self, id: int):
        now = utc_now_iso()

        async def _do():
            await self.db.execute(
                "UPDATE strategy_groups SET config_version = config_version + 1, updated_at = ? WHERE id = ?",
                (now, id),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("increment_config_version failed", id=id, error=str(e))
            raise

    async def ensure_default_strategy_groups(self):
        async with self.db.execute("SELECT COUNT(*) as c FROM strategy_groups") as cur:
            row = await cur.fetchone()

        if row and row[0] == 0:
            await self.create_strategy_group(
                "模拟盘1",
                0.15,
                2.25,
                150,
                is_live=False,
                priority=10,
                raw_config_json="{}",
            )
            await self.create_strategy_group(
                "模拟盘2",
                0.20,
                2.75,
                510,
                is_live=False,
                priority=20,
                raw_config_json="{}",
            )

    # tokens

    async def upsert_token_first_seen(
        self,
        token_mint: str,
        chain: str = "solana",
        pool_address: Optional[str] = None,
        launchpad: Optional[str] = None,
        symbol: Optional[str] = None,
        name: Optional[str] = None,
        pool_created_at: Optional[str] = None,
        latest_state: str = "discovered",
    ):
        now = utc_now_iso()

        async def _do():
            await self.db.execute(
                """
                INSERT INTO tokens(
                  token_mint, chain, pool_address, launchpad, symbol, name,
                  pool_created_at, first_seen_at, latest_state, updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(token_mint) DO UPDATE SET
                  chain = COALESCE(excluded.chain, tokens.chain),
                  pool_address = COALESCE(excluded.pool_address, tokens.pool_address),
                  launchpad = COALESCE(excluded.launchpad, tokens.launchpad),
                  symbol = COALESCE(excluded.symbol, tokens.symbol),
                  name = COALESCE(excluded.name, tokens.name),
                  pool_created_at = COALESCE(excluded.pool_created_at, tokens.pool_created_at),
                  latest_state = COALESCE(excluded.latest_state, tokens.latest_state),
                  updated_at = excluded.updated_at
                """,
                (
                    token_mint,
                    chain,
                    pool_address,
                    launchpad,
                    symbol,
                    name,
                    pool_created_at,
                    now,
                    latest_state,
                    now,
                ),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("upsert_token_first_seen failed", token=token_mint, error=str(e))
            raise

    async def update_token_latest_snapshot(
        self,
        token_mint: str,
        latest_snapshot_id: Optional[int] = None,
        latest_price_usd: Optional[float] = None,
        latest_price_sol: Optional[float] = None,
        latest_liquidity_usd: Optional[float] = None,
        latest_sol_side_liquidity: Optional[float] = None,
        latest_market_cap: Optional[float] = None,
        latest_type: Optional[str] = None,
        latest_state: Optional[str] = None,
    ):
        updates = {"updated_at": utc_now_iso()}

        if latest_snapshot_id is not None:
            updates["latest_snapshot_id"] = latest_snapshot_id
        if latest_price_usd is not None:
            updates["latest_price_usd"] = latest_price_usd
        if latest_price_sol is not None:
            updates["latest_price_sol"] = latest_price_sol
        if latest_liquidity_usd is not None:
            updates["latest_liquidity_usd"] = latest_liquidity_usd
        if latest_sol_side_liquidity is not None:
            updates["latest_sol_side_liquidity"] = latest_sol_side_liquidity
        if latest_market_cap is not None:
            updates["latest_market_cap"] = latest_market_cap
        if latest_type is not None:
            updates["latest_type"] = latest_type
        if latest_state is not None:
            updates["latest_state"] = latest_state

        cols = [f"{k} = ?" for k in updates.keys()]
        params = list(updates.values())
        params.append(token_mint)

        async def _do():
            await self.db.execute(
                f"UPDATE tokens SET {', '.join(cols)} WHERE token_mint = ?",
                tuple(params),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("update_token_latest_snapshot failed", token=token_mint, error=str(e))
            raise

    async def get_token(self, token_mint: str) -> Optional[Dict[str, Any]]:
        async with self.db.execute("SELECT * FROM tokens WHERE token_mint = ?", (token_mint,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_tokens(self, limit: int = 100) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM tokens ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # token_metric_snapshots

    async def insert_token_metric_snapshot(
        self,
        token_mint: str,
        observed_at: str,
        raw_json: str,
        **kwargs,
    ) -> int:
        cols = ["token_mint", "observed_at", "raw_json"]
        vals = [token_mint, observed_at, raw_json]

        for k, v in kwargs.items():
            if v is not None:
                cols.append(k)
                vals.append(v)

        placeholders = ",".join(["?"] * len(vals))
        sql = f"INSERT INTO token_metric_snapshots({','.join(cols)}) VALUES({placeholders})"

        async def _do():
            cur = await self.db.execute(sql, tuple(vals))
            return cur.lastrowid

        try:
            return await self._write_txn(_do())
        except Exception as e:
            self._safe_log("insert_token_metric_snapshot failed", token=token_mint, error=str(e))
            raise

    async def get_token_metric_snapshot(self, snapshot_id: int) -> Optional[Dict[str, Any]]:
        async with self.db.execute("SELECT * FROM token_metric_snapshots WHERE id = ?", (snapshot_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_latest_token_metric_snapshot(self, token_mint: str) -> Optional[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM token_metric_snapshots WHERE token_mint = ? ORDER BY observed_at DESC, id DESC LIMIT 1",
            (token_mint,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_token_metric_snapshots(self, token_mint: str, limit: int = 100) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM token_metric_snapshots WHERE token_mint = ? ORDER BY observed_at DESC, id DESC LIMIT ?",
            (token_mint, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # kline_snapshots

    async def insert_kline_snapshots(
        self,
        token_mint: str,
        interval: str,
        open_time: str,
        open_p: float,
        high: float,
        low: float,
        close: float,
        buy_volume: float,
        sell_volume: float,
        volume_usd: float,
        raw_json: str,
    ):
        async def _do():
            await self.db.execute(
                "INSERT INTO kline_snapshots(token_mint, interval, open_time, open, high, low, close, buy_volume, sell_volume, volume_usd, raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    token_mint,
                    interval,
                    open_time,
                    open_p,
                    high,
                    low,
                    close,
                    buy_volume,
                    sell_volume,
                    volume_usd,
                    raw_json,
                ),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("insert_kline_snapshots failed", token=token_mint, error=str(e))
            raise

    async def get_recent_klines(self, token_mint: str, interval: str, limit: int = 10) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM kline_snapshots WHERE token_mint = ? AND interval = ? ORDER BY open_time DESC LIMIT ?",
            (token_mint, interval, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # tick_snapshots

    async def insert_tick_snapshot(
        self,
        token_mint: str,
        source: str,
        observed_at: str,
        price_usd: Optional[float],
        price_sol: Optional[float],
        liquidity_usd: Optional[float],
        sol_side_liquidity: Optional[float],
        market_cap: Optional[float],
        raw_json: Optional[str] = None,
    ):
        async def _do():
            await self.db.execute(
                "INSERT INTO tick_snapshots(token_mint, source, observed_at, price_usd, price_sol, liquidity_usd, sol_side_liquidity, market_cap, raw_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    token_mint,
                    source,
                    observed_at,
                    price_usd,
                    price_sol,
                    liquidity_usd,
                    sol_side_liquidity,
                    market_cap,
                    raw_json,
                ),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("insert_tick_snapshot failed", token=token_mint, error=str(e))
            raise

    async def get_recent_ticks(self, token_mint: str, seconds: int = 60) -> List[Dict[str, Any]]:
        cutoff = iso_before_seconds(seconds)
        async with self.db.execute(
            """
            SELECT *
            FROM tick_snapshots
            WHERE token_mint = ?
              AND observed_at >= ?
            ORDER BY observed_at DESC, id DESC
            """,
            (token_mint, cutoff),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # discovery_events

    def normalize_pool_address(self, pool_address: Optional[str]) -> str:
        return pool_address if pool_address is not None else ""

    async def get_discovery_event_by_snapshot_token_pool(
        self,
        snapshot_id: int,
        token_mint: str,
        pool_address: Optional[str],
        strategy_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        if snapshot_id is None:
            return None

        normalized_pool = self.normalize_pool_address(pool_address)

        if strategy_id is not None:
            sql = """
                SELECT *
                FROM discovery_events
                WHERE source_snapshot_id = ?
                  AND token_mint = ?
                  AND pool_address = ?
                  AND strategy_id = ?
                ORDER BY id DESC
                LIMIT 1
            """
            params = (snapshot_id, token_mint, normalized_pool, strategy_id)
        else:
            sql = """
                SELECT *
                FROM discovery_events
                WHERE source_snapshot_id = ?
                  AND token_mint = ?
                  AND pool_address = ?
                ORDER BY id DESC
                LIMIT 1
            """
            params = (snapshot_id, token_mint, normalized_pool)

        async with self.db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def create_discovery_event_idempotent(
        self,
        token_mint: str,
        pool_address: Optional[str] = None,
        pool_created_at: Optional[str] = None,
        t_seconds: Optional[int] = None,
        snapshot_id: Optional[int] = None,
        strategy_id: Optional[int] = None,
        strategy_config_version: Optional[int] = None,
        status: str = "DISCOVERED",
        next_second_check_at: Optional[str] = None,
        feature_vector_json: Optional[str] = None,
    ) -> tuple[int, bool]:
        if snapshot_id is not None:
            existing = await self.get_discovery_event_by_snapshot_token_pool(
                snapshot_id,
                token_mint,
                pool_address,
                strategy_id=strategy_id,
            )
            if existing:
                return existing["id"], False

        normalized_pool = self.normalize_pool_address(pool_address)
        now = utc_now_iso()

        async def _do():
            cur = await self.db.execute(
                """
                INSERT INTO discovery_events(
                  token_mint, pool_address, strategy_id, strategy_config_version,
                  first_seen_at, pool_created_at, t_seconds, status,
                  source_snapshot_id, initial_snapshot_id,
                  next_second_check_at, feature_vector_json,
                  created_at, updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    token_mint,
                    normalized_pool,
                    strategy_id,
                    strategy_config_version,
                    now,
                    pool_created_at,
                    t_seconds,
                    status,
                    snapshot_id,
                    snapshot_id,
                    next_second_check_at,
                    feature_vector_json,
                    now,
                    now,
                ),
            )
            return cur.lastrowid

        try:
            event_id = await self._write_txn(_do())
            return event_id, True
        except aiosqlite.IntegrityError:
            existing = await self.get_discovery_event_by_snapshot_token_pool(
                snapshot_id,
                token_mint,
                pool_address,
                strategy_id=strategy_id,
            )
            if existing:
                return existing["id"], False
            raise
        except Exception as e:
            self._safe_log("create_discovery_event_idempotent failed", token=token_mint, error=str(e))
            raise

    async def create_discovery_event(
        self,
        token_mint: str,
        pool_address: Optional[str] = None,
        pool_created_at: Optional[str] = None,
        t_seconds: Optional[int] = None,
        source_snapshot_id: Optional[int] = None,
        strategy_id: Optional[int] = None,
        strategy_config_version: Optional[int] = None,
        status: str = "DISCOVERED",
        next_second_check_at: Optional[str] = None,
    ) -> int:
        normalized_pool = self.normalize_pool_address(pool_address)
        now = utc_now_iso()

        async def _do():
            cur = await self.db.execute(
                """
                INSERT INTO discovery_events(
                  token_mint, pool_address, strategy_id, strategy_config_version,
                  first_seen_at, pool_created_at, t_seconds, status,
                  source_snapshot_id, initial_snapshot_id,
                  next_second_check_at, created_at, updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    token_mint,
                    normalized_pool,
                    strategy_id,
                    strategy_config_version,
                    now,
                    pool_created_at,
                    t_seconds,
                    status,
                    source_snapshot_id,
                    source_snapshot_id,
                    next_second_check_at,
                    now,
                    now,
                ),
            )
            return cur.lastrowid

        try:
            return await self._write_txn(_do())
        except Exception as e:
            self._safe_log("create_discovery_event failed", token=token_mint, error=str(e))
            raise

    async def get_discovery_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        async with self.db.execute("SELECT * FROM discovery_events WHERE id = ?", (event_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_latest_discovery_event_for_token(self, token_mint: str) -> Optional[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM discovery_events WHERE token_mint = ? ORDER BY id DESC LIMIT 1",
            (token_mint,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_discovery_event_status(
        self,
        event_id: int,
        status: str,
        **kwargs,
    ):
        allowed = {
            "strategy_id",
            "strategy_config_version",
            "source_snapshot_id",
            "initial_snapshot_id",
            "recheck_snapshot_id",
            "initial_match_id",
            "recheck_match_id",
            "second_filter_match_id",
            "next_second_check_at",
            "second_filter_checked_at",
            "entry_position_id",
            "last_error",
            "fail_reason_json",
            "feature_vector_json",
            "t_seconds",
        }

        updates: Dict[str, Any] = {"status": status, "updated_at": utc_now_iso()}
        for k, v in kwargs.items():
            if k not in allowed:
                raise ValueError(f"Unsupported discovery_events update column: {k}")
            updates[k] = v

        cols = [f"{k} = ?" for k in updates.keys()]
        params = list(updates.values())
        params.append(event_id)

        async def _do():
            await self.db.execute(
                f"UPDATE discovery_events SET {', '.join(cols)} WHERE id = ?",
                tuple(params),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("update_discovery_event_status failed", id=event_id, error=str(e))
            raise

    async def list_pending_second_filter_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        now = utc_now_iso()
        async with self.db.execute(
            """
            SELECT *
            FROM discovery_events
            WHERE status = 'INITIAL_FILTER_PASSED'
              AND next_second_check_at IS NOT NULL
              AND next_second_check_at <= ?
            ORDER BY next_second_check_at ASC, id ASC
            LIMIT ?
            """,
            (now, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_discovery_events(
        self,
        token_mint: Optional[str] = None,
        status: Optional[str] = None,
        strategy_id: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM discovery_events WHERE 1=1"
        params: List[Any] = []

        if token_mint:
            query += " AND token_mint = ?"
            params.append(token_mint)
        if status:
            query += " AND status = ?"
            params.append(status)
        if strategy_id is not None:
            query += " AND strategy_id = ?"
            params.append(strategy_id)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        async with self.db.execute(query, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_token_discovery_events(self, token_mint: str, limit: int = 100) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM discovery_events WHERE token_mint = ? ORDER BY id DESC LIMIT ?",
            (token_mint, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # token_strategy_matches

    async def insert_strategy_match(
        self,
        token_mint: str,
        strategy_id: int,
        strategy_config_version: int,
        snapshot_id: Optional[int],
        stage: str,
        passed: bool,
        pass_fail_detail_json: str,
        feature_vector_json: Optional[str],
        discovery_event_id: Optional[int] = None,
    ) -> int:
        created_at = utc_now_iso()

        async def _do():
            cur = await self.db.execute(
                """
                INSERT INTO token_strategy_matches(
                  token_mint, strategy_id, strategy_config_version, snapshot_id,
                  discovery_event_id, stage, passed,
                  pass_fail_detail_json, feature_vector_json, created_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    token_mint,
                    strategy_id,
                    strategy_config_version,
                    snapshot_id,
                    discovery_event_id,
                    stage,
                    1 if passed else 0,
                    pass_fail_detail_json,
                    feature_vector_json,
                    created_at,
                ),
            )
            return cur.lastrowid

        try:
            return await self._write_txn(_do())
        except Exception as e:
            self._safe_log("insert_strategy_match failed", token=token_mint, error=str(e))
            raise

    async def list_strategy_matches_by_token(self, token_mint: str, limit: int = 50) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM token_strategy_matches WHERE token_mint = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (token_mint, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_token_strategy_matches(self, token_mint: str, limit: int = 100) -> List[Dict[str, Any]]:
        return await self.list_strategy_matches_by_token(token_mint, limit)

    # positions

    async def create_position(
        self,
        token_mint: str,
        is_live: bool,
        locked_strategy_config_json: str,
        status: str,
        entry_price_usd: Optional[float],
        entry_price_sol: Optional[float],
        entry_token_amount: float,
        remaining_token_amount: float,
        remaining_value_usd: Optional[float],
        opened_at: Optional[str] = None,
        live_strategy_id: Optional[int] = None,
        strategy_config_version: int = 1,
        total_cost_sol: float = 0.0,
        open_trade_event_id: Optional[int] = None,
        last_fill_at: Optional[str] = None,
        last_fill_price_usd: Optional[float] = None,
        last_fill_price_sol: Optional[float] = None,
        discovery_event_id: Optional[int] = None,
        account_type: Optional[str] = None,
        legacy_config_status: Optional[str] = None,
        pool_address: Optional[str] = None,
        remaining_value_sol: Optional[float] = None,
        next_risk_check_at: Optional[str] = None,
        risk_check_interval_seconds: Optional[int] = None,
    ) -> int:
        now = utc_now_iso()
        opened_at = opened_at or now
        acct = account_type or ("LIVE" if is_live else "SIM")

        if remaining_value_sol is None and entry_price_sol is not None:
            remaining_value_sol = remaining_token_amount * entry_price_sol

        if last_fill_price_sol is None:
            last_fill_price_sol = entry_price_sol

        if last_fill_at is None:
            last_fill_at = opened_at

        async def _do():
            cur = await self.db.execute(
                """
                INSERT INTO positions(
                  token_mint, pool_address, discovery_event_id,
                  is_live, account_type, live_strategy_id, strategy_config_version,
                  locked_strategy_config_json, legacy_config_status, status,
                  entry_price_usd, entry_price_sol,
                  entry_token_amount, remaining_token_amount,
                  remaining_value_usd, remaining_value_sol,
                  total_cost_sol, opened_at,
                  last_fill_at, last_fill_price_usd, last_fill_price_sol,
                  open_trade_event_id,
                  next_risk_check_at, risk_check_interval_seconds,
                  executed_exit_rules_json,
                  updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    token_mint,
                    pool_address,
                    discovery_event_id,
                    1 if is_live else 0,
                    acct,
                    live_strategy_id,
                    strategy_config_version,
                    locked_strategy_config_json,
                    legacy_config_status,
                    status,
                    entry_price_usd,
                    entry_price_sol,
                    entry_token_amount,
                    remaining_token_amount,
                    remaining_value_usd,
                    remaining_value_sol,
                    total_cost_sol,
                    opened_at,
                    last_fill_at,
                    last_fill_price_usd,
                    last_fill_price_sol,
                    open_trade_event_id,
                    next_risk_check_at,
                    risk_check_interval_seconds,
                    "[]",
                    now,
                ),
            )
            return cur.lastrowid

        try:
            return await self._write_txn(_do())
        except Exception as e:
            self._safe_log("create_position failed", token=token_mint, error=str(e))
            raise

    async def get_position(self, position_id: int) -> Optional[Dict[str, Any]]:
        async with self.db.execute("SELECT * FROM positions WHERE id = ?", (position_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_open_positions(self, account_type: Optional[str] = None) -> List[Dict[str, Any]]:
        base = (
            "SELECT * FROM positions "
            "WHERE status NOT IN ('CLOSED', 'LEGACY_INVALID_CONFIG', 'MIGRATION_NEEDED')"
        )
        params: List[Any] = []

        if account_type:
            base += " AND account_type = ?"
            params.append(account_type)

        base += " ORDER BY opened_at DESC"

        async with self.db.execute(base, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_due_risk_check_positions(
        self,
        account_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        now = utc_now_iso()
        sql = """
            SELECT *
            FROM positions
            WHERE status NOT IN ('CLOSED', 'LEGACY_INVALID_CONFIG', 'MIGRATION_NEEDED')
              AND (
                next_risk_check_at IS NULL
                OR next_risk_check_at <= ?
              )
        """
        params: List[Any] = [now]

        if account_type:
            sql += " AND account_type = ?"
            params.append(account_type)

        sql += " ORDER BY COALESCE(next_risk_check_at, opened_at) ASC, id ASC LIMIT ?"
        params.append(limit)

        async with self.db.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_all_positions(self, limit: int = 100, account_type: Optional[str] = None) -> List[Dict[str, Any]]:
        if account_type:
            async with self.db.execute(
                "SELECT * FROM positions WHERE account_type = ? ORDER BY opened_at DESC LIMIT ?",
                (account_type, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with self.db.execute(
                "SELECT * FROM positions ORDER BY opened_at DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_positions_for_portfolio(self, account_type: str, limit: int = 100) -> List[Dict[str, Any]]:
        async with self.db.execute(
            """
            SELECT
              id, token_mint, status, account_type,
              entry_price_usd, entry_price_sol,
              entry_token_amount, remaining_token_amount,
              remaining_value_usd, remaining_value_sol,
              realized_pnl_sol, realized_pnl_pct, pnl_pct,
              total_cost_sol, total_return_sol,
              opened_at, closed_at, close_reason,
              last_exit_reason,
              updated_at, is_live,
              last_fill_at, last_fill_price_usd, last_fill_price_sol,
              last_risk_check_at, next_risk_check_at, risk_check_interval_seconds,
              executed_exit_rules_json
            FROM positions
            WHERE account_type = ?
            ORDER BY opened_at DESC
            LIMIT ?
            """,
            (account_type, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_positions_summary(self) -> Dict[str, Any]:
        async with self.db.execute(
            "SELECT COUNT(*) as c FROM positions WHERE account_type = 'LIVE' AND status NOT IN ('CLOSED', 'LEGACY_INVALID_CONFIG', 'MIGRATION_NEEDED')"
        ) as cur:
            row = await cur.fetchone()
        live_open = row[0] if row else 0

        async with self.db.execute(
            "SELECT COUNT(*) as c FROM positions WHERE account_type = 'SIM' AND status NOT IN ('CLOSED', 'LEGACY_INVALID_CONFIG', 'MIGRATION_NEEDED')"
        ) as cur:
            row = await cur.fetchone()
        sim_open = row[0] if row else 0

        async with self.db.execute(
            "SELECT COALESCE(SUM(realized_pnl_sol), 0) as total_pnl FROM positions WHERE account_type = 'LIVE' AND status = 'CLOSED'"
        ) as cur:
            row = await cur.fetchone()
        live_pnl = row[0] if row else 0

        async with self.db.execute(
            "SELECT COALESCE(SUM(realized_pnl_sol), 0) as total_pnl FROM positions WHERE account_type = 'SIM' AND status = 'CLOSED'"
        ) as cur:
            row = await cur.fetchone()
        sim_pnl = row[0] if row else 0

        one_hour_ago = iso_before_seconds(3600)

        async with self.db.execute(
            "SELECT COUNT(*) as c FROM discovery_events WHERE created_at > ?",
            (one_hour_ago,),
        ) as cur:
            row = await cur.fetchone()
        events = row[0] if row else 0

        async with self.db.execute(
            "SELECT COUNT(*) as c FROM system_events WHERE level = 'ERROR' AND created_at > ?",
            (one_hour_ago,),
        ) as cur:
            row = await cur.fetchone()
        errors = row[0] if row else 0

        return {
            "live_open_count": live_open,
            "sim_open_count": sim_open,
            "live_pnl_sol": live_pnl,
            "sim_pnl_sol": sim_pnl,
            "recent_discoveries": events,
            "recent_errors": errors,
        }

    async def update_position_remaining(
        self,
        position_id: int,
        remaining_token_amount: float,
        remaining_value_usd: Optional[float] = None,
        remaining_value_sol: Optional[float] = None,
        last_fill_at: Optional[str] = None,
        last_fill_price_usd: Optional[float] = None,
        last_fill_price_sol: Optional[float] = None,
        total_return_sol: Optional[float] = None,
        realized_pnl_sol: Optional[float] = None,
        realized_pnl_pct: Optional[float] = None,
        pnl_pct: Optional[float] = None,
    ):
        updates: Dict[str, Any] = {
            "remaining_token_amount": remaining_token_amount,
            "updated_at": utc_now_iso(),
        }

        if remaining_value_usd is not None:
            updates["remaining_value_usd"] = remaining_value_usd
        if remaining_value_sol is not None:
            updates["remaining_value_sol"] = remaining_value_sol
        if last_fill_at is not None:
            updates["last_fill_at"] = last_fill_at
        if last_fill_price_usd is not None:
            updates["last_fill_price_usd"] = last_fill_price_usd
        if last_fill_price_sol is not None:
            updates["last_fill_price_sol"] = last_fill_price_sol
        if total_return_sol is not None:
            updates["total_return_sol"] = total_return_sol
        if realized_pnl_sol is not None:
            updates["realized_pnl_sol"] = realized_pnl_sol
        if realized_pnl_pct is not None:
            updates["realized_pnl_pct"] = realized_pnl_pct
        if pnl_pct is not None:
            updates["pnl_pct"] = pnl_pct

        cols = [f"{k} = ?" for k in updates.keys()]
        params = list(updates.values())
        params.append(position_id)

        async def _do():
            await self.db.execute(
                f"UPDATE positions SET {', '.join(cols)} WHERE id = ?",
                tuple(params),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("update_position_remaining failed", position_id=position_id, error=str(e))
            raise

    async def update_position_risk_schedule(
        self,
        position_id: int,
        remaining_value_sol: Optional[float],
        interval_seconds: int,
        last_risk_check_at: Optional[str] = None,
        next_risk_check_at: Optional[str] = None,
    ):
        now = utc_now_iso()
        last_at = last_risk_check_at or now
        next_at = next_risk_check_at or iso_after_seconds(interval_seconds)

        async def _do():
            await self.db.execute(
                """
                UPDATE positions
                SET remaining_value_sol = COALESCE(?, remaining_value_sol),
                    last_risk_check_at = ?,
                    next_risk_check_at = ?,
                    risk_check_interval_seconds = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    remaining_value_sol,
                    last_at,
                    next_at,
                    interval_seconds,
                    now,
                    position_id,
                ),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("update_position_risk_schedule failed", position_id=position_id, error=str(e))
            raise

    async def close_position(
        self,
        position_id: int,
        closed_at: Optional[str] = None,
        close_reason: Optional[str] = None,
        total_return_sol: Optional[float] = None,
        realized_pnl_sol: Optional[float] = None,
        realized_pnl_pct: Optional[float] = None,
        pnl_pct: Optional[float] = None,
    ):
        closed_at = closed_at or utc_now_iso()

        updates: Dict[str, Any] = {
            "status": "CLOSED",
            "closed_at": closed_at,
            "close_reason": close_reason,
            "last_exit_reason": close_reason,
            "updated_at": closed_at,
        }

        if total_return_sol is not None:
            updates["total_return_sol"] = total_return_sol
        if realized_pnl_sol is not None:
            updates["realized_pnl_sol"] = realized_pnl_sol
        if realized_pnl_pct is not None:
            updates["realized_pnl_pct"] = realized_pnl_pct
        if pnl_pct is not None:
            updates["pnl_pct"] = pnl_pct

        cols = [f"{k} = ?" for k in updates.keys()]
        params = list(updates.values())
        params.append(position_id)

        async def _do():
            await self.db.execute(
                f"UPDATE positions SET {', '.join(cols)} WHERE id = ?",
                tuple(params),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("close_position failed", position_id=position_id, error=str(e))
            raise

    async def mark_position_legacy_config(self, position_id: int, status: str):
        now = utc_now_iso()

        async def _do():
            await self.db.execute(
                "UPDATE positions SET legacy_config_status = ?, updated_at = ? WHERE id = ?",
                (status, now, position_id),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("mark_position_legacy_config failed", position_id=position_id, error=str(e))

    async def get_executed_exit_rules(self, position_id: int) -> set[str]:
        position = await self.get_position(position_id)
        if not position:
            return set()

        raw = position.get("executed_exit_rules_json") or "[]"
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return {str(x) for x in data}
        except Exception:
            pass
        return set()

    async def has_exit_rule_executed(self, position_id: int, rule_code: str) -> bool:
        rules = await self.get_executed_exit_rules(position_id)
        return rule_code in rules

    async def mark_exit_rule_executed(self, position_id: int, rule_code: str):
        rules = await self.get_executed_exit_rules(position_id)
        rules.add(rule_code)
        encoded = json.dumps(sorted(rules), ensure_ascii=False)
        now = utc_now_iso()

        async def _do():
            await self.db.execute(
                "UPDATE positions SET executed_exit_rules_json = ?, updated_at = ? WHERE id = ?",
                (encoded, now, position_id),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("mark_exit_rule_executed failed", position_id=position_id, rule=rule_code, error=str(e))
            raise

    async def list_recent_closed_live_positions(self, limit: int = 10) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM positions WHERE account_type = 'LIVE' AND status = 'CLOSED' ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_open_live_position_by_token_and_cycle(
        self,
        token_mint: str,
        discovery_event_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        return await self.get_open_position_by_token_and_cycle(
            token_mint=token_mint,
            account_type="LIVE",
            discovery_event_id=discovery_event_id,
        )

    async def get_open_position_by_token_and_cycle(
        self,
        token_mint: str,
        account_type: Optional[str] = None,
        discovery_event_id: Optional[int] = None,
        strategy_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        sql = """
            SELECT *
            FROM positions
            WHERE token_mint = ?
              AND status NOT IN ('CLOSED', 'LEGACY_INVALID_CONFIG', 'MIGRATION_NEEDED')
        """
        params: List[Any] = [token_mint]

        if account_type:
            sql += " AND account_type = ?"
            params.append(account_type)

        if discovery_event_id is not None:
            sql += " AND discovery_event_id = ?"
            params.append(discovery_event_id)

        if strategy_id is not None:
            sql += " AND live_strategy_id = ?"
            params.append(strategy_id)

        sql += " ORDER BY id DESC LIMIT 1"

        async with self.db.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_open_live_position_by_token(self, token_mint: str) -> Optional[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM positions WHERE token_mint = ? AND is_live = 1 AND status != 'CLOSED' ORDER BY id DESC LIMIT 1",
            (token_mint,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_positions_by_token(self, token_mint: str, limit: int = 100) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM positions WHERE token_mint = ? ORDER BY id DESC LIMIT ?",
            (token_mint, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_positions_by_token_and_is_live(
        self,
        token_mint: str,
        is_live: bool,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM positions WHERE token_mint = ? AND is_live = ? ORDER BY id DESC LIMIT ?",
            (token_mint, 1 if is_live else 0, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # trade_events

    async def append_trade_event(self, idempotency_key: str, **kwargs) -> Dict[str, Any]:
        created_at = utc_now_iso()

        async with self.db.execute(
            "SELECT * FROM trade_events WHERE idempotency_key = ?",
            (idempotency_key,),
        ) as cur:
            row = await cur.fetchone()
        if row:
            return dict(row)

        token_mint = kwargs.get("token_mint")
        side = kwargs.get("side", "UNKNOWN")
        event_type = kwargs.get("event_type", "UNKNOWN")
        status = kwargs.get("status", "PENDING")
        is_live = kwargs.get("is_live", 0)
        account_type_val = kwargs.get("account_type") or ("LIVE" if is_live else "SIM")

        extra_cols = [
            "position_id",
            "strategy_id",
            "requested_pct",
            "requested_sol_amount",
            "requested_token_amount",
            "executed_sol_amount",
            "executed_token_amount",
            "price_usd",
            "price_sol",
            "slippage_bps",
            "price_impact_pct",
            "quote_json",
            "route_plan_json",
            "jito_tip_lamports",
            "priority_fee_lamports",
            "tx_signature",
            "bundle_id",
            "error_code",
            "error_message",
            "provider",
            "latency_ms",
        ]

        cols = [
            "idempotency_key",
            "created_at",
            "token_mint",
            "side",
            "event_type",
            "status",
            "is_live",
            "account_type",
        ]
        vals = [
            idempotency_key,
            created_at,
            token_mint,
            side,
            event_type,
            status,
            1 if is_live else 0,
            account_type_val,
        ]

        for c in extra_cols:
            if c in kwargs and kwargs[c] is not None:
                cols.append(c)
                vals.append(kwargs[c])

        placeholders = ",".join(["?"] * len(vals))
        sql = f"INSERT INTO trade_events({','.join(cols)}) VALUES({placeholders})"

        async def _do():
            cur = await self.db.execute(sql, tuple(vals))
            return cur.lastrowid

        try:
            nid = await self._write_txn(_do())
            async with self.db.execute("SELECT * FROM trade_events WHERE id = ?", (nid,)) as cur2:
                row2 = await cur2.fetchone()
            return dict(row2)
        except aiosqlite.IntegrityError:
            async with self.db.execute(
                "SELECT * FROM trade_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ) as cur:
                row = await cur.fetchone()
            if row:
                return dict(row)
            raise
        except Exception as e:
            self._safe_log("append_trade_event failed", idempotency_key=idempotency_key, error=str(e))
            raise

    async def get_trade_event(self, id: int) -> Optional[Dict[str, Any]]:
        async with self.db.execute("SELECT * FROM trade_events WHERE id = ?", (id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_trade_events(
        self,
        limit: int = 100,
        account_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if account_type:
            async with self.db.execute(
                "SELECT * FROM trade_events WHERE account_type = ? ORDER BY id DESC LIMIT ?",
                (account_type, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with self.db.execute(
                "SELECT * FROM trade_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # bandit_observations

    async def insert_bandit_observation(
        self,
        token_mint: str,
        strategy_id: int,
        is_live: bool,
        action_json: str,
        feature_vector_json: str,
        position_id: Optional[int] = None,
        discovery_event_id: Optional[int] = None,
    ):
        created_at = utc_now_iso()

        async def _do():
            await self.db.execute(
                "INSERT INTO bandit_observations(token_mint, position_id, strategy_id, is_live, discovery_event_id, action_json, feature_vector_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    token_mint,
                    position_id,
                    strategy_id,
                    1 if is_live else 0,
                    discovery_event_id,
                    action_json,
                    feature_vector_json,
                    created_at,
                ),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("insert_bandit_observation failed", token=token_mint, error=str(e))
            raise

    async def finalize_bandit_observation(
        self,
        observation_id: int,
        reward_json: str,
        final_net_pnl_pct: float,
        exit_reason: str,
        max_runup_pct: Optional[float] = None,
        max_drawdown_pct: Optional[float] = None,
        holding_seconds: Optional[int] = None,
    ):
        finalized_at = utc_now_iso()

        updates: Dict[str, Any] = {
            "reward_json": reward_json,
            "final_net_pnl_pct": final_net_pnl_pct,
            "exit_reason": exit_reason,
            "finalized_at": finalized_at,
        }

        if max_runup_pct is not None:
            updates["max_runup_pct"] = max_runup_pct
        if max_drawdown_pct is not None:
            updates["max_drawdown_pct"] = max_drawdown_pct
        if holding_seconds is not None:
            updates["holding_seconds"] = holding_seconds

        cols = [f"{k} = ?" for k in updates.keys()]
        params = list(updates.values())
        params.append(observation_id)

        async def _do():
            await self.db.execute(
                f"UPDATE bandit_observations SET {', '.join(cols)} WHERE id = ?",
                tuple(params),
            )

        try:
            await self._write_txn(_do())
        except Exception as e:
            self._safe_log("finalize_bandit_observation failed", id=observation_id, error=str(e))
            raise

    async def list_token_bandit_observations(self, token_mint: str, limit: int = 100) -> List[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM bandit_observations WHERE token_mint = ? ORDER BY id DESC LIMIT ?",
            (token_mint, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]