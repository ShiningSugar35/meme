from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..database import Database
from ..repositories.models import ModelRepository
from ..repositories.samples import SampleRepository
from ..risk.service import RiskService
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
        simulation_session_id = str(simulation["session"]["id"])
        pnl_today = self._pnl_since(today, simulation_session_id)
        pnl_7d = self._pnl_since(seven_days, simulation_session_id)
        positions = self.database.fetch_all(
            """
            SELECT account_kind, COUNT(*) AS count, COALESCE(SUM(invested_usd),0) AS invested_usd
            FROM positions
            WHERE status IN ('opening','open','closing')
              AND (
                    account_kind='live'
                    OR simulation_session_id=?
                  )
            GROUP BY account_kind
            """,
            (simulation_session_id,),
        )
        champion = self.models.champion()
        stats = self.samples.statistics()
        mature = int(stats.get("mature") or 0)
        positives = int(stats.get("positives") or 0)
        return {
            "as_of": now.isoformat(),
            "model": champion,
            "dataset": {
                **stats,
                "positive_rate": positives / mature if mature else None,
            },
            "pnl": {"today": pnl_today, "seven_days": pnl_7d},
            "open_positions": positions,
            "simulation": simulation,
            "risk": RiskService(self.database).status(),
            "runtime": RuntimeService(self.database).status(),
            "equity_curve": self._equity_curve(seven_days, simulation_session_id),
            "signal_activity": self._signal_activity(seven_days),
        }

    def list_signals(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT p.id, p.probability, p.profile, p.threshold, p.selected, p.predicted_at,
                   s.address, s.name, s.symbol, s.launchpad, s.entry_time, s.tag,
                   m.version AS model_version
            FROM predictions p
            JOIN samples s ON s.id=p.sample_id
            JOIN models m ON m.id=p.model_id
            ORDER BY p.predicted_at DESC LIMIT ?
            """,
            (limit,),
        )

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

    def _pnl_since(self, since: str, simulation_session_id: str) -> dict[str, float]:
        rows = self.database.fetch_all(
            """
            SELECT account_kind, COALESCE(SUM(net_pnl_usd),0) AS pnl
            FROM positions
            WHERE status='closed' AND exit_time >= ?
              AND (account_kind='live' OR simulation_session_id=?)
            GROUP BY account_kind
            """,
            (since, simulation_session_id),
        )
        result = {"total": 0.0}
        for row in rows:
            value = float(row["pnl"] or 0)
            result[row["account_kind"]] = value
            result["total"] += value
        return result

    def _equity_curve(self, since: str, simulation_session_id: str) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT substr(exit_time,1,10) AS date, account_kind,
                   ROUND(SUM(net_pnl_usd), 4) AS daily_pnl
            FROM positions
            WHERE status='closed' AND exit_time >= ?
              AND (account_kind='live' OR simulation_session_id=?)
            GROUP BY substr(exit_time,1,10), account_kind ORDER BY date
            """,
            (since, simulation_session_id),
        )

    def _signal_activity(self, since: str) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT substr(predicted_at,1,10) AS date, profile,
                   COUNT(*) AS predictions, SUM(selected) AS selected
            FROM predictions WHERE predicted_at >= ?
            GROUP BY substr(predicted_at,1,10), profile ORDER BY date
            """,
            (since,),
        )

