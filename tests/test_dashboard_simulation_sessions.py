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


def test_portfolio_view_filters_profile_time_and_exposes_market_snapshot(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    paper = PaperTradingService(database)
    session_id = paper.ensure_simulation_session()["id"]
    now = datetime.now(timezone.utc)
    insert_closed(
        database,
        position_id="balanced-recent",
        account_kind="paper",
        session_id=session_id,
        pnl=6.0,
        now=now,
    )
    insert_closed(
        database,
        position_id="balanced-old",
        account_kind="paper",
        session_id=session_id,
        pnl=-2.0,
        now=now - timedelta(days=2),
    )
    insert_closed(
        database,
        position_id="aggressive-recent",
        account_kind="shadow_aggressive",
        session_id=session_id,
        pnl=9.0,
        now=now,
    )
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,profile,status,simulation_session_id,
            entry_time,expires_at,invested_usd,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "balanced-open",
            "token-balanced-open",
            "paper",
            "balanced",
            "open",
            session_id,
            now.isoformat(),
            (now + timedelta(hours=2)).isoformat(),
            40.0,
            '{"market_snapshot":{"price":1.2,"liquidity_usd":12345.0,"market_cap_usd":98765.0,"as_of":"2026-08-11T00:00:00+00:00"}}',
        ),
    )

    view = DashboardService(database).portfolio_view(
        mode="simulation",
        profile="balanced",
        page=1,
        page_size=10,
        start_at=(now - timedelta(hours=12)).isoformat(),
    )

    assert view["history"]["total"] == 1
    assert [row["id"] for row in view["history"]["items"]] == ["balanced-recent"]
    assert [row["id"] for row in view["current"]] == ["balanced-open"]
    assert view["current"][0]["current_liquidity_usd"] == pytest.approx(12345.0)
    assert view["current"][0]["current_market_cap_usd"] == pytest.approx(98765.0)
    assert view["accounts"]["balanced"]["realized_pnl_usd"] == pytest.approx(4.0)
    assert view["accounts"]["aggressive"]["realized_pnl_usd"] == pytest.approx(9.0)


def test_simulation_audit_has_one_row_per_profile_per_session(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    paper = PaperTradingService(database)
    session_id = paper.ensure_simulation_session()["id"]
    now = datetime.now(timezone.utc)
    insert_closed(database, position_id="audit-balanced", account_kind="paper", session_id=session_id, pnl=1.0, now=now)
    insert_closed(database, position_id="audit-aggressive", account_kind="shadow_aggressive", session_id=session_id, pnl=2.0, now=now)

    rows = paper.simulation_audit(limit_sessions=1)

    assert [row["profile"] for row in rows] == ["balanced", "aggressive", "conservative"]
    pnl = {row["profile"]: row["realized_pnl_usd"] for row in rows}
    assert pnl == {"balanced": pytest.approx(1.0), "aggressive": pytest.approx(2.0), "conservative": pytest.approx(0.0)}
