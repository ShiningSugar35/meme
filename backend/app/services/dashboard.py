from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ..database import Database
from ..repositories.models import ModelRepository
from ..repositories.samples import SampleRepository
from ..risk.service import RiskService
from .paper_trading import PaperTradingService
from .runtime import RuntimeService


class DashboardService:
    PROFILE_ACCOUNT = {
        "balanced": "paper",
        "aggressive": "shadow_aggressive",
        "conservative": "shadow_conservative",
    }

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

    def portfolio_view(
        self,
        *,
        mode: str,
        profile: str,
        page: int = 1,
        page_size: int = 30,
        start_at: str | None = None,
        end_at: str | None = None,
    ) -> dict[str, Any]:
        if mode not in {"simulation", "live"}:
            raise ValueError("mode must be simulation or live")
        if profile not in self.PROFILE_ACCOUNT:
            raise ValueError("profile must be aggressive, balanced or conservative")

        runtime = RuntimeService(self.database).status()
        simulation = PaperTradingService(self.database).simulation_status()
        account_kind = self.PROFILE_ACCOUNT[profile] if mode == "simulation" else "live"
        common_clauses = ["account_kind=?", "profile=?"]
        common_params: list[Any] = [account_kind, profile]
        if mode == "simulation":
            common_clauses.append("simulation_session_id=?")
            common_params.append(str(simulation["session"]["id"]))

        current_where = " AND ".join(
            common_clauses + ["status IN ('opening','open','closing','manual_intervention')"]
        )
        current = self.database.fetch_all(
            f"SELECT * FROM positions WHERE {current_where} ORDER BY entry_time DESC",
            tuple(common_params),
        )

        history_clauses = list(common_clauses) + ["status='closed'"]
        history_params = list(common_params)
        if start_at:
            history_clauses.append("exit_time>=?")
            history_params.append(start_at)
        if end_at:
            history_clauses.append("exit_time<=?")
            history_params.append(end_at)
        history_where = " AND ".join(history_clauses)
        total = int(
            (
                self.database.fetch_one(
                    f"SELECT COUNT(*) AS count FROM positions WHERE {history_where}",
                    tuple(history_params),
                )
                or {"count": 0}
            )["count"]
        )
        total_pages = max(1, (total + page_size - 1) // page_size)
        resolved_page = min(max(1, page), total_pages)
        offset = (resolved_page - 1) * page_size
        history = self.database.fetch_all(
            f"""
            SELECT * FROM positions
            WHERE {history_where}
            ORDER BY exit_time DESC, entry_time DESC
            LIMIT ? OFFSET ?
            """,
            tuple(history_params + [page_size, offset]),
        )

        if mode == "simulation":
            accounts = {
                item_profile: simulation["accounts"].get(item_account, {})
                for item_profile, item_account in self.PROFILE_ACCOUNT.items()
            }
        else:
            live_rows = self.database.fetch_all(
                """
                SELECT
                    profile,
                    COUNT(*) AS positions,
                    COALESCE(SUM(CASE WHEN status IN ('opening','open','closing','manual_intervention') THEN 1 ELSE 0 END),0) AS open_positions,
                    COALESCE(SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END),0) AS closed_positions,
                    COALESCE(SUM(CASE WHEN status='closed' THEN net_pnl_usd ELSE 0 END),0) AS realized_pnl_usd
                FROM positions WHERE account_kind='live'
                GROUP BY profile
                """
            )
            by_profile = {str(row["profile"]): row for row in live_rows}
            accounts = {
                item_profile: {
                    **by_profile.get(
                        item_profile,
                        {
                            "positions": 0,
                            "open_positions": 0,
                            "closed_positions": 0,
                            "realized_pnl_usd": 0.0,
                        },
                    ),
                    "account": "live",
                    "cash_usd": None,
                    "sol_fee_reserve": None,
                    "source": "gmgn_trading_api_scaffold",
                }
                for item_profile in self.PROFILE_ACCOUNT
            }
        account_summary = accounts.get(profile, {})

        return {
            "mode": mode,
            "profile": profile,
            "live_trading_enabled": bool(runtime.get("live_trading_enabled")),
            "simulation_enabled": bool(runtime.get("simulation_enabled")),
            "provider": "gmgn_api" if mode == "live" else "simulator",
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
        result.pop("metadata_json", None)
        return result

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
