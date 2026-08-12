from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .config import get_settings


SCHEMA_VERSION = 11


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Small SQLite data-access boundary with one connection per operation."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else get_settings().database_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def execute(self, sql: str, parameters: Sequence[Any] | Mapping[str, Any] = ()) -> int:
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(sql, parameters)
            return cursor.rowcount

    def fetch_one(
        self, sql: str, parameters: Sequence[Any] | Mapping[str, Any] = ()
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
            return dict(row) if row is not None else None

    def fetch_all(
        self, sql: str, parameters: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, parameters).fetchall()]

    def initialize(self) -> None:
        with self.transaction(immediate=True) as connection:
            connection.executescript(SCHEMA_SQL)
            self._migrate_schema(connection)
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}

    @classmethod
    def _ensure_column(
        cls,
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        if column not in cls._column_names(connection, table):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @classmethod
    def _migrate_schema(cls, connection: sqlite3.Connection) -> None:
        # v2: preserve complete label-path facts and durable training requests.
        for column, definition in (
            ("first_take_profit_at", "INTEGER"),
            ("first_stop_loss_at", "INTEGER"),
            ("exit_reason", "TEXT"),
            ("same_bar_conflict", "INTEGER NOT NULL DEFAULT 0"),
            ("gross_return_rate", "REAL"),
            ("return_source", "TEXT"),
        ):
            cls._ensure_column(connection, "samples", column, definition)
        cls._ensure_column(
            connection,
            "training_runs",
            "request_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        cls._ensure_column(connection, "training_runs", "scheduled_for", "TEXT")
        cls._ensure_column(
            connection,
            "training_runs",
            "retry_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_training_run_per_schedule "
            "ON training_runs(scheduled_for) WHERE scheduled_for IS NOT NULL"
        )
        cls._ensure_column(connection, "positions", "simulation_session_id", "TEXT")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_positions_simulation_session "
            "ON positions(simulation_session_id, account_kind, status)"
        )

        # v5: durable simulation-session registry.
        current_session: dict[str, Any] = {}
        current_row = connection.execute(
            "SELECT value_json FROM runtime_state WHERE key='simulation_session'"
        ).fetchone()
        if current_row:
            try:
                decoded = json.loads(current_row["value_json"] or "{}")
                if isinstance(decoded, dict):
                    current_session = decoded
            except (TypeError, json.JSONDecodeError):
                current_session = {}
        current_id = str(current_session.get("id") or "")
        historical = connection.execute(
            """
            SELECT simulation_session_id AS id,
                   MIN(entry_time) AS started_at,
                   MAX(COALESCE(exit_time, entry_time)) AS ended_at
            FROM positions
            WHERE simulation_session_id IS NOT NULL
              AND account_kind IN ('paper','shadow_aggressive','shadow_conservative')
            GROUP BY simulation_session_id
            """
        ).fetchall()
        for row in historical:
            session_id = str(row["id"])
            is_current = bool(current_id and session_id == current_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO simulation_sessions(
                    id,started_at,ended_at,status,initial_cash_usd,
                    initial_sol_fee_reserve,created_reason
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    session_id,
                    row["started_at"] or current_session.get("started_at") or utc_now_iso(),
                    None if is_current else row["ended_at"],
                    "active" if is_current else "closed",
                    float(current_session.get("initial_cash_usd") or 1000.0) if is_current else 1000.0,
                    float(current_session.get("initial_sol_fee_reserve") or 0.1) if is_current else 0.1,
                    "runtime_backfill" if is_current else "legacy_backfill",
                ),
            )
        if current_id:
            connection.execute(
                "UPDATE simulation_sessions SET status='closed', ended_at=COALESCE(ended_at, ?) WHERE status='active' AND id<>?",
                (utc_now_iso(), current_id),
            )
            connection.execute(
                """
                INSERT INTO simulation_sessions(
                    id,started_at,ended_at,status,initial_cash_usd,
                    initial_sol_fee_reserve,created_reason
                ) VALUES(?,?,NULL,'active',?,?,?)
                ON CONFLICT(id) DO UPDATE SET status='active', ended_at=NULL
                """,
                (
                    current_id,
                    current_session.get("started_at") or utc_now_iso(),
                    float(current_session.get("initial_cash_usd") or 1000.0),
                    float(current_session.get("initial_sol_fee_reserve") or 0.1),
                    "runtime_backfill",
                ),
            )

        # v6: durable Agent approval registry.
        proposal_row = connection.execute(
            "SELECT value_json FROM runtime_state WHERE key='agent_proposals'"
        ).fetchone()
        if proposal_row:
            try:
                legacy_proposals = json.loads(proposal_row["value_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                legacy_proposals = []
            if isinstance(legacy_proposals, list):
                allowed_status = {
                    "pending_approval",
                    "approved",
                    "rejected",
                    "executed",
                    "failed",
                }
                for record in legacy_proposals:
                    if not isinstance(record, dict) or not record.get("id"):
                        continue
                    status = str(record.get("status") or "pending_approval")
                    if status not in allowed_status:
                        status = "pending_approval"
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO agent_proposals(
                            id,proposal_type,payload_json,status,created_at
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            str(record["id"]),
                            str(record.get("proposal_type") or "unknown"),
                            json.dumps(record.get("payload") or {}, ensure_ascii=False, separators=(",", ":")),
                            status,
                            str(record.get("created_at") or utc_now_iso()),
                        ),
                    )

        # v7/v8: Top-3 strategy registry and final removal of the historical
        # three-profile schema. Existing rows are retained under neutral legacy
        # strategy keys; all new simulation rows use account_kind='simulation'.
        cls._migrate_strategy_tables(connection)

        # v9: simulation is a single-USD accounting ledger. SOL remains an
        # immutable execution fact on each trade, while its fee-time USD value
        # is frozen explicitly. Legacy rows are never repriced with today's SOL
        # price; their new USD fields intentionally remain NULL when no original
        # fee-time FX fact exists.
        for column, definition in (
            ("strategy_key", "TEXT"),
            ("simulation_session_id", "TEXT"),
            ("platform_fee_usd", "REAL"),
            ("sol_usd_price", "REAL"),
            ("sol_usd_observed_at", "INTEGER"),
            ("network_fee_usd", "REAL"),
            ("slippage_cost_usd", "REAL"),
            ("fee_occurred_at", "TEXT"),
        ):
            cls._ensure_column(connection, "trades", column, definition)
        connection.execute(
            """
            UPDATE trades
            SET strategy_key=COALESCE(strategy_key,(SELECT p.strategy_key FROM positions p WHERE p.id=trades.position_id)),
                simulation_session_id=COALESCE(simulation_session_id,(SELECT p.simulation_session_id FROM positions p WHERE p.id=trades.position_id)),
                account_kind=COALESCE((SELECT p.account_kind FROM positions p WHERE p.id=trades.position_id),account_kind)
            WHERE position_id IS NOT NULL
            """
        )
        connection.execute(
            """
            UPDATE trades
            SET strategy_key=COALESCE(strategy_key,account_kind), account_kind='simulation'
            WHERE account_kind IN ('model_1','model_2','model_3','rules_only')
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_simulation_strategy "
            "ON trades(simulation_session_id,strategy_key,created_at)"
        )
        # The column is retained only for backward-compatible session history.
        # Active/new simulation sessions use a single USD ledger and therefore
        # never carry an artificial SOL fee reserve.
        connection.execute(
            "UPDATE simulation_sessions SET initial_sol_fee_reserve=0 WHERE status='active'"
        )

        # v10: completed Top-3 candidates may intentionally remain unpromoted
        # while the old model generation drains its positions. Preserve those
        # candidate rows across restart and widen the durable trigger enum for
        # insufficient-data daily training.
        cls._migrate_training_runs_v10(connection)

        # v11: H1 labels are first-class audit facts. The historical 2h max/min
        # columns remain untouched for provenance, but all new label finalization
        # writes only the one-hour fields below.
        for column in ("price_1h_max_ratio", "price_1h_min_ratio", "final_1h_close_ratio"):
            cls._ensure_column(connection, "samples", column, "REAL")
        connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS reject_completed_sample_insert
            BEFORE INSERT ON samples
            WHEN NEW.token_type='completed'
            BEGIN
                SELECT RAISE(ABORT, 'completed lifecycle is not supported');
            END;
            CREATE TRIGGER IF NOT EXISTS reject_completed_sample_update
            BEFORE UPDATE OF token_type ON samples
            WHEN NEW.token_type='completed'
            BEGIN
                SELECT RAISE(ABORT, 'completed lifecycle is not supported');
            END;
            """
        )

    @classmethod
    def _migrate_training_runs_v10(cls, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='training_runs'"
        ).fetchone()
        sql = str(row[0] or "") if row else ""
        if "'daily'" in sql:
            return
        connection.execute("DROP INDEX IF EXISTS one_training_run_per_schedule")
        connection.execute("ALTER TABLE training_runs RENAME TO training_runs_pre_v10")
        connection.executescript(
            """
            CREATE TABLE training_runs (
                id TEXT PRIMARY KEY,
                trigger TEXT NOT NULL CHECK(trigger IN ('manual','weekly','daily','startup_catchup','degraded')),
                status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','skipped')),
                requested_at TEXT NOT NULL,
                request_json TEXT NOT NULL DEFAULT '{}',
                scheduled_for TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                candidate_model_id TEXT REFERENCES models(id),
                champion_before_id TEXT REFERENCES models(id),
                promoted INTEGER NOT NULL DEFAULT 0 CHECK(promoted IN (0,1)),
                summary_json TEXT NOT NULL DEFAULT '{}',
                error_message TEXT
            );
            CREATE UNIQUE INDEX one_training_run_per_schedule
                ON training_runs(scheduled_for) WHERE scheduled_for IS NOT NULL;
            """
        )
        connection.execute(
            """
            INSERT INTO training_runs(
                id,trigger,status,requested_at,request_json,scheduled_for,retry_count,
                started_at,completed_at,candidate_model_id,champion_before_id,promoted,
                summary_json,error_message
            )
            SELECT id,trigger,status,requested_at,request_json,scheduled_for,retry_count,
                   started_at,completed_at,candidate_model_id,champion_before_id,promoted,
                   summary_json,error_message
            FROM training_runs_pre_v10
            """
        )
        connection.execute("DROP TABLE training_runs_pre_v10")

    @classmethod
    def _migrate_strategy_tables(cls, connection: sqlite3.Connection) -> None:
        prediction_columns = cls._column_names(connection, "predictions")
        position_columns = cls._column_names(connection, "positions")
        needs_rebuild = (
            "profile" in prediction_columns
            or "profile" in position_columns
            or "strategy_key" not in prediction_columns
            or "strategy_key" not in position_columns
            or "sample_id" not in position_columns
            or "simulation" not in str(
                connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='positions'").fetchone()[0]
            )
        )
        if not needs_rebuild:
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_predictions_strategy_time ON predictions(strategy_key,predicted_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_positions_strategy_status ON positions(simulation_session_id,strategy_key,status)"
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_position_per_prediction_strategy
                ON positions(prediction_id,strategy_key) WHERE prediction_id IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_rule_position_per_sample_session
                ON positions(sample_id,simulation_session_id,strategy_key)
                WHERE prediction_id IS NULL AND sample_id IS NOT NULL AND strategy_key='rules_only'
                """
            )
            return

        connection.execute("ALTER TABLE trades RENAME TO trades_pre_strategy_v8")
        connection.execute("ALTER TABLE positions RENAME TO positions_pre_strategy_v8")
        connection.execute("ALTER TABLE predictions RENAME TO predictions_pre_strategy_v8")
        connection.executescript(
            """
            CREATE TABLE predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
                model_id TEXT NOT NULL REFERENCES models(id),
                probability REAL NOT NULL CHECK(probability>=0 AND probability<=1),
                strategy_key TEXT NOT NULL,
                threshold REAL NOT NULL CHECK(threshold>=0 AND threshold<=1),
                selected INTEGER NOT NULL CHECK(selected IN (0,1)),
                predicted_at TEXT NOT NULL,
                UNIQUE(sample_id,model_id,strategy_key)
            );
            CREATE TABLE positions (
                id TEXT PRIMARY KEY,
                token_address TEXT NOT NULL,
                account_kind TEXT NOT NULL CHECK(account_kind IN ('simulation','live')),
                strategy_key TEXT,
                status TEXT NOT NULL CHECK(status IN ('opening','open','closing','closed','manual_intervention','failed')),
                simulation_session_id TEXT REFERENCES simulation_sessions(id),
                sample_id INTEGER REFERENCES samples(id),
                prediction_id INTEGER REFERENCES predictions(id),
                model_id TEXT REFERENCES models(id),
                entry_time TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                invested_usd REAL NOT NULL CHECK(invested_usd>=0),
                token_amount REAL,
                entry_price REAL,
                stop_loss_price REAL,
                take_profit_price REAL,
                exit_time TEXT,
                exit_price REAL,
                exit_reason TEXT,
                gross_pnl_usd REAL,
                net_pnl_usd REAL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE trades (
                id TEXT PRIMARY KEY,
                position_id TEXT REFERENCES positions(id),
                client_order_id TEXT NOT NULL UNIQUE,
                intent_fingerprint TEXT NOT NULL,
                journal_state TEXT NOT NULL DEFAULT 'reserved',
                provider_order_id TEXT,
                transaction_hash TEXT,
                side TEXT NOT NULL CHECK(side IN ('buy','sell')),
                account_kind TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('created','quoting','submitted','pending','processed','confirmed','failed','expired','cancelled')),
                requested_amount REAL NOT NULL,
                filled_amount REAL,
                expected_output REAL,
                actual_output REAL,
                slippage_bps INTEGER,
                priority_fee_sol REAL,
                tip_fee_sol REAL,
                network_fee_sol REAL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                failure_category TEXT,
                failure_code TEXT,
                failure_message TEXT,
                request_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        old_prediction_columns = cls._column_names(connection, "predictions_pre_strategy_v8")
        if "strategy_key" in old_prediction_columns:
            prediction_strategy = (
                "COALESCE(strategy_key,CASE profile "
                "WHEN 'balanced' THEN 'legacy_model_1' "
                "WHEN 'aggressive' THEN 'legacy_model_2' "
                "WHEN 'conservative' THEN 'legacy_model_3' ELSE 'legacy_model' END)"
            )
        else:
            prediction_strategy = (
                "CASE profile WHEN 'balanced' THEN 'legacy_model_1' "
                "WHEN 'aggressive' THEN 'legacy_model_2' "
                "WHEN 'conservative' THEN 'legacy_model_3' ELSE 'legacy_model' END"
            )
        connection.execute(
            f"""
            INSERT INTO predictions(id,sample_id,model_id,probability,strategy_key,threshold,selected,predicted_at)
            SELECT id,sample_id,model_id,probability,{prediction_strategy},threshold,selected,predicted_at
            FROM predictions_pre_strategy_v8
            """
        )

        old_position_columns = cls._column_names(connection, "positions_pre_strategy_v8")
        if "strategy_key" in old_position_columns:
            position_strategy = (
                "COALESCE(strategy_key,CASE account_kind "
                "WHEN 'paper' THEN 'legacy_model_1' "
                "WHEN 'shadow_aggressive' THEN 'legacy_model_2' "
                "WHEN 'shadow_conservative' THEN 'legacy_model_3' ELSE NULL END)"
            )
        else:
            position_strategy = (
                "CASE account_kind WHEN 'paper' THEN 'legacy_model_1' "
                "WHEN 'shadow_aggressive' THEN 'legacy_model_2' "
                "WHEN 'shadow_conservative' THEN 'legacy_model_3' ELSE NULL END"
            )
        if "sample_id" in old_position_columns:
            sample_expression = (
                "COALESCE(sample_id,(SELECT p.sample_id FROM predictions_pre_strategy_v8 p "
                "WHERE p.id=positions_pre_strategy_v8.prediction_id))"
            )
        else:
            sample_expression = (
                "(SELECT p.sample_id FROM predictions_pre_strategy_v8 p "
                "WHERE p.id=positions_pre_strategy_v8.prediction_id)"
            )
        connection.execute(
            f"""
            INSERT INTO positions(
                id,token_address,account_kind,strategy_key,status,simulation_session_id,
                sample_id,prediction_id,model_id,entry_time,expires_at,invested_usd,
                token_amount,entry_price,stop_loss_price,take_profit_price,exit_time,
                exit_price,exit_reason,gross_pnl_usd,net_pnl_usd,metadata_json
            )
            SELECT id,token_address,CASE WHEN account_kind='live' THEN 'live' ELSE 'simulation' END,
                   {position_strategy},status,simulation_session_id,{sample_expression},prediction_id,
                   model_id,entry_time,expires_at,invested_usd,token_amount,entry_price,
                   stop_loss_price,take_profit_price,exit_time,exit_price,exit_reason,
                   gross_pnl_usd,net_pnl_usd,metadata_json
            FROM positions_pre_strategy_v8
            """
        )
        connection.execute(
            """
            INSERT INTO trades(
                id,position_id,client_order_id,intent_fingerprint,journal_state,
                provider_order_id,transaction_hash,side,account_kind,status,
                requested_amount,filled_amount,expected_output,actual_output,slippage_bps,
                priority_fee_sol,tip_fee_sol,network_fee_sol,attempt_count,failure_category,
                failure_code,failure_message,request_json,response_json,result_json,created_at,updated_at
            )
            SELECT id,position_id,client_order_id,intent_fingerprint,journal_state,
                   provider_order_id,transaction_hash,side,
                   CASE WHEN account_kind='live' THEN 'live' ELSE 'simulation' END,status,
                   requested_amount,filled_amount,expected_output,actual_output,slippage_bps,
                   priority_fee_sol,tip_fee_sol,network_fee_sol,attempt_count,failure_category,
                   failure_code,failure_message,request_json,response_json,result_json,created_at,updated_at
            FROM trades_pre_strategy_v8
            """
        )
        connection.execute("DROP TABLE trades_pre_strategy_v8")
        connection.execute("DROP TABLE positions_pre_strategy_v8")
        connection.execute("DROP TABLE predictions_pre_strategy_v8")
        connection.executescript(
            """
            CREATE INDEX idx_predictions_time ON predictions(predicted_at DESC);
            CREATE INDEX idx_predictions_strategy_time ON predictions(strategy_key,predicted_at DESC);
            CREATE INDEX idx_positions_status_kind ON positions(status,account_kind);
            CREATE INDEX idx_positions_strategy_status ON positions(simulation_session_id,strategy_key,status);
            CREATE UNIQUE INDEX one_position_per_prediction_strategy
                ON positions(prediction_id,strategy_key) WHERE prediction_id IS NOT NULL;
            CREATE UNIQUE INDEX one_rule_position_per_sample_session
                ON positions(sample_id,simulation_session_id,strategy_key)
                WHERE prediction_id IS NULL AND sample_id IS NOT NULL AND strategy_key='rules_only';
            CREATE UNIQUE INDEX one_live_position_per_token
                ON positions(token_address) WHERE account_kind='live' AND status IN ('opening','open','closing');
            CREATE INDEX idx_trades_position ON trades(position_id,created_at);
            CREATE INDEX idx_trades_status ON trades(status,updated_at);
            """
        )

    def set_runtime_state(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        now = utc_now_iso()
        self.execute(
            """
            INSERT INTO runtime_state(key, value_json, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
            """,
            (key, payload, now),
        )

    def get_runtime_state(self, key: str, default: Any = None) -> Any:
        row = self.fetch_one("SELECT value_json FROM runtime_state WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            return default

    def audit(
        self,
        *,
        category: str,
        action: str,
        severity: str = "info",
        entity_type: str | None = None,
        entity_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.execute(
            """
            INSERT INTO audit_logs(
                created_at, severity, category, action, entity_type, entity_id, details_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                severity,
                category,
                action,
                entity_type,
                entity_id,
                json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
            ),
        )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_key TEXT NOT NULL UNIQUE,
    chain TEXT NOT NULL DEFAULT 'sol',
    address TEXT NOT NULL,
    name TEXT,
    symbol TEXT,
    token_type TEXT,
    entry_time INTEGER NOT NULL,
    age_minutes REAL,
    launchpad TEXT,
    entry_price REAL NOT NULL,
    liquidity REAL,
    liquidity_estimated INTEGER NOT NULL DEFAULT 0 CHECK(liquidity_estimated IN (0,1)),
    utility_eligible INTEGER NOT NULL DEFAULT 1 CHECK(utility_eligible IN (0,1)),
    holder_count REAL,
    features_json TEXT NOT NULL,
    price_2h_max_ratio REAL,
    price_2h_min_ratio REAL,
    price_1h_max_ratio REAL,
    price_1h_min_ratio REAL,
    final_1h_close_ratio REAL,
    final_close_ratio REAL,
    first_take_profit_at INTEGER,
    first_stop_loss_at INTEGER,
    exit_reason TEXT,
    same_bar_conflict INTEGER NOT NULL DEFAULT 0 CHECK(same_bar_conflict IN (0,1)),
    gross_return_rate REAL,
    return_source TEXT,
    tag INTEGER CHECK(tag IN (0,1) OR tag IS NULL),
    label_status TEXT NOT NULL DEFAULT 'pending' CHECK(label_status IN ('pending','mature','failed')),
    label_version TEXT NOT NULL DEFAULT 'sl090_tp160_h2_binary_v3',
    label_source TEXT NOT NULL DEFAULT 'collector',
    terminal_return_estimated INTEGER NOT NULL DEFAULT 0 CHECK(terminal_return_estimated IN (0,1)),
    raw_json TEXT,
    collected_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_entry_time ON samples(entry_time);
CREATE INDEX IF NOT EXISTS idx_samples_label_status ON samples(label_status, entry_time);
CREATE INDEX IF NOT EXISTS idx_samples_address_status ON samples(chain, address, label_status);

CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL UNIQUE,
    algorithm TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('candidate','champion','retired','rejected','failed')),
    early_stage INTEGER NOT NULL CHECK(early_stage IN (0,1)),
    trained_at TEXT NOT NULL,
    training_window_start INTEGER,
    training_window_end INTEGER,
    validation_window_start INTEGER,
    validation_window_end INTEGER,
    feature_names_json TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    thresholds_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    training_data_hash TEXT,
    parent_model_id TEXT REFERENCES models(id),
    promoted_at TEXT,
    rejection_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_models_status_trained ON models(status, trained_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS only_one_champion ON models(status) WHERE status='champion';

CREATE TABLE IF NOT EXISTS active_model_slots (
    slot INTEGER PRIMARY KEY CHECK(slot BETWEEN 1 AND 3),
    model_id TEXT NOT NULL UNIQUE REFERENCES models(id),
    composite_score REAL NOT NULL,
    threshold REAL NOT NULL CHECK(threshold>=0 AND threshold<=1),
    metrics_json TEXT NOT NULL DEFAULT '{}',
    selected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL REFERENCES models(id),
    probability REAL NOT NULL CHECK(probability>=0 AND probability<=1),
    strategy_key TEXT NOT NULL,
    threshold REAL NOT NULL CHECK(threshold>=0 AND threshold<=1),
    selected INTEGER NOT NULL CHECK(selected IN (0,1)),
    predicted_at TEXT NOT NULL,
    UNIQUE(sample_id,model_id,strategy_key)
);
CREATE INDEX IF NOT EXISTS idx_predictions_time ON predictions(predicted_at DESC);

CREATE TABLE IF NOT EXISTS simulation_sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','closed')),
    initial_cash_usd REAL NOT NULL CHECK(initial_cash_usd>=0),
    initial_sol_fee_reserve REAL NOT NULL CHECK(initial_sol_fee_reserve>=0),
    created_reason TEXT NOT NULL DEFAULT 'automatic'
);
CREATE UNIQUE INDEX IF NOT EXISTS only_one_active_simulation_session
    ON simulation_sessions(status) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_simulation_sessions_started
    ON simulation_sessions(started_at DESC);

CREATE TABLE IF NOT EXISTS asset_usd_prices (
    asset TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    price_usd REAL NOT NULL CHECK(price_usd>0),
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(asset,observed_at)
);
CREATE INDEX IF NOT EXISTS idx_asset_usd_prices_latest
    ON asset_usd_prices(asset,observed_at DESC);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    token_address TEXT NOT NULL,
    account_kind TEXT NOT NULL CHECK(account_kind IN ('simulation','live')),
    strategy_key TEXT,
    status TEXT NOT NULL CHECK(status IN ('opening','open','closing','closed','manual_intervention','failed')),
    simulation_session_id TEXT REFERENCES simulation_sessions(id),
    sample_id INTEGER REFERENCES samples(id),
    prediction_id INTEGER REFERENCES predictions(id),
    model_id TEXT REFERENCES models(id),
    entry_time TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    invested_usd REAL NOT NULL CHECK(invested_usd>=0),
    token_amount REAL,
    entry_price REAL,
    stop_loss_price REAL,
    take_profit_price REAL,
    exit_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    gross_pnl_usd REAL,
    net_pnl_usd REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_positions_status_kind ON positions(status,account_kind);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_position_per_token
    ON positions(token_address) WHERE account_kind='live' AND status IN ('opening','open','closing');

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    position_id TEXT REFERENCES positions(id),
    client_order_id TEXT NOT NULL UNIQUE,
    intent_fingerprint TEXT NOT NULL,
    journal_state TEXT NOT NULL DEFAULT 'reserved',
    provider_order_id TEXT,
    transaction_hash TEXT,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    account_kind TEXT NOT NULL,
    strategy_key TEXT,
    simulation_session_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('created','quoting','submitted','pending','processed','confirmed','failed','expired','cancelled')),
    requested_amount REAL NOT NULL,
    filled_amount REAL,
    expected_output REAL,
    actual_output REAL,
    slippage_bps INTEGER,
    priority_fee_sol REAL,
    tip_fee_sol REAL,
    network_fee_sol REAL,
    platform_fee_usd REAL,
    sol_usd_price REAL,
    sol_usd_observed_at INTEGER,
    network_fee_usd REAL,
    slippage_cost_usd REAL,
    fee_occurred_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    failure_category TEXT,
    failure_code TEXT,
    failure_message TEXT,
    request_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id,created_at);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status,updated_at);

CREATE TABLE IF NOT EXISTS training_runs (
    id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL CHECK(trigger IN ('manual','weekly','daily','startup_catchup','degraded')),
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','skipped')),
    requested_at TEXT NOT NULL,
    request_json TEXT NOT NULL DEFAULT '{}',
    scheduled_for TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    candidate_model_id TEXT REFERENCES models(id),
    champion_before_id TEXT REFERENCES models(id),
    promoted INTEGER NOT NULL DEFAULT 0 CHECK(promoted IN (0,1)),
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS agent_proposals (
    id TEXT PRIMARY KEY,
    proposal_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('pending_approval','approved','rejected','executed','failed')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decision_note TEXT,
    executed_at TEXT,
    result_json TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_proposals_status_created
    ON agent_proposals(status,created_at DESC);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('debug','info','warning','error','critical')),
    category TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_category ON audit_logs(category,created_at DESC);
"""


def get_database() -> Database:
    return Database()
