from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app.database import Database
from backend.app.repositories.models import ModelRepository
from backend.app.services.dashboard import DashboardService
from backend.app.services.paper_trading import PaperTradingService


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "dashboard.db")
    database.initialize()
    return database


def seed_top3(database: Database) -> None:
    models = ModelRepository(database)
    for slot, algorithm in enumerate(("random_forest", "extra_trees", "decision_tree"), start=1):
        model_id = f"model-{slot}"
        models.register(
            {
                "id": model_id,
                "version": model_id,
                "algorithm": algorithm,
                "status": "candidate",
                "early_stage": True,
                "trained_at": f"2026-08-1{slot}T10:00:00+00:00",
                "feature_names": ["age"],
                "parameters": {},
                "thresholds": {"decision": 0.2 + 0.1 * slot},
                "metrics": {"composite_score": 0.5 - slot * 0.05},
                "artifact_path": f"ml_models/{model_id}.joblib",
            }
        )
    models.set_active_models(
        [
            {"id": f"model-{slot}", "composite_score": 0.5 - slot * 0.05, "threshold": 0.2 + 0.1 * slot, "metrics": {}}
            for slot in (1, 2, 3)
        ]
    )


def insert_closed(
    database: Database,
    *,
    position_id: str,
    strategy_key: str | None,
    session_id: str | None,
    pnl: float,
    now: datetime,
    account_kind: str = "simulation",
) -> None:
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,strategy_key,status,simulation_session_id,
            entry_time,expires_at,invested_usd,exit_time,net_pnl_usd
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            position_id,
            f"token-{position_id}",
            account_kind,
            strategy_key,
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
    insert_closed(database, position_id="old-model", strategy_key="model_1", session_id=old_session, pnl=100.0, now=now)
    current = paper.reset_simulation()["session"]["id"]
    insert_closed(database, position_id="current-model", strategy_key="model_1", session_id=current, pnl=7.5, now=now)
    insert_closed(database, position_id="live-position", strategy_key=None, session_id=None, pnl=2.5, now=now, account_kind="live")

    dashboard = DashboardService(database).overview()

    assert dashboard["simulation"]["session"]["id"] == current
    assert dashboard["live_realized_pnl_usd"] == pytest.approx(2.5)
    assert dashboard["simulation"]["accounts"]["model_1"]["realized_pnl_usd"] == pytest.approx(7.5)
    assert dashboard["pnl"]["seven_days"]["model_1"] == pytest.approx(7.5)
    assert dashboard["pnl"]["seven_days"]["live"] == pytest.approx(2.5)
    assert dashboard["pnl"]["seven_days"]["total"] == pytest.approx(10.0)
    assert all(row["daily_pnl"] != pytest.approx(100.0) for row in dashboard["equity_curve"])

    service = DashboardService(database)
    current_positions = service.list_positions(include_closed=True)
    all_positions = service.list_positions(include_closed=True, include_previous_simulation_sessions=True)
    assert {row["id"] for row in current_positions} == {"current-model", "live-position"}
    assert {row["id"] for row in all_positions} == {"old-model", "current-model", "live-position"}


def test_portfolio_view_filters_strategy_time_and_exposes_market_snapshot(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    seed_top3(database)
    paper = PaperTradingService(database)
    session_id = paper.ensure_simulation_session()["id"]
    now = datetime.now(timezone.utc)
    insert_closed(database, position_id="model1-recent", strategy_key="model_1", session_id=session_id, pnl=6.0, now=now)
    insert_closed(database, position_id="model1-old", strategy_key="model_1", session_id=session_id, pnl=-2.0, now=now - timedelta(days=2))
    insert_closed(database, position_id="model2-recent", strategy_key="model_2", session_id=session_id, pnl=9.0, now=now)
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,strategy_key,status,simulation_session_id,
            entry_time,expires_at,invested_usd,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "model1-open", "token-model1-open", "simulation", "model_1", "open", session_id,
            now.isoformat(), (now + timedelta(hours=2)).isoformat(), 40.0,
            '{"market_snapshot":{"price":1.2,"liquidity_usd":12345.0,"market_cap_usd":98765.0,"as_of":"2026-08-11T00:00:00+00:00"}}',
        ),
    )

    view = DashboardService(database).portfolio_view(
        mode="simulation", strategy="model_1", page=1, page_size=10,
        start_at=(now - timedelta(hours=12)).isoformat(),
    )

    assert view["history"]["total"] == 1
    assert view["model_alias"] == "sim_20260811"
    assert [row["id"] for row in view["history"]["items"]] == ["model1-recent"]
    assert [row["id"] for row in view["current"]] == ["model1-open"]
    assert view["current"][0]["current_liquidity_usd"] == pytest.approx(12345.0)
    assert view["current"][0]["current_market_cap_usd"] == pytest.approx(98765.0)
    assert view["accounts"]["model_1"]["realized_pnl_usd"] == pytest.approx(4.0)
    assert view["accounts"]["model_2"]["realized_pnl_usd"] == pytest.approx(9.0)


def test_simulation_audit_has_four_rows_per_session(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    seed_top3(database)
    paper = PaperTradingService(database)
    session_id = paper.ensure_simulation_session()["id"]
    now = datetime.now(timezone.utc)
    insert_closed(database, position_id="audit-model1", strategy_key="model_1", session_id=session_id, pnl=1.0, now=now)
    insert_closed(database, position_id="audit-rules", strategy_key="rules_only", session_id=session_id, pnl=2.0, now=now)

    rows = paper.simulation_audit(limit_sessions=1)

    assert [row["strategy_key"] for row in rows] == ["model_1", "model_2", "model_3", "rules_only"]
    assert rows[0]["first_entry_time"] == (now - timedelta(hours=1)).isoformat()
    assert rows[0]["last_exit_time"] == now.isoformat()
    assert rows[3]["model_label"] == "不用模型"
    pnl = {row["strategy_key"]: row["realized_pnl_usd"] for row in rows}
    assert pnl == {
        "model_1": pytest.approx(1.0),
        "model_2": pytest.approx(0.0),
        "model_3": pytest.approx(0.0),
        "rules_only": pytest.approx(2.0),
    }
