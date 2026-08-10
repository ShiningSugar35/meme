from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app.database import Database
from backend.app.services.dashboard import DashboardService
from backend.app.services.paper_trading import PaperTradingService


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "dashboard.db")
    database.initialize()
    return database


def insert_closed(
    database: Database,
    *,
    position_id: str,
    account_kind: str,
    session_id: str | None,
    pnl: float,
    now: datetime,
) -> None:
    profile = (
        "aggressive"
        if account_kind == "shadow_aggressive"
        else "conservative"
        if account_kind == "shadow_conservative"
        else "balanced"
    )
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,profile,status,simulation_session_id,
            entry_time,expires_at,invested_usd,exit_time,net_pnl_usd
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            position_id,
            f"token-{position_id}",
            account_kind,
            profile,
            "closed",
            session_id,
            (now - timedelta(hours=1)).isoformat(),
            (now + timedelta(hours=1)).isoformat(),
            50.0,
            now.isoformat(),
            pnl,
        ),
    )


def test_dashboard_excludes_previous_simulation_sessions_but_keeps_live(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    paper = PaperTradingService(database)
    now = datetime.now(timezone.utc)
    old_session = paper.ensure_simulation_session()["id"]
    insert_closed(
        database,
        position_id="old-paper",
        account_kind="paper",
        session_id=old_session,
        pnl=100.0,
        now=now,
    )
    current = paper.reset_simulation()["session"]["id"]
    insert_closed(
        database,
        position_id="current-paper",
        account_kind="paper",
        session_id=current,
        pnl=7.5,
        now=now,
    )
    insert_closed(
        database,
        position_id="live-position",
        account_kind="live",
        session_id=None,
        pnl=2.5,
        now=now,
    )

    dashboard = DashboardService(database).overview()

    assert dashboard["simulation"]["session"]["id"] == current
    assert dashboard["simulation"]["accounts"]["paper"]["realized_pnl_usd"] == pytest.approx(7.5)
    assert dashboard["pnl"]["seven_days"]["paper"] == pytest.approx(7.5)
    assert dashboard["pnl"]["seven_days"]["live"] == pytest.approx(2.5)
    assert dashboard["pnl"]["seven_days"]["total"] == pytest.approx(10.0)
    assert all(row["daily_pnl"] != pytest.approx(100.0) for row in dashboard["equity_curve"])

    service = DashboardService(database)
    current_positions = service.list_positions(include_closed=True)
    all_positions = service.list_positions(
        include_closed=True,
        include_previous_simulation_sessions=True,
    )
    assert {row["id"] for row in current_positions} == {"current-paper", "live-position"}
    assert {row["id"] for row in all_positions} == {
        "old-paper",
        "current-paper",
        "live-position",
    }
