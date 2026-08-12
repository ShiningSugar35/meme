from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from ..collector.constants import LabelPolicy
from ..collector.models import Kline
from ..config import Settings, get_settings
from ..database import Database, utc_now_iso
from ..strategy import RULES_ONLY, SIMULATION_STRATEGIES, algorithm_display_name, validate_strategy
from ..trading.simulator.quote import SimulatedQuoteProvider
from ..trading.simulator.types import ExecutionQuote, QuoteRequest, Side
from .sol_price import SolUsdPriceService


PaperAccount = str
PAPER_ACCOUNTS: tuple[str, ...] = SIMULATION_STRATEGIES


@dataclass(frozen=True, slots=True)
class PaperOpenResult:
    opened: bool
    reason: str
    position_id: str | None = None


@dataclass(frozen=True, slots=True)
class PaperLiquidationResult:
    closed: bool
    reason: str
    position_id: str


@dataclass(frozen=True, slots=True)
class PaperMonitorResult:
    position_id: str
    state: str
    reason: str
    trigger_at: int | None = None


class PaperTradingService:
    """SQLite-backed four-strategy simulation ledger."""

    EXIT_MAX_ATTEMPTS = 7

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        quote_provider: SimulatedQuoteProvider | None = None,
        sol_price_service: SolUsdPriceService | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.quote_provider = quote_provider or SimulatedQuoteProvider()
        self.sol_price = sol_price_service or SolUsdPriceService(database)

    @staticmethod
    def _state_key(strategy_key: str) -> str:
        return f"portfolio_strategy:{validate_strategy(strategy_key)}"

    @staticmethod
    def _strategy_for_row(row: dict[str, Any]) -> str:
        return validate_strategy(str(row.get("strategy_key") or ""))

    def ensure_simulation_session(self) -> dict[str, Any]:
        existing = self.database.get_runtime_state("simulation_session")
        if isinstance(existing, dict) and existing.get("id"):
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE simulation_sessions SET status='closed', ended_at=COALESCE(ended_at, ?) WHERE status='active' AND id<>?",
                    (utc_now_iso(), existing["id"]),
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
                        existing["id"],
                        existing.get("started_at") or utc_now_iso(),
                        float(existing.get("initial_cash_usd") or 1_000.0),
                        0.0,
                        "runtime_recovery",
                    ),
                )
            return existing
        active = self.database.fetch_one(
            "SELECT * FROM simulation_sessions WHERE status='active' ORDER BY started_at DESC LIMIT 1"
        )
        if active:
            session = {
                "id": active["id"],
                "started_at": active["started_at"],
                "initial_cash_usd": float(active["initial_cash_usd"]),
                "initial_sol_fee_reserve": 0.0,
            }
            self.database.set_runtime_state("simulation_session", session)
            return session
        session = {
            "id": f"sim_{uuid.uuid4().hex}",
            "started_at": utc_now_iso(),
            "initial_cash_usd": 1_000.0,
            "initial_sol_fee_reserve": 0.0,
        }
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE simulation_sessions SET status='closed', ended_at=COALESCE(ended_at, ?) WHERE status='active'",
                (utc_now_iso(),),
            )
            connection.execute(
                """
                INSERT INTO simulation_sessions(
                    id,started_at,ended_at,status,initial_cash_usd,
                    initial_sol_fee_reserve,created_reason
                ) VALUES(?,?,NULL,'active',?,?,?)
                """,
                (
                    session["id"],
                    session["started_at"],
                    session["initial_cash_usd"],
                    session["initial_sol_fee_reserve"],
                    "automatic",
                ),
            )
            self._write_runtime_state(connection, "simulation_session", session)
            for strategy in PAPER_ACCOUNTS:
                self._write_runtime_state(
                    connection,
                    self._state_key(strategy),
                    self._new_account_state(strategy, session["id"]),
                )
        return session

    @staticmethod
    def _new_account_state(
        account: PaperAccount,
        session_id: str,
        *,
        cash_usd: float = 1_000.0,
        source: str = "simulation_fixed",
        model_id: str | None = None,
        statistics_started_at: str | None = None,
    ) -> dict[str, Any]:
        state = {
            "session_id": session_id,
            "strategy_key": account,
            "cash_usd": float(cash_usd),
            "initial_cash_usd": float(cash_usd),
            "accounting_currency": "USD",
            "network_fee_accounting": "fee_time_sol_usd",
            "source": source,
            "updated_at": utc_now_iso(),
        }
        if model_id is not None:
            state["model_id"] = model_id
        if statistics_started_at is not None:
            state["statistics_started_at"] = statistics_started_at
        return state

    @staticmethod
    def _write_runtime_state(connection: Any, key: str, value: dict[str, Any]) -> None:
        connection.execute(
            """
            INSERT INTO runtime_state(key,value_json,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                value_json=excluded.value_json, updated_at=excluded.updated_at
            """,
            (
                key,
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                utc_now_iso(),
            ),
        )

    def ensure_account(
        self,
        account: PaperAccount,
        *,
        cash_usd: float = 1_000.0,
        source: str = "simulation_fixed",
    ) -> dict[str, Any]:
        strategy = validate_strategy(account)
        session = self.ensure_simulation_session()
        key = self._state_key(strategy)
        existing = self.database.get_runtime_state(key)
        if isinstance(existing, dict) and existing.get("session_id") == session["id"]:
            normalized = dict(existing)
            normalized.pop("sol_fee_reserve", None)
            normalized.pop("initial_sol_fee_reserve", None)
            normalized["accounting_currency"] = "USD"
            normalized["network_fee_accounting"] = "fee_time_sol_usd"
            normalized = self._reconcile_account_cash(strategy, session, normalized)
            if normalized != existing:
                normalized["updated_at"] = utc_now_iso()
                self.database.set_runtime_state(key, normalized)
            return normalized
        state = self._new_account_state(
            strategy,
            str(session["id"]),
            cash_usd=cash_usd,
            source=source,
        )
        state = self._reconcile_account_cash(strategy, session, state)
        self.database.set_runtime_state(key, state)
        return state

    def _account_generation_scope(
        self,
        strategy: str,
        session: dict[str, Any],
    ) -> tuple[str | None, str]:
        if strategy != RULES_ONLY:
            slot = int(strategy.rsplit("_", 1)[1])
            row = self.database.fetch_one(
                "SELECT model_id,selected_at FROM active_model_slots WHERE slot=?",
                (slot,),
            )
            if row:
                return str(row["model_id"]), str(row["selected_at"])
        return None, str(session.get("started_at") or "")

    def _reconcile_account_cash(
        self,
        strategy: str,
        session: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Rebuild available cash from durable accounting facts.

        This is intentionally deterministic so an older runtime_state value cannot
        drift away from positions/trades after accounting migrations or a process
        restart. Closed position net PnL is the complete cash delta for a round
        trip; current open principal and already-paid execution fees remain cash
        outflows; failed BUY fees without a position are also real outflows.
        """
        session_id = str(session["id"])
        model_id, started_at = self._account_generation_scope(strategy, session)
        if not started_at:
            return state
        if model_id is not None:
            closed = self.database.fetch_one(
                """
                SELECT COALESCE(SUM(net_pnl_usd),0) AS pnl
                FROM positions
                WHERE simulation_session_id=? AND strategy_key=? AND model_id=?
                  AND status='closed' AND entry_time>=?
                """,
                (session_id, strategy, model_id, started_at),
            ) or {}
            open_row = self.database.fetch_one(
                """
                SELECT COALESCE(SUM(invested_usd),0) AS invested
                FROM positions
                WHERE simulation_session_id=? AND strategy_key=? AND model_id=?
                  AND status IN ('opening','open','closing','manual_intervention')
                  AND entry_time>=?
                """,
                (session_id, strategy, model_id, started_at),
            ) or {}
            open_costs = self.database.fetch_one(
                """
                SELECT COALESCE(SUM(COALESCE(t.platform_fee_usd,0)+COALESCE(t.network_fee_usd,0)),0) AS costs
                FROM trades t JOIN positions p ON p.id=t.position_id
                WHERE p.simulation_session_id=? AND p.strategy_key=? AND p.model_id=?
                  AND p.status IN ('opening','open','closing','manual_intervention')
                  AND p.entry_time>=?
                """,
                (session_id, strategy, model_id, started_at),
            ) or {}
        else:
            closed = self.database.fetch_one(
                """
                SELECT COALESCE(SUM(net_pnl_usd),0) AS pnl
                FROM positions
                WHERE simulation_session_id=? AND strategy_key=? AND status='closed'
                  AND entry_time>=?
                """,
                (session_id, strategy, started_at),
            ) or {}
            open_row = self.database.fetch_one(
                """
                SELECT COALESCE(SUM(invested_usd),0) AS invested
                FROM positions
                WHERE simulation_session_id=? AND strategy_key=?
                  AND status IN ('opening','open','closing','manual_intervention')
                  AND entry_time>=?
                """,
                (session_id, strategy, started_at),
            ) or {}
            open_costs = self.database.fetch_one(
                """
                SELECT COALESCE(SUM(COALESCE(t.platform_fee_usd,0)+COALESCE(t.network_fee_usd,0)),0) AS costs
                FROM trades t JOIN positions p ON p.id=t.position_id
                WHERE p.simulation_session_id=? AND p.strategy_key=?
                  AND p.status IN ('opening','open','closing','manual_intervention')
                  AND p.entry_time>=?
                """,
                (session_id, strategy, started_at),
            ) or {}
        failed_entry = self.database.fetch_one(
            """
            SELECT COALESCE(SUM(COALESCE(network_fee_usd,0)),0) AS costs
            FROM trades
            WHERE account_kind='simulation' AND strategy_key=? AND simulation_session_id=?
              AND position_id IS NULL AND side='buy' AND status='failed' AND created_at>=?
            """,
            (strategy, session_id, started_at),
        ) or {}
        initial_cash = float(state.get("initial_cash_usd") or session.get("initial_cash_usd") or 1_000.0)
        expected = (
            initial_cash
            + float(closed.get("pnl") or 0.0)
            - float(open_row.get("invested") or 0.0)
            - float(open_costs.get("costs") or 0.0)
            - float(failed_entry.get("costs") or 0.0)
        )
        current = float(state.get("cash_usd") or 0.0)
        if abs(expected - current) <= 1e-9:
            return state
        result = dict(state)
        result["cash_usd"] = expected
        result["ledger_reconciled_at"] = utc_now_iso()
        result["ledger_reconciliation_delta_usd"] = expected - current
        self.database.audit(
            category="simulation",
            action="account_cash_reconciled",
            entity_type="simulation_strategy",
            entity_id=strategy,
            details={
                "model_id": model_id,
                "statistics_started_at": started_at,
                "before_cash_usd": current,
                "after_cash_usd": expected,
                "delta_usd": expected - current,
            },
        )
        return result

    def reset_model_accounts_for_activation(
        self,
        active_models: Sequence[dict[str, Any]],
        *,
        activated_at: str,
    ) -> None:
        """Start a fresh $1000 ledger for each newly activated model generation.

        Promotion is allowed only after model_1/2/3 are flat, so resetting these
        three strategy ledgers cannot strand capital in an older position. The
        rules_only control account is intentionally continuous across model
        generations.
        """
        if len(active_models) != 3:
            raise ValueError("exactly three model accounts are required for activation reset")
        session = self.ensure_simulation_session()
        with self.database.transaction(immediate=True) as connection:
            for slot, model in enumerate(active_models, start=1):
                strategy = f"model_{slot}"
                self._write_runtime_state(
                    connection,
                    self._state_key(strategy),
                    self._new_account_state(
                        strategy,
                        str(session["id"]),
                        cash_usd=1_000.0,
                        source="model_activation",
                        model_id=str(model["id"]),
                        statistics_started_at=activated_at,
                    ),
                )
        self.database.audit(
            category="simulation",
            action="model_generation_accounts_reset",
            entity_type="simulation_session",
            entity_id=str(session["id"]),
            details={
                "activated_at": activated_at,
                "model_ids": [str(model["id"]) for model in active_models],
                "cash_usd_each": 1_000.0,
            },
        )

    def reset_simulation(self, *, created_reason: str = "manual_reset") -> dict[str, Any]:
        open_count = int((self.database.fetch_one(
            """
            SELECT COUNT(*) AS count FROM positions
            WHERE account_kind='simulation'
              AND strategy_key IN ('model_1','model_2','model_3','rules_only')
              AND status IN ('opening','open','closing','manual_intervention')
            """
        ) or {"count": 0})["count"])
        if open_count:
            raise ValueError("simulation cannot reset while strategy positions are open")
        session = {
            "id": f"sim_{uuid.uuid4().hex}",
            "started_at": utc_now_iso(),
            "initial_cash_usd": 1_000.0,
            "initial_sol_fee_reserve": 0.0,
        }
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE simulation_sessions SET status='closed', ended_at=COALESCE(ended_at, ?) WHERE status='active'",
                (utc_now_iso(),),
            )
            connection.execute(
                """
                INSERT INTO simulation_sessions(
                    id,started_at,ended_at,status,initial_cash_usd,
                    initial_sol_fee_reserve,created_reason
                ) VALUES(?,?,NULL,'active',?,?,?)
                """,
                (
                    session["id"],
                    session["started_at"],
                    session["initial_cash_usd"],
                    session["initial_sol_fee_reserve"],
                    created_reason,
                ),
            )
            self._write_runtime_state(connection, "simulation_session", session)
            for strategy in PAPER_ACCOUNTS:
                self._write_runtime_state(
                    connection,
                    self._state_key(strategy),
                    self._new_account_state(strategy, session["id"]),
                )
        self.database.audit(
            category="simulation",
            action="simulation_session_reset",
            entity_type="simulation_session",
            entity_id=session["id"],
            details={"created_reason": created_reason},
        )
        return self.simulation_status()

    def simulation_status(self) -> dict[str, Any]:
        session = self.ensure_simulation_session()
        session_id = str(session["id"])
        active_scopes = {
            f"model_{int(row['slot'])}": row
            for row in self.database.fetch_all(
                "SELECT slot,model_id,selected_at FROM active_model_slots ORDER BY slot"
            )
        }
        accounts: dict[str, Any] = {}
        for strategy in PAPER_ACCOUNTS:
            state = self.ensure_account(strategy)
            scope = active_scopes.get(strategy)
            if scope:
                summary = self.database.fetch_one(
                    """
                    SELECT COUNT(*) AS positions,
                           COALESCE(SUM(CASE WHEN p.status IN ('opening','open','closing') THEN 1 ELSE 0 END),0) AS open_positions,
                           COALESCE(SUM(CASE WHEN p.status='closed' THEN 1 ELSE 0 END),0) AS closed_positions,
                           COALESCE(SUM(CASE WHEN p.status IN ('opening','open','closing') THEN p.invested_usd ELSE 0 END),0) AS invested_usd,
                           COALESCE(SUM(CASE WHEN p.status='closed' THEN p.net_pnl_usd ELSE 0 END),0) AS realized_pnl_usd
                    FROM positions p
                    WHERE p.strategy_key=? AND p.simulation_session_id=?
                      AND p.model_id=? AND p.entry_time>=?
                    """,
                    (strategy, session_id, scope["model_id"], scope["selected_at"]),
                ) or {}
                fees = self.database.fetch_one(
                    """
                    SELECT COALESCE(SUM(COALESCE(platform_fee_usd,0)),0) AS platform_fee_usd,
                           COALESCE(SUM(COALESCE(network_fee_usd,0)),0) AS network_fee_usd,
                           COALESCE(SUM(COALESCE(network_fee_sol,0)),0) AS network_fee_sol,
                           COALESCE(SUM(COALESCE(slippage_cost_usd,0)),0) AS slippage_cost_usd
                    FROM trades
                    WHERE account_kind='simulation' AND strategy_key=? AND simulation_session_id=?
                      AND created_at>=?
                    """,
                    (strategy, session_id, scope["selected_at"]),
                ) or {}
                try:
                    statistics_epoch = int(datetime.fromisoformat(str(scope["selected_at"])).timestamp())
                except (TypeError, ValueError):
                    statistics_epoch = 0
                quality = self.database.fetch_one(
                    """
                    SELECT COALESCE(SUM(CASE WHEN p.selected=1 AND s.tag=1 THEN 1 ELSE 0 END),0) AS tp,
                           COALESCE(SUM(CASE WHEN p.selected=1 THEN 1 ELSE 0 END),0) AS selected_count,
                           COALESCE(SUM(CASE WHEN s.tag=1 THEN 1 ELSE 0 END),0) AS positive_count
                    FROM predictions p
                    JOIN samples s ON s.id=p.sample_id
                    WHERE p.model_id=? AND p.strategy_key=? AND p.predicted_at>=?
                      AND s.entry_time>=?
                      AND s.label_status='mature' AND s.tag IN (0,1)
                      AND s.token_type IN ('new_creation','near_completion')
                      AND s.label_version=?
                    """,
                    (scope["model_id"], strategy, scope["selected_at"], statistics_epoch, LabelPolicy().label_version),
                ) or {}
                selected_count = int(quality.get("selected_count") or 0)
                positive_count = int(quality.get("positive_count") or 0)
                tp = int(quality.get("tp") or 0)
                precision = (tp / selected_count) if selected_count else None
                recall = (tp / positive_count) if positive_count else None
                statistics_started_at = str(scope["selected_at"])
                model_id = str(scope["model_id"])
            else:
                summary = self.database.fetch_one(
                    """
                    SELECT COUNT(*) AS positions,
                           COALESCE(SUM(CASE WHEN status IN ('opening','open','closing') THEN 1 ELSE 0 END),0) AS open_positions,
                           COALESCE(SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END),0) AS closed_positions,
                           COALESCE(SUM(CASE WHEN status IN ('opening','open','closing') THEN invested_usd ELSE 0 END),0) AS invested_usd,
                           COALESCE(SUM(CASE WHEN status='closed' THEN net_pnl_usd ELSE 0 END),0) AS realized_pnl_usd
                    FROM positions
                    WHERE strategy_key=? AND simulation_session_id=?
                    """,
                    (strategy, session_id),
                ) or {}
                fees = self.database.fetch_one(
                    """
                    SELECT COALESCE(SUM(COALESCE(platform_fee_usd,0)),0) AS platform_fee_usd,
                           COALESCE(SUM(COALESCE(network_fee_usd,0)),0) AS network_fee_usd,
                           COALESCE(SUM(COALESCE(network_fee_sol,0)),0) AS network_fee_sol,
                           COALESCE(SUM(COALESCE(slippage_cost_usd,0)),0) AS slippage_cost_usd
                    FROM trades
                    WHERE account_kind='simulation' AND strategy_key=? AND simulation_session_id=?
                    """,
                    (strategy, session_id),
                ) or {}
                statistics_started_at = str(session.get("started_at") or "")
                try:
                    statistics_epoch = int(datetime.fromisoformat(statistics_started_at).timestamp())
                except (TypeError, ValueError):
                    statistics_epoch = 0
                quality = self.database.fetch_one(
                    """
                    SELECT COUNT(*) AS mature_count,
                           COALESCE(SUM(CASE WHEN tag=1 THEN 1 ELSE 0 END),0) AS positive_count
                    FROM samples
                    WHERE label_status='mature' AND tag IN (0,1) AND entry_time>=?
                      AND token_type IN ('new_creation','near_completion')
                      AND label_version=?
                    """,
                    (statistics_epoch, LabelPolicy().label_version),
                ) or {}
                mature_count = int(quality.get("mature_count") or 0)
                positive_count = int(quality.get("positive_count") or 0)
                # rules_only classifies every admitted sample as positive. Its
                # precision therefore equals the observed positive prevalence;
                # positive-class recall is 100% whenever at least one positive
                # mature sample exists in the current simulation session.
                precision = (positive_count / mature_count) if mature_count else None
                recall = 1.0 if positive_count else None
                model_id = None
            platform_fee = float(fees.get("platform_fee_usd") or 0.0)
            network_fee = float(fees.get("network_fee_usd") or 0.0)
            accounts[strategy] = {
                **state,
                **summary,
                **fees,
                "model_id": model_id,
                "statistics_started_at": statistics_started_at,
                "trade_count": int(summary.get("closed_positions") or 0),
                "precision": precision,
                "recall": recall,
                "total_fees_usd": platform_fee + network_fee,
                "total_execution_cost_usd": platform_fee + network_fee + float(fees.get("slippage_cost_usd") or 0.0),
            }
        registry = self.database.fetch_one(
            "SELECT status,ended_at,created_reason FROM simulation_sessions WHERE id=?",
            (session_id,),
        ) or {}
        public_session = {**session, **registry}
        public_session.pop("initial_sol_fee_reserve", None)
        return {"session": public_session, "accounts": accounts}

    def simulation_history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        sessions = self.database.fetch_all(
            "SELECT * FROM simulation_sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        for session in sessions:
            session.pop("initial_sol_fee_reserve", None)
            summaries = self.database.fetch_all(
                """
                SELECT strategy_key,
                       COUNT(*) AS positions,
                       COALESCE(SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END),0) AS closed_positions,
                       COALESCE(SUM(CASE WHEN status IN ('opening','open','closing') THEN 1 ELSE 0 END),0) AS open_positions,
                       COALESCE(SUM(CASE WHEN status='closed' THEN net_pnl_usd ELSE 0 END),0) AS realized_pnl_usd
                FROM positions
                WHERE simulation_session_id=?
                  AND strategy_key IN ('model_1','model_2','model_3','rules_only')
                GROUP BY strategy_key
                """,
                (session["id"],),
            )
            by_strategy = {row["strategy_key"]: row for row in summaries}
            session["accounts"] = {
                strategy: by_strategy.get(
                    strategy,
                    {
                        "strategy_key": strategy,
                        "positions": 0,
                        "closed_positions": 0,
                        "open_positions": 0,
                        "realized_pnl_usd": 0.0,
                    },
                )
                for strategy in PAPER_ACCOUNTS
            }
            session["realized_pnl_usd"] = sum(
                float(item.get("realized_pnl_usd") or 0.0)
                for item in session["accounts"].values()
            )
        return sessions

    def simulation_audit(self, *, limit_sessions: int = 50) -> list[dict[str, Any]]:
        sessions = self.simulation_history(limit=limit_sessions)
        active_models = {
            f"model_{row['active_slot']}": row
            for row in self.database.fetch_all(
                """
                SELECT s.slot AS active_slot,s.selected_at,m.id,m.algorithm,m.trained_at
                FROM active_model_slots s JOIN models m ON m.id=s.model_id
                ORDER BY s.slot
                """
            )
        }
        rows: list[dict[str, Any]] = []
        for session in sessions:
            accounts = session.get("accounts") or {}
            for strategy in PAPER_ACCOUNTS:
                model = active_models.get(strategy)
                summary = accounts.get(strategy) or {}
                scope_clauses = ["simulation_session_id=?", "strategy_key=?"]
                scope_params: list[Any] = [session["id"], strategy]
                if session.get("status") == "active" and model is not None:
                    scope_clauses.extend(["model_id=?", "entry_time>=?"])
                    scope_params.extend([str(model["id"]), str(model.get("selected_at") or "")])
                    scope_where = " AND ".join(scope_clauses)
                    summary = self.database.fetch_one(
                        f"""
                        SELECT strategy_key,
                               COUNT(*) AS positions,
                               COALESCE(SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END),0) AS closed_positions,
                               COALESCE(SUM(CASE WHEN status IN ('opening','open','closing') THEN 1 ELSE 0 END),0) AS open_positions,
                               COALESCE(SUM(CASE WHEN status='closed' THEN net_pnl_usd ELSE 0 END),0) AS realized_pnl_usd
                        FROM positions WHERE {scope_where}
                        """,
                        tuple(scope_params),
                    ) or {}
                scope_where = " AND ".join(scope_clauses)
                timing = self.database.fetch_one(
                    f"""
                    SELECT MIN(entry_time) AS first_entry_time, MAX(exit_time) AS last_exit_time
                    FROM positions WHERE {scope_where}
                    """,
                    tuple(scope_params),
                ) or {}
                rows.append(
                    {
                        "session_id": session["id"],
                        "strategy_key": strategy,
                        "model_id": model.get("id") if model else None,
                        "algorithm": model.get("algorithm") if model else None,
                        "model_label": "不用模型" if strategy == RULES_ONLY else self._short_model_label(model),
                        "status": session["status"],
                        "first_entry_time": timing.get("first_entry_time"),
                        "last_exit_time": timing.get("last_exit_time"),
                        "positions": int(summary.get("positions") or 0),
                        "open_positions": int(summary.get("open_positions") or 0),
                        "closed_positions": int(summary.get("closed_positions") or 0),
                        "realized_pnl_usd": float(summary.get("realized_pnl_usd") or 0.0),
                    }
                )
        return rows

    @staticmethod
    def _short_model_label(model: dict[str, Any] | None) -> str:
        if not model:
            return "模型未就绪"
        trained = str(model.get("trained_at") or "").replace("-", "")[:8]
        return f"{trained}-{algorithm_display_name(str(model.get('algorithm') or 'Model'))}"

    def sync_shadow_accounts(self, *, cash_usd: float, sol_fee_reserve: float, snapshot_id: str) -> None:
        # Shadow-wallet mirroring is no longer part of the four-strategy design.
        return None

    def _network_fee_fact(
        self,
        requested_at: datetime,
        *,
        latency_ms: int,
        network_fee_sol: float,
    ) -> dict[str, Any] | None:
        fee_sol = max(0.0, float(network_fee_sol or 0.0))
        occurred_at = requested_at + timedelta(milliseconds=max(0, int(latency_ms or 0)))
        if fee_sol == 0:
            return {
                "network_fee_sol": 0.0,
                "sol_usd_price": None,
                "sol_usd_observed_at": None,
                "network_fee_usd": 0.0,
                "fee_occurred_at": occurred_at.isoformat(),
            }
        price = self.sol_price.price_at(occurred_at)
        if price is None:
            return None
        return {
            "network_fee_sol": fee_sol,
            "sol_usd_price": float(price.price_usd),
            "sol_usd_observed_at": int(price.observed_at),
            "network_fee_usd": fee_sol * float(price.price_usd),
            "fee_occurred_at": occurred_at.isoformat(),
        }

    @staticmethod
    def _slippage_cost_usd(amount_usd: float, quote: Any) -> float:
        return max(0.0, float(amount_usd)) * max(0.0, float(quote.slippage_bps or 0.0)) / 10_000.0

    def _position_paid_costs(self, position_id: str) -> float:
        row = self.database.fetch_one(
            """
            SELECT COALESCE(SUM(COALESCE(platform_fee_usd,0)+COALESCE(network_fee_usd,0)),0) AS paid
            FROM trades WHERE position_id=?
            """,
            (position_id,),
        ) or {"paid": 0.0}
        return float(row.get("paid") or 0.0)

    def open_from_prediction(
        self,
        *,
        sample_id: int,
        prediction_id: int,
        model_id: str,
        strategy_key: str,
    ) -> PaperOpenResult:
        return self._open_sample(
            sample_id=sample_id,
            prediction_id=prediction_id,
            model_id=model_id,
            strategy_key=strategy_key,
        )

    def open_rule_only(self, *, sample_id: int) -> PaperOpenResult:
        return self._open_sample(
            sample_id=sample_id,
            prediction_id=None,
            model_id=None,
            strategy_key=RULES_ONLY,
        )

    def _open_sample(
        self,
        *,
        sample_id: int,
        prediction_id: int | None,
        model_id: str | None,
        strategy_key: str,
    ) -> PaperOpenResult:
        strategy = validate_strategy(strategy_key)
        liquidation = self.database.get_runtime_state("liquidation_job") or {}
        if isinstance(liquidation, dict) and liquidation.get("status") in {"queued", "running"}:
            return PaperOpenResult(False, "liquidation_in_progress")
        session = self.ensure_simulation_session()
        if prediction_id is not None:
            existing = self.database.fetch_one(
                "SELECT id FROM positions WHERE prediction_id=? AND strategy_key=? LIMIT 1",
                (prediction_id, strategy),
            )
        else:
            existing = self.database.fetch_one(
                """
                SELECT id FROM positions
                WHERE sample_id=? AND simulation_session_id=? AND strategy_key='rules_only'
                LIMIT 1
                """,
                (sample_id, session["id"]),
            )
        if existing:
            return PaperOpenResult(False, "already_opened", existing["id"])
        sample = self.database.fetch_one("SELECT * FROM samples WHERE id=?", (sample_id,))
        if not sample:
            return PaperOpenResult(False, "sample_not_found")
        liquidity = float(sample.get("liquidity") or 0)
        if liquidity <= 0:
            return PaperOpenResult(False, "real_entry_liquidity_missing")
        open_count = int((self.database.fetch_one(
            """
            SELECT COUNT(*) AS count FROM positions
            WHERE strategy_key=? AND simulation_session_id=?
              AND status IN ('opening','open','closing')
            """,
            (strategy, session["id"]),
        ) or {"count": 0})["count"])
        if open_count >= self.settings.max_open_positions:
            return PaperOpenResult(False, "max_open_positions")

        state = self.ensure_account(strategy)
        capital = min(0.01 * liquidity, 50.0)
        if float(state["cash_usd"]) < capital:
            return PaperOpenResult(False, "insufficient_paper_cash")

        position_id = f"{strategy}-{uuid.uuid4().hex[:16]}"
        observed_at = datetime.fromtimestamp(int(sample["entry_time"]), timezone.utc)
        quote = self.quote_provider.quote(
            QuoteRequest(
                token_address=sample["address"],
                side=Side.BUY,
                amount_usd=capital,
                reference_price=float(sample["entry_price"]),
                liquidity_usd=liquidity,
                requested_at=observed_at,
                position_id=position_id,
            )
        )
        fee_fact = self._network_fee_fact(
            observed_at,
            latency_ms=quote.latency_ms,
            network_fee_sol=quote.network_fee_sol,
        )
        if fee_fact is None:
            return PaperOpenResult(False, "sol_usd_price_unavailable")
        if not quote.success or quote.fill_price is None:
            self._record_failed_trade(position_id, sample, strategy, "buy", capital, quote, fee_fact)
            return PaperOpenResult(False, quote.failure_category.value if quote.failure_category else "quote_failed")
        network_fee_usd = float(fee_fact["network_fee_usd"])
        if float(state["cash_usd"]) < capital + quote.fee_usd + network_fee_usd:
            return PaperOpenResult(False, "insufficient_paper_cash")

        expires_at = observed_at + timedelta(hours=1)
        quantity = capital / quote.fill_price
        metadata = {
            "sample_id": sample_id,
            "strategy_key": strategy,
            "execution_policy_version": "h1_route_aware_v1",
            "simulation_session_id": state["session_id"],
            "entry_fee_usd": quote.fee_usd,
            "entry_network_fee_sol": quote.network_fee_sol,
            "entry_network_fee_usd": network_fee_usd,
            "entry_sol_usd_price": fee_fact["sol_usd_price"],
            "entry_fee_occurred_at": fee_fact["fee_occurred_at"],
            "entry_slippage_bps": quote.slippage_bps,
            "latency_ms": quote.latency_ms,
            "quote_source": "seeded_local_execution_model",
        }
        next_state = dict(state)
        next_state["cash_usd"] = float(state["cash_usd"]) - capital - quote.fee_usd - network_fee_usd
        next_state["updated_at"] = utc_now_iso()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO positions(
                    id,token_address,account_kind,strategy_key,status,simulation_session_id,
                    sample_id,prediction_id,model_id,entry_time,expires_at,invested_usd,token_amount,
                    entry_price,stop_loss_price,take_profit_price,metadata_json
                ) VALUES(?,?,'simulation',?,'open',?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    position_id,
                    sample["address"],
                    strategy,
                    state["session_id"],
                    sample_id,
                    prediction_id,
                    model_id,
                    observed_at.isoformat(),
                    expires_at.isoformat(),
                    capital,
                    quantity,
                    quote.fill_price,
                    quote.fill_price * 0.9,
                    quote.fill_price * 1.6,
                    json.dumps(metadata, separators=(",", ":")),
                ),
            )
            self._insert_paper_trade(
                connection,
                position_id,
                sample,
                strategy,
                "buy",
                capital,
                quote,
                fee_fact,
                simulation_session_id=str(state["session_id"]),
            )
            self._write_runtime_state(connection, self._state_key(strategy), next_state)
        return PaperOpenResult(True, "opened", position_id)

    def settle_mature_positions(self) -> int:
        rows = self.database.fetch_all(
            """
            SELECT p.*,s.tag,s.entry_price AS reference_entry_price,s.liquidity,
                   s.final_1h_close_ratio,s.price_1h_min_ratio
            FROM positions p
            JOIN samples s ON s.id=p.sample_id
            WHERE p.strategy_key IN ('model_1','model_2','model_3','rules_only')
              AND p.status='open' AND s.label_status='mature'
              AND s.token_type IN ('new_creation','near_completion')
              AND s.label_version=?
            ORDER BY p.entry_time
            """,
            (LabelPolicy().label_version,),
        )
        settled = 0
        for row in rows:
            if self._settle(row):
                settled += 1
        return settled

    def monitor_position_realtime(
        self,
        position_id: str,
        current_price: float,
        *,
        now_ts: int | None = None,
        execution_quote: ExecutionQuote | None = None,
        defer_execution: bool = False,
    ) -> PaperMonitorResult:
        """Evaluate an open paper position from the current market price only."""
        row = self.database.fetch_one(
            """
            SELECT p.*,s.liquidity AS sample_liquidity
            FROM positions p
            LEFT JOIN predictions pr ON pr.id=p.prediction_id
            LEFT JOIN samples s ON s.id=COALESCE(p.sample_id,pr.sample_id)
            WHERE p.id=?
            """,
            (position_id,),
        )
        if not row:
            return PaperMonitorResult(position_id, "blocked", "position_not_found")
        if (
            str(row.get("account_kind") or "") != "simulation"
            or str(row.get("strategy_key") or "") not in SIMULATION_STRATEGIES
        ):
            return PaperMonitorResult(position_id, "blocked", "not_strategy_position")
        if row["status"] == "closed":
            return PaperMonitorResult(position_id, "closed", "already_closed")

        current_ts = int(now_ts or datetime.now(timezone.utc).timestamp())
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        pending_exit = metadata.get("paper_exit_pending")
        if isinstance(pending_exit, dict):
            if defer_execution and execution_quote is None:
                return PaperMonitorResult(
                    position_id,
                    "pending",
                    "execution_route_probe_required",
                    int(pending_exit.get("trigger_at") or 0) or None,
                )
            return self._execute_monitored_exit(
                row, metadata, pending_exit, now_ts=current_ts, execution_quote=execution_quote
            )
        if row["status"] != "open":
            return PaperMonitorResult(position_id, "pending", f"position_{row['status']}")

        price = float(current_price or 0.0)
        if price <= 0:
            return PaperMonitorResult(position_id, "blocked", "current_price_unavailable")
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        stop_price = float(row.get("stop_loss_price") or 0.0)
        take_price = float(row.get("take_profit_price") or 0.0)
        reason: str | None = None
        if stop_price > 0 and price <= stop_price:
            reason = "stop_loss_0_9x"
        elif take_price > 0 and price >= take_price:
            reason = "take_profit_1_6x"
        elif current_ts >= int(expires_at.timestamp()):
            reason = "timeout_1h"
        if reason is None:
            metadata["last_market_check_at"] = utc_now_iso()
            metadata["last_market_price"] = price
            self.database.execute(
                "UPDATE positions SET metadata_json=? WHERE id=? AND status='open'",
                (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
            )
            return PaperMonitorResult(position_id, "open", "no_exit_trigger")

        market_snapshot = metadata.get("market_snapshot") if isinstance(metadata.get("market_snapshot"), dict) else {}
        live_liquidity = market_snapshot.get("liquidity_usd")
        liquidity = float(live_liquidity) if live_liquidity is not None else float(row.get("sample_liquidity") or 0)
        pending_exit = {
            "reason": reason,
            "reference_price": price,
            "trigger_at": current_ts,
            "liquidity_usd": liquidity,
            "liquidity_source": "current_market_snapshot" if live_liquidity is not None else "entry_sample_fallback",
            "attempt_count": 0,
        }
        metadata["paper_exit_pending"] = pending_exit
        metadata["last_market_check_at"] = utc_now_iso()
        metadata["last_market_price"] = price
        self.database.execute(
            "UPDATE positions SET status='closing',metadata_json=? WHERE id=? AND status='open'",
            (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
        )
        row["status"] = "closing"
        row["metadata_json"] = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        if defer_execution and execution_quote is None:
            return PaperMonitorResult(position_id, "pending", "execution_route_probe_required", current_ts)
        return self._execute_monitored_exit(
            row, metadata, pending_exit, now_ts=current_ts, execution_quote=execution_quote
        )

    def monitor_position(
        self,
        position_id: str,
        klines: Sequence[Kline],
        *,
        now_ts: int | None = None,
        execution_quote: ExecutionQuote | None = None,
        defer_execution: bool = False,
    ) -> PaperMonitorResult:
        row = self.database.fetch_one(
            """
            SELECT p.*,s.liquidity AS sample_liquidity
            FROM positions p
            LEFT JOIN predictions pr ON pr.id=p.prediction_id
            LEFT JOIN samples s ON s.id=COALESCE(p.sample_id,pr.sample_id)
            WHERE p.id=?
            """,
            (position_id,),
        )
        if not row:
            return PaperMonitorResult(position_id, "blocked", "position_not_found")
        if (
            str(row.get("account_kind") or "") != "simulation"
            or str(row.get("strategy_key") or "") not in SIMULATION_STRATEGIES
        ):
            return PaperMonitorResult(position_id, "blocked", "not_strategy_position")
        if row["status"] == "closed":
            return PaperMonitorResult(position_id, "closed", "already_closed")

        current_ts = int(now_ts or datetime.now(timezone.utc).timestamp())
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        pending_exit = metadata.get("paper_exit_pending")
        if isinstance(pending_exit, dict):
            if defer_execution and execution_quote is None:
                return PaperMonitorResult(position_id, "pending", "execution_route_probe_required", int(pending_exit.get("trigger_at") or 0) or None)
            return self._execute_monitored_exit(
                row,
                metadata,
                pending_exit,
                now_ts=current_ts,
                execution_quote=execution_quote,
            )
        if row["status"] != "open":
            return PaperMonitorResult(position_id, "pending", f"position_{row['status']}")

        opened_at = datetime.fromisoformat(str(row["entry_time"]))
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        opened_ts = int(opened_at.timestamp())
        expires_ts = int(expires_at.timestamp())
        end_ts = min(current_ts, expires_ts)
        ordered = sorted(
            (line for line in klines if opened_ts <= int(line.timestamp) <= end_ts),
            key=lambda line: line.timestamp,
        )

        stop_price = float(row.get("stop_loss_price") or 0)
        take_price = float(row.get("take_profit_price") or 0)
        decision: tuple[str, float, int] | None = None
        for line in ordered:
            low = line.low if line.low is not None else line.close
            high = line.high if line.high is not None else line.close
            if low is not None and stop_price > 0 and low <= stop_price:
                decision = ("stop_loss_0_9x", stop_price, int(line.timestamp))
                break
            if high is not None and take_price > 0 and high >= take_price:
                decision = ("take_profit_1_6x", take_price, int(line.timestamp))
                break

        if decision is None and current_ts >= expires_ts:
            close_lines = [line for line in ordered if line.close not in (None, 0)]
            if close_lines:
                final_line = close_lines[-1]
                decision = ("timeout_1h", float(final_line.close), expires_ts)
            else:
                return PaperMonitorResult(position_id, "pending", "timeout_candle_unavailable")

        if decision is None:
            if ordered:
                metadata["last_market_check_at"] = utc_now_iso()
                metadata["last_kline_timestamp"] = int(ordered[-1].timestamp)
                self.database.execute(
                    "UPDATE positions SET metadata_json=? WHERE id=? AND status='open'",
                    (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
                )
            return PaperMonitorResult(position_id, "open", "no_exit_trigger")

        reason, reference_price, trigger_at = decision
        market_snapshot = metadata.get("market_snapshot") if isinstance(metadata.get("market_snapshot"), dict) else {}
        live_liquidity = market_snapshot.get("liquidity_usd")
        liquidity = float(live_liquidity) if live_liquidity is not None else float(row.get("sample_liquidity") or 0)
        pending_exit = {
            "reason": reason,
            "reference_price": reference_price,
            "trigger_at": trigger_at,
            "liquidity_usd": liquidity,
            "liquidity_source": "current_market_snapshot" if live_liquidity is not None else "entry_sample_fallback",
            "attempt_count": 0,
        }
        metadata["paper_exit_pending"] = pending_exit
        metadata["last_market_check_at"] = utc_now_iso()
        metadata["last_kline_timestamp"] = int(ordered[-1].timestamp) if ordered else trigger_at
        self.database.execute(
            "UPDATE positions SET status='closing',metadata_json=? WHERE id=? AND status='open'",
            (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
        )
        row["status"] = "closing"
        row["metadata_json"] = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        if defer_execution and execution_quote is None:
            return PaperMonitorResult(position_id, "pending", "execution_route_probe_required", trigger_at)
        return self._execute_monitored_exit(
            row,
            metadata,
            pending_exit,
            now_ts=current_ts,
            execution_quote=execution_quote,
        )

    def _execute_monitored_exit(
        self,
        row: dict[str, Any],
        metadata: dict[str, Any],
        pending_exit: dict[str, Any],
        *,
        now_ts: int,
        execution_quote: ExecutionQuote | None = None,
    ) -> PaperMonitorResult:
        position_id = str(row["id"])
        strategy = self._strategy_for_row(row)
        quantity = float(row.get("token_amount") or 0)
        reference_price = float(pending_exit.get("reference_price") or 0)
        liquidity = float(pending_exit.get("liquidity_usd") or 0)
        trigger_at = int(pending_exit.get("trigger_at") or 0)
        reason = str(pending_exit.get("reason") or "market_exit")
        if quantity <= 0 or reference_price <= 0 or trigger_at <= 0:
            return PaperMonitorResult(position_id, "blocked", "pending_exit_facts_incomplete", trigger_at or None)

        gross_reference = quantity * reference_price
        execution_at = datetime.fromtimestamp(now_ts, timezone.utc)
        quote = execution_quote or self.quote_provider.quote(
            QuoteRequest(
                token_address=row["token_address"],
                side=Side.SELL,
                amount_usd=gross_reference,
                reference_price=reference_price,
                liquidity_usd=liquidity,
                requested_at=execution_at,
                position_id=position_id,
            )
        )
        fee_fact = self._network_fee_fact(
            execution_at,
            latency_ms=quote.latency_ms,
            network_fee_sol=quote.network_fee_sol,
        )
        if fee_fact is None:
            return PaperMonitorResult(position_id, "blocked", "sol_usd_price_unavailable", trigger_at)
        if not quote.success or quote.fill_price is None:
            pending_exit["attempt_count"] = int(pending_exit.get("attempt_count") or 0) + 1
            pending_exit["last_failure"] = quote.failure_category.value if quote.failure_category else "quote_failed"
            pending_exit["last_attempt_at"] = utc_now_iso()
            pending_exit["network_fee_sol_charged"] = float(pending_exit.get("network_fee_sol_charged") or 0) + float(fee_fact["network_fee_sol"])
            pending_exit["network_fee_usd_charged"] = float(pending_exit.get("network_fee_usd_charged") or 0) + float(fee_fact["network_fee_usd"])
            metadata["paper_exit_pending"] = pending_exit
            self._record_failed_trade(
                position_id,
                row,
                strategy,
                "sell",
                gross_reference,
                quote,
                fee_fact,
                attach_position=True,
                client_order_id=f"{position_id}:sell_attempt:{pending_exit['attempt_count']}",
            )
            if self._sell_failure_is_terminal(row, pending_exit, now_ts=now_ts):
                return self._finalize_failed_exit(row, metadata, failure_reason=str(pending_exit["last_failure"]), failed_at=now_ts)
            self.database.execute(
                "UPDATE positions SET status='closing',metadata_json=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
            )
            return PaperMonitorResult(position_id, "pending", pending_exit["last_failure"], trigger_at)

        state = self.ensure_account(strategy)
        network_fee_usd = float(fee_fact["network_fee_usd"])
        gross_sale = quantity * quote.fill_price
        proceeds = gross_sale - quote.fee_usd - network_fee_usd
        invested = float(row["invested_usd"] or 0)
        gross_pnl = gross_sale - invested
        prior_costs = self._position_paid_costs(position_id)
        net_pnl = proceeds - invested - prior_costs
        final_metadata = dict(metadata)
        route_probe = pending_exit.get("route_probe") if isinstance(pending_exit.get("route_probe"), dict) else {}
        final_metadata.pop("paper_exit_pending", None)
        final_metadata.update({
            "execution_policy_version": "h1_route_aware_v1",
            "exit_route_probe": route_probe,
            "exit_trigger_at": trigger_at,
            "exit_quote_source": "jupiter_read_only_quote" if execution_quote is not None and route_probe.get("state") == "quoted" else "seeded_local_execution_model",
            "exit_fee_usd": quote.fee_usd,
            "exit_network_fee_sol": quote.network_fee_sol,
            "exit_network_fee_usd": network_fee_usd,
            "exit_sol_usd_price": fee_fact["sol_usd_price"],
            "exit_fee_occurred_at": fee_fact["fee_occurred_at"],
            "exit_slippage_bps": quote.slippage_bps,
        })
        next_state = dict(state)
        next_state["cash_usd"] = float(state["cash_usd"]) + proceeds
        next_state["updated_at"] = utc_now_iso()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE positions SET status='closed',exit_time=?,exit_price=?,exit_reason=?,
                    gross_pnl_usd=?,net_pnl_usd=?,metadata_json=?
                WHERE id=? AND status='closing'
                """,
                (
                    execution_at.isoformat(), quote.fill_price, reason, gross_pnl, net_pnl,
                    json.dumps(final_metadata, ensure_ascii=False, separators=(",", ":")), position_id,
                ),
            )
            if cursor.rowcount != 1:
                return PaperMonitorResult(position_id, "pending", "position_state_changed", trigger_at)
            self._insert_paper_trade(
                connection,
                position_id,
                row,
                strategy,
                "sell",
                gross_reference,
                quote,
                fee_fact,
                client_order_id=f"{position_id}:market_exit",
            )
            self._write_runtime_state(connection, self._state_key(strategy), next_state)
        self.database.audit(
            category="simulation",
            action="paper_position_closed",
            entity_type="position",
            entity_id=position_id,
            details={"strategy_key": strategy, "exit_reason": reason},
        )
        return PaperMonitorResult(position_id, "closed", reason, trigger_at)

    def _sell_failure_is_terminal(
        self,
        row: dict[str, Any],
        pending_exit: dict[str, Any],
        *,
        now_ts: int,
    ) -> bool:
        attempts = int(pending_exit.get("attempt_count") or 0)
        if attempts >= self.EXIT_MAX_ATTEMPTS:
            return True
        failure = str(pending_exit.get("last_failure") or "")
        if failure != "no_route":
            return False
        try:
            expires_at = datetime.fromisoformat(str(row["expires_at"]))
        except (TypeError, ValueError):
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return now_ts >= int(expires_at.timestamp())

    def _finalize_failed_exit(
        self,
        row: dict[str, Any],
        metadata: dict[str, Any],
        *,
        failure_reason: str,
        failed_at: int,
    ) -> PaperMonitorResult:
        position_id = str(row["id"])
        strategy = self._strategy_for_row(row)
        pending_exit = metadata.get("paper_exit_pending")
        pending_exit = pending_exit if isinstance(pending_exit, dict) else {}
        trigger_at = int(pending_exit.get("trigger_at") or 0)
        invested = float(row.get("invested_usd") or 0)
        paid_costs = self._position_paid_costs(position_id)
        final_metadata = dict(metadata)
        route_probe = pending_exit.get("route_probe") if isinstance(pending_exit.get("route_probe"), dict) else {}
        final_metadata.pop("paper_exit_pending", None)
        final_metadata["execution_policy_version"] = "h1_route_aware_v1"
        final_metadata["exit_route_probe"] = route_probe
        final_metadata["sell_failed"] = True
        final_metadata["sell_failure_reason"] = failure_reason
        final_metadata["sell_failure_attempts"] = int(pending_exit.get("attempt_count") or 0)
        final_metadata["sell_failed_at"] = datetime.fromtimestamp(failed_at, timezone.utc).isoformat()
        exit_reason = f"sell_failed_{failure_reason}"
        gross_pnl = -invested
        net_pnl = -invested - paid_costs
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE positions SET status='closed',exit_time=?,exit_price=0,exit_reason=?,
                    gross_pnl_usd=?,net_pnl_usd=?,metadata_json=?
                WHERE id=? AND status='closing'
                """,
                (
                    datetime.fromtimestamp(failed_at, timezone.utc).isoformat(), exit_reason,
                    gross_pnl, net_pnl,
                    json.dumps(final_metadata, ensure_ascii=False, separators=(",", ":")), position_id,
                ),
            )
            if cursor.rowcount != 1:
                return PaperMonitorResult(position_id, "pending", "position_state_changed", trigger_at or None)
            client_order_id = f"{position_id}:sell_failed"
            connection.execute(
                """
                INSERT OR IGNORE INTO trades(
                    id,position_id,client_order_id,intent_fingerprint,journal_state,side,account_kind,
                    strategy_key,simulation_session_id,status,requested_amount,platform_fee_usd,
                    network_fee_sol,network_fee_usd,slippage_cost_usd,failure_category,failure_message,
                    created_at,updated_at
                ) VALUES(?,?,?,?,'terminal','sell','simulation',?,?, 'failed',?,0,0,0,0,?,?,?,?)
                """,
                (
                    str(uuid.uuid4()), position_id, client_order_id,
                    hashlib.sha256(client_order_id.encode()).hexdigest(), strategy,
                    row.get("simulation_session_id"), invested, failure_reason, failure_reason,
                    utc_now_iso(), utc_now_iso(),
                ),
            )
        self.database.audit(
            category="simulation",
            action="paper_sell_failed_closed",
            severity="warning",
            entity_type="position",
            entity_id=position_id,
            details={
                "strategy_key": strategy,
                "failure_reason": failure_reason,
                "attempts": int(pending_exit.get("attempt_count") or 0),
                "net_pnl_usd": net_pnl,
            },
        )
        return PaperMonitorResult(position_id, "closed", exit_reason, trigger_at or None)

    def liquidate_position(
        self,
        position_id: str,
        *,
        now: datetime | None = None,
    ) -> PaperLiquidationResult:
        row = self.database.fetch_one("SELECT * FROM positions WHERE id=?", (position_id,))
        if not row:
            return PaperLiquidationResult(False, "position_not_found", position_id)
        if (
            str(row.get("account_kind") or "") != "simulation"
            or str(row.get("strategy_key") or "") not in SIMULATION_STRATEGIES
        ):
            return PaperLiquidationResult(False, "not_strategy_position", position_id)
        if row["status"] == "closed":
            return PaperLiquidationResult(True, "already_closed", position_id)
        if row["status"] not in {"opening", "open", "closing"}:
            return PaperLiquidationResult(False, f"position_{row['status']}", position_id)

        strategy = self._strategy_for_row(row)
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_epoch = int(moment.timestamp())
        market = self.database.fetch_one(
            """
            SELECT entry_price,liquidity,entry_time FROM samples
            WHERE chain='sol' AND address=? AND entry_time<=?
            ORDER BY entry_time DESC,id DESC LIMIT 1
            """,
            (row["token_address"], now_epoch),
        )
        if not market:
            return PaperLiquidationResult(False, "market_reference_missing", position_id)
        if now_epoch - int(market["entry_time"]) > self.settings.signal_max_age_seconds:
            return PaperLiquidationResult(False, "market_reference_stale", position_id)
        reference_price = float(market.get("entry_price") or 0)
        liquidity = float(market.get("liquidity") or 0)
        quantity = float(row.get("token_amount") or 0)
        if reference_price <= 0 or liquidity <= 0 or quantity <= 0:
            return PaperLiquidationResult(False, "market_reference_incomplete", position_id)
        gross_reference = quantity * reference_price
        quote = self.quote_provider.quote(
            QuoteRequest(
                token_address=row["token_address"], side=Side.SELL, amount_usd=gross_reference,
                reference_price=reference_price, liquidity_usd=liquidity,
                requested_at=moment, position_id=position_id,
            )
        )
        fee_fact = self._network_fee_fact(
            moment,
            latency_ms=quote.latency_ms,
            network_fee_sol=quote.network_fee_sol,
        )
        if fee_fact is None:
            return PaperLiquidationResult(False, "sol_usd_price_unavailable", position_id)
        if not quote.success or quote.fill_price is None:
            self._record_failed_trade(
                position_id,
                row,
                strategy,
                "sell",
                gross_reference,
                quote,
                fee_fact,
                attach_position=True,
                client_order_id=f"{position_id}:liquidate_failed:{uuid.uuid4().hex[:8]}",
            )
            return PaperLiquidationResult(False, quote.failure_category.value if quote.failure_category else "quote_failed", position_id)
        state = self.ensure_account(strategy)
        network_fee_usd = float(fee_fact["network_fee_usd"])
        gross_sale = quantity * quote.fill_price
        proceeds = gross_sale - quote.fee_usd - network_fee_usd
        gross_pnl = gross_sale - float(row["invested_usd"])
        pnl = proceeds - float(row["invested_usd"]) - self._position_paid_costs(position_id)
        next_state = dict(state)
        next_state["cash_usd"] = float(state["cash_usd"]) + proceeds
        next_state["updated_at"] = utc_now_iso()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE positions SET status='closed',exit_time=?,exit_price=?,exit_reason='liquidate_all',
                    gross_pnl_usd=?,net_pnl_usd=? WHERE id=? AND status IN ('opening','open','closing')
                """,
                (moment.isoformat(), quote.fill_price, gross_pnl, pnl, position_id),
            )
            if cursor.rowcount != 1:
                return PaperLiquidationResult(False, "position_state_changed", position_id)
            self._insert_paper_trade(
                connection,
                position_id,
                row,
                strategy,
                "sell",
                gross_reference,
                quote,
                fee_fact,
                client_order_id=f"{position_id}:liquidate_all",
            )
            self._write_runtime_state(connection, self._state_key(strategy), next_state)
        return PaperLiquidationResult(True, "closed", position_id)

    def _settle(self, row: dict[str, Any]) -> bool:
        tag = int(row["tag"])
        reference_entry = float(row["reference_entry_price"])
        if tag == 1:
            exit_reason, reference_price = "take_profit_1_6x", reference_entry * 1.6
        elif float(row.get("price_1h_min_ratio") or 1.0) <= 0.9:
            exit_reason, reference_price = "stop_loss_0_9x", reference_entry * 0.9
        else:
            ratio = float(row.get("final_1h_close_ratio") or 1.0)
            exit_reason, reference_price = "timeout_1h", reference_entry * ratio
        quantity = float(row["token_amount"] or 0)
        gross_reference = quantity * reference_price
        observed_at = datetime.fromisoformat(row["expires_at"])
        strategy = self._strategy_for_row(row)
        quote = self.quote_provider.quote(
            QuoteRequest(
                token_address=row["token_address"], side=Side.SELL, amount_usd=gross_reference,
                reference_price=reference_price, liquidity_usd=float(row.get("liquidity") or 0),
                requested_at=observed_at, position_id=row["id"],
            )
        )
        fee_fact = self._network_fee_fact(
            observed_at,
            latency_ms=quote.latency_ms,
            network_fee_sol=quote.network_fee_sol,
        )
        if fee_fact is None:
            return False
        if not quote.success or quote.fill_price is None:
            self._record_failed_trade(
                str(row["id"]),
                row,
                strategy,
                "sell",
                gross_reference,
                quote,
                fee_fact,
                attach_position=True,
                client_order_id=f"{row['id']}:settle_failed:{uuid.uuid4().hex[:8]}",
            )
            return False
        state = self.ensure_account(strategy)
        network_fee_usd = float(fee_fact["network_fee_usd"])
        gross_sale = quantity * quote.fill_price
        proceeds = gross_sale - quote.fee_usd - network_fee_usd
        gross_pnl = gross_sale - float(row["invested_usd"])
        pnl = proceeds - float(row["invested_usd"]) - self._position_paid_costs(str(row["id"]))
        next_state = dict(state)
        next_state["cash_usd"] = float(state["cash_usd"]) + proceeds
        next_state["updated_at"] = utc_now_iso()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE positions SET status='closed',exit_time=?,exit_price=?,exit_reason=?,
                    gross_pnl_usd=?,net_pnl_usd=? WHERE id=? AND status='open'
                """,
                (observed_at.isoformat(), quote.fill_price, exit_reason, gross_pnl, pnl, row["id"]),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_paper_trade(
                connection,
                str(row["id"]),
                row,
                strategy,
                "sell",
                gross_reference,
                quote,
                fee_fact,
            )
            self._write_runtime_state(connection, self._state_key(strategy), next_state)
        return True

    def _record_failed_trade(
        self,
        position_id: str,
        sample: dict[str, Any],
        account: str,
        side: str,
        amount: float,
        quote: Any,
        fee_fact: dict[str, Any],
        *,
        attach_position: bool = False,
        client_order_id: str | None = None,
    ) -> None:
        now = utc_now_iso()
        client_order_id = client_order_id or f"{position_id}:{side}"
        fingerprint = hashlib.sha256(client_order_id.encode()).hexdigest()
        state = self.ensure_account(account)
        session_id = sample.get("simulation_session_id") or state.get("session_id")
        network_fee_usd = float(fee_fact["network_fee_usd"])
        next_state = dict(state)
        if network_fee_usd > 0:
            next_state["cash_usd"] = float(state["cash_usd"]) - network_fee_usd
            next_state["updated_at"] = now
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO trades(
                    id,position_id,client_order_id,intent_fingerprint,journal_state,side,account_kind,
                    strategy_key,simulation_session_id,status,requested_amount,network_fee_sol,
                    platform_fee_usd,sol_usd_price,sol_usd_observed_at,network_fee_usd,
                    slippage_cost_usd,fee_occurred_at,failure_category,failure_message,created_at,updated_at
                ) VALUES(?,?,?,?,'terminal',?,'simulation',?,?,'failed',?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(uuid.uuid4()), position_id if attach_position else None, client_order_id, fingerprint,
                    side, account, session_id, amount, float(fee_fact["network_fee_sol"]), 0.0,
                    fee_fact["sol_usd_price"], fee_fact["sol_usd_observed_at"], network_fee_usd, 0.0,
                    fee_fact["fee_occurred_at"],
                    quote.failure_category.value if quote.failure_category else "api",
                    (quote.message or "")[:500], now, now,
                ),
            )
            if network_fee_usd > 0:
                self._write_runtime_state(connection, self._state_key(account), next_state)

    def _insert_paper_trade(
        self,
        connection: Any,
        position_id: str,
        sample: dict[str, Any],
        account: str,
        side: str,
        amount: float,
        quote: Any,
        fee_fact: dict[str, Any],
        *,
        simulation_session_id: str | None = None,
        client_order_id: str | None = None,
    ) -> None:
        now = utc_now_iso()
        client_order_id = client_order_id or f"{position_id}:{side}"
        session_id = simulation_session_id or sample.get("simulation_session_id")
        connection.execute(
            """
            INSERT INTO trades(
                id,position_id,client_order_id,intent_fingerprint,journal_state,side,account_kind,
                strategy_key,simulation_session_id,status,requested_amount,filled_amount,slippage_bps,
                network_fee_sol,platform_fee_usd,sol_usd_price,sol_usd_observed_at,network_fee_usd,
                slippage_cost_usd,fee_occurred_at,response_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?, 'simulation',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), position_id, client_order_id,
                hashlib.sha256(client_order_id.encode()).hexdigest(), "terminal", side,
                account, session_id, "confirmed", amount, amount, int(round(quote.slippage_bps)),
                float(fee_fact["network_fee_sol"]), float(quote.fee_usd), fee_fact["sol_usd_price"],
                fee_fact["sol_usd_observed_at"], float(fee_fact["network_fee_usd"]),
                self._slippage_cost_usd(amount, quote), fee_fact["fee_occurred_at"],
                json.dumps({"latency_ms": quote.latency_ms, "fee_usd": quote.fee_usd}, separators=(",", ":")),
                now, now,
            ),
        )
