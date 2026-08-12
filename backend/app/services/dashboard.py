from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ..database import Database
from ..repositories.models import ModelRepository
from ..repositories.samples import SampleRepository
from ..risk.service import RiskService
from ..strategy import (
    MODEL_STRATEGIES,
    RULES_ONLY,
    SIMULATION_STRATEGIES,
    algorithm_display_name,
    validate_strategy,
)
from .paper_trading import PaperTradingService
from .runtime import RuntimeService


class DashboardService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.samples = SampleRepository(database)
        self.models = ModelRepository(database)

    def overview(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        seven_days = (now - timedelta(days=7)).isoformat()
        simulation = PaperTradingService(self.database).simulation_status()
        session_id = str(simulation["session"]["id"])
        active_models = self.models.active_models()
        stats = self.samples.statistics()
        mature = int(stats.get("mature") or 0)
        positives = int(stats.get("positives") or 0)
        return {
            "as_of": now.isoformat(),
            "model": active_models[0] if active_models else None,
            "active_models": [self._model_for_view(item) for item in active_models],
            "strategies": self._strategy_registry(active_models),
            "strategy_performance": self._strategy_performance(session_id),
            "live_realized_pnl_usd": self._live_realized_pnl(),
            "dataset": {**stats, "positive_rate": positives / mature if mature else None},
            "pnl": {
                "today": self._pnl_since(today, session_id),
                "seven_days": self._pnl_since(seven_days, session_id),
            },
            "open_positions": self.database.fetch_all(
                """
                SELECT COALESCE(strategy_key,'live') AS strategy_key,COUNT(*) AS count,
                       COALESCE(SUM(invested_usd),0) AS invested_usd
                FROM positions
                WHERE status IN ('opening','open','closing')
                  AND (account_kind='live' OR simulation_session_id=?)
                GROUP BY COALESCE(strategy_key,'live')
                """,
                (session_id,),
            ),
            "simulation": simulation,
            "risk": RiskService(self.database).status(),
            "runtime": RuntimeService(self.database).status(),
            "equity_curve": self._equity_curve(seven_days, session_id),
            "signal_activity": self._signal_activity(seven_days),
        }

    def list_signals(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """
            SELECT p.id,p.probability,p.strategy_key,p.threshold,p.selected,p.predicted_at,
                   s.address,s.name,s.symbol,s.launchpad,s.entry_time,s.tag,
                   m.id AS model_id,m.version AS model_version,m.algorithm,m.trained_at,
                   a.slot AS active_slot
            FROM predictions p
            JOIN samples s ON s.id=p.sample_id
            JOIN models m ON m.id=p.model_id
            LEFT JOIN active_model_slots a ON a.model_id=p.model_id
            WHERE p.strategy_key IN ('model_1','model_2','model_3')
            ORDER BY p.predicted_at DESC,p.id DESC LIMIT ?
            """,
            (limit,),
        )
        for row in rows:
            row["model_label"] = self._short_model_label(row)
        return rows

    def list_positions(
        self,
        *,
        include_closed: bool = True,
        include_previous_simulation_sessions: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if not include_closed:
            clauses.append("status IN ('opening','open','closing','manual_intervention')")
        if not include_previous_simulation_sessions:
            session_id = str(PaperTradingService(self.database).ensure_simulation_session()["id"])
            clauses.append("(account_kind='live' OR simulation_session_id=?)")
            parameters.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        return self.database.fetch_all(
            f"SELECT * FROM positions {where} ORDER BY entry_time DESC LIMIT ?",
            tuple(parameters),
        )

    def portfolio_view(
        self,
        *,
        mode: str,
        strategy: str,
        page: int = 1,
        page_size: int = 30,
        start_at: str | None = None,
        end_at: str | None = None,
    ) -> dict[str, Any]:
        if mode not in {"simulation", "live"}:
            raise ValueError("mode must be simulation or live")
        validate_strategy(strategy)

        runtime = RuntimeService(self.database).status()
        simulation = PaperTradingService(self.database).simulation_status()
        active_models = self.models.active_models()
        strategy_registry = self._strategy_registry(active_models)
        strategy_model = next(
            (item for item in active_models if f"model_{item.get('active_slot')}" == strategy),
            None,
        )

        if mode == "simulation":
            common_clauses = ["p.strategy_key=?", "p.simulation_session_id=?"]
            common_params: list[Any] = [strategy, str(simulation["session"]["id"])]
        else:
            common_clauses = ["p.account_kind='live'"]
            common_params = []
            if strategy != RULES_ONLY:
                common_clauses.append("COALESCE(p.strategy_key,'model_1')=?")
                common_params.append(strategy)

        current_where = " AND ".join(
            common_clauses + ["p.status IN ('opening','open','closing','manual_intervention')"]
        )
        join_from = """
            FROM positions p
            LEFT JOIN predictions pr ON pr.id=p.prediction_id
            LEFT JOIN samples s ON s.id=COALESCE(p.sample_id,pr.sample_id)
        """
        current = self.database.fetch_all(
            f"""
            SELECT p.*,s.launchpad AS launchpad
            {join_from}
            WHERE {current_where}
            ORDER BY p.entry_time DESC
            """,
            tuple(common_params),
        )

        history_clauses = list(common_clauses) + ["p.status='closed'"]
        history_params = list(common_params)
        if start_at:
            history_clauses.append("p.exit_time>=?")
            history_params.append(start_at)
        if end_at:
            history_clauses.append("p.exit_time<=?")
            history_params.append(end_at)
        history_where = " AND ".join(history_clauses)
        total = int((self.database.fetch_one(
            f"SELECT COUNT(*) AS count {join_from} WHERE {history_where}",
            tuple(history_params),
        ) or {"count": 0})["count"])
        total_pages = max(1, (total + page_size - 1) // page_size)
        resolved_page = min(max(1, page), total_pages)
        offset = (resolved_page - 1) * page_size
        history = self.database.fetch_all(
            f"""
            SELECT p.*,s.launchpad AS launchpad
            {join_from}
            WHERE {history_where}
            ORDER BY p.exit_time DESC,p.entry_time DESC
            LIMIT ? OFFSET ?
            """,
            tuple(history_params + [page_size, offset]),
        )

        if mode == "simulation":
            accounts = simulation["accounts"]
            account_summary = accounts.get(strategy, {})
        else:
            summary = self.database.fetch_one(
                """
                SELECT COUNT(*) AS positions,
                       COALESCE(SUM(CASE WHEN status IN ('opening','open','closing','manual_intervention') THEN 1 ELSE 0 END),0) AS open_positions,
                       COALESCE(SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END),0) AS closed_positions,
                       COALESCE(SUM(CASE WHEN status='closed' THEN net_pnl_usd ELSE 0 END),0) AS realized_pnl_usd
                FROM positions WHERE account_kind='live'
                """
            ) or {}
            accounts = {}
            account_summary = {
                **summary,
                "strategy_key": strategy,
                "cash_usd": None,
                "sol_fee_reserve": None,
                "source": "gmgn_trading_api_scaffold",
            }

        return {
            "mode": mode,
            "strategy": strategy,
            "strategy_info": next(
                (item for item in strategy_registry if item["strategy_key"] == strategy),
                {"strategy_key": strategy, "label": strategy},
            ),
            "strategies": strategy_registry,
            "live_trading_enabled": bool(runtime.get("live_trading_enabled")),
            "simulation_enabled": bool(runtime.get("simulation_enabled")),
            "provider": "gmgn_api" if mode == "live" else "simulator",
            "model_alias": self._model_runtime_alias(mode, strategy_model),
            "session": simulation["session"] if mode == "simulation" else None,
            "accounts": accounts,
            "account": account_summary,
            "current": [self._position_for_view(item) for item in current],
            "history": {
                "items": [self._position_for_view(item) for item in history],
                "page": resolved_page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
            },
        }

    @staticmethod
    def _position_for_view(item: dict[str, Any]) -> dict[str, Any]:
        result = dict(item)
        try:
            metadata = json.loads(str(result.get("metadata_json") or "{}"))
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        snapshot = metadata.get("market_snapshot") if isinstance(metadata, dict) else None
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        result["current_price"] = snapshot.get("price")
        result["current_liquidity_usd"] = snapshot.get("liquidity_usd")
        result["current_market_cap_usd"] = snapshot.get("market_cap_usd")
        result["market_snapshot_at"] = snapshot.get("as_of")
        result["sell_failed"] = bool(metadata.get("sell_failed")) or str(result.get("exit_reason") or "").startswith("sell_failed_")
        result["sell_failure_reason"] = metadata.get("sell_failure_reason")
        result.pop("metadata_json", None)
        return result

    def _strategy_registry(self, active_models: list[dict[str, Any]]) -> list[dict[str, Any]]:
        registry: list[dict[str, Any]] = []
        for slot in (1, 2, 3):
            model = next((item for item in active_models if int(item.get("active_slot") or 0) == slot), None)
            registry.append(
                {
                    "strategy_key": f"model_{slot}",
                    "label": self._short_model_label(model) if model else f"模型 {slot} 未就绪",
                    "model_id": model.get("id") if model else None,
                    "algorithm": model.get("algorithm") if model else None,
                    "rank": slot,
                    "threshold": model.get("active_threshold") if model else None,
                    "composite_score": model.get("active_composite_score") if model else None,
                    "feature_count": len(model.get("feature_names") or []) if model else 0,
                }
            )
        registry.append(
            {
                "strategy_key": RULES_ONLY,
                "label": "不用模型",
                "model_id": None,
                "algorithm": None,
                "rank": None,
                "threshold": None,
                "composite_score": None,
                "feature_count": 0,
            }
        )
        return registry

    @staticmethod
    def _model_for_view(model: dict[str, Any]) -> dict[str, Any]:
        result = dict(model)
        result["model_label"] = DashboardService._short_model_label(model)
        return result

    @staticmethod
    def _short_model_label(model: dict[str, Any] | None) -> str:
        if not model:
            return "模型未就绪"
        try:
            trained_at = datetime.fromisoformat(str(model.get("trained_at") or ""))
        except ValueError:
            trained_at = None
        if trained_at is not None:
            if trained_at.tzinfo is None:
                trained_at = trained_at.replace(tzinfo=timezone.utc)
            trained = trained_at.astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d")
        else:
            trained = "未训练"
        return f"{trained}-{algorithm_display_name(str(model.get('algorithm') or 'Model'))}"

    def _strategy_performance(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """
            SELECT strategy_key,COUNT(*) AS positions,
                   COALESCE(SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END),0) AS closed_positions,
                   COALESCE(SUM(CASE WHEN status IN ('open','opening','closing') THEN 1 ELSE 0 END),0) AS open_positions,
                   COALESCE(SUM(CASE WHEN status='closed' THEN net_pnl_usd ELSE 0 END),0) AS realized_pnl_usd
            FROM positions
            WHERE simulation_session_id=?
              AND strategy_key IN ('model_1','model_2','model_3','rules_only')
            GROUP BY strategy_key
            """,
            (session_id,),
        )
        by_key = {row["strategy_key"]: row for row in rows}
        return [
            {
                "strategy_key": key,
                **by_key.get(key, {"positions": 0, "closed_positions": 0, "open_positions": 0, "realized_pnl_usd": 0.0}),
            }
            for key in SIMULATION_STRATEGIES
        ]

    def _live_realized_pnl(self) -> float:
        row = self.database.fetch_one(
            "SELECT COALESCE(SUM(net_pnl_usd),0) AS pnl FROM positions WHERE account_kind='live' AND status='closed' AND net_pnl_usd IS NOT NULL"
        ) or {"pnl": 0.0}
        return float(row.get("pnl") or 0.0)

    @staticmethod
    def _model_runtime_alias(mode: str, model: dict[str, Any] | None) -> str | None:
        if not model:
            return "rules_only" if mode == "simulation" else None
        try:
            trained_at = datetime.fromisoformat(str(model.get("trained_at") or ""))
        except ValueError:
            return None
        if trained_at.tzinfo is None:
            trained_at = trained_at.replace(tzinfo=timezone.utc)
        trained_at = trained_at.astimezone(timezone(timedelta(hours=8)))
        prefix = "sim" if mode == "simulation" else "live"
        return f"{prefix}_{trained_at:%Y%m%d}"

    def _pnl_since(self, since: str, session_id: str) -> dict[str, float]:
        rows = self.database.fetch_all(
            """
            SELECT COALESCE(strategy_key,'live') AS strategy_key,COALESCE(SUM(net_pnl_usd),0) AS pnl
            FROM positions
            WHERE status='closed' AND exit_time>=?
              AND (account_kind='live' OR simulation_session_id=?)
            GROUP BY COALESCE(strategy_key,'live')
            """,
            (since, session_id),
        )
        result = {"total": 0.0}
        for row in rows:
            value = float(row["pnl"] or 0)
            result[str(row["strategy_key"])] = value
            result["total"] += value
        return result

    def _equity_curve(self, since: str, session_id: str) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT substr(exit_time,1,10) AS date,COALESCE(strategy_key,'live') AS strategy_key,
                   ROUND(SUM(net_pnl_usd),4) AS daily_pnl
            FROM positions
            WHERE status='closed' AND exit_time>=?
              AND (account_kind='live' OR simulation_session_id=?)
            GROUP BY substr(exit_time,1,10),COALESCE(strategy_key,'live') ORDER BY date
            """,
            (since, session_id),
        )

    def _signal_activity(self, since: str) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT substr(predicted_at,1,10) AS date,strategy_key,
                   COUNT(*) AS predictions,SUM(selected) AS selected
            FROM predictions
            WHERE predicted_at>=? AND strategy_key IN ('model_1','model_2','model_3')
            GROUP BY substr(predicted_at,1,10),strategy_key ORDER BY date
            """,
            (since,),
        )
