from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app.database import Database
from backend.app.repositories.models import ModelRepository
from backend.app.repositories.samples import SampleRecord, SampleRepository
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
    database.execute(
        "UPDATE active_model_slots SET selected_at='2026-08-01T00:00:00+00:00'"
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
    model_id: str | None = None,
) -> None:
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,strategy_key,status,simulation_session_id,model_id,
            entry_time,expires_at,invested_usd,exit_time,net_pnl_usd
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            position_id,
            f"token-{position_id}",
            account_kind,
            strategy_key,
            "closed",
            session_id,
            model_id,
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
    insert_closed(database, position_id="model1-recent", strategy_key="model_1", session_id=session_id, pnl=6.0, now=now, model_id="model-1")
    insert_closed(database, position_id="model1-old", strategy_key="model_1", session_id=session_id, pnl=-2.0, now=now - timedelta(days=2), model_id="model-1")
    insert_closed(database, position_id="model1-wrong-generation", strategy_key="model_1", session_id=session_id, pnl=99.0, now=now, model_id="model-2")
    insert_closed(database, position_id="model2-recent", strategy_key="model_2", session_id=session_id, pnl=9.0, now=now, model_id="model-2")
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,strategy_key,status,simulation_session_id,model_id,
            entry_time,expires_at,invested_usd,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "model1-open", "token-model1-open", "simulation", "model_1", "open", session_id, "model-1",
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


def test_model_card_quality_and_trade_count_are_scoped_to_current_generation(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    seed_top3(database)
    paper = PaperTradingService(database)
    session_id = paper.ensure_simulation_session()["id"]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    activated_at = now - timedelta(hours=1)
    database.execute(
        "UPDATE active_model_slots SET selected_at=? WHERE slot=1",
        (activated_at.isoformat(),),
    )
    samples = SampleRepository(database)
    prediction_ids: list[int] = []
    for index, (tag, selected) in enumerate(((1, 1), (0, 1), (1, 0)), start=1):
        entry = activated_at + timedelta(minutes=index * 10)
        samples.insert(
            SampleRecord(
                address=f"quality-token-{index}",
                token_type="new_creation",
                entry_time=int(entry.timestamp()),
                entry_price=1.0,
                liquidity=10_000.0,
                features={"age": float(index)},
                tag=tag,
                label_status="mature",
            )
        )
        sample_id = int(database.fetch_one("SELECT id FROM samples WHERE address=?", (f"quality-token-{index}",))["id"])
        database.execute(
            """
            INSERT INTO predictions(sample_id,model_id,probability,strategy_key,threshold,selected,predicted_at)
            VALUES(?, 'model-1', ?, 'model_1', 0.3, ?, ?)
            """,
            (sample_id, 0.9 if selected else 0.1, selected, entry.isoformat()),
        )
        prediction_ids.append(int(database.fetch_one(
            "SELECT id FROM predictions WHERE sample_id=? AND model_id='model-1'",
            (sample_id,),
        )["id"]))

    for index, prediction_id in enumerate(prediction_ids[:2], start=1):
        entry = activated_at + timedelta(minutes=index * 10)
        database.execute(
            """
            INSERT INTO positions(
                id,token_address,account_kind,strategy_key,status,simulation_session_id,
                sample_id,prediction_id,model_id,entry_time,expires_at,invested_usd,
                exit_time,exit_reason,net_pnl_usd
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"quality-position-{index}", f"quality-position-token-{index}", "simulation", "model_1", "closed",
                session_id, None, prediction_id, "model-1", entry.isoformat(),
                (entry + timedelta(hours=2)).isoformat(), 50.0, (entry + timedelta(minutes=30)).isoformat(),
                "take_profit_1_6x" if index == 1 else "sell_failed_retry_exhausted",
                20.0 if index == 1 else -50.0,
            ),
        )

    account = paper.simulation_status()["accounts"]["model_1"]
    assert account["trade_count"] == 2
    assert account["closed_positions"] == 2
    assert account["profit_count"] == 1
    assert account["loss_count"] == 1
    assert account["precision"] == pytest.approx(0.5)
    assert account["recall"] == pytest.approx(0.5)
    assert account["realized_pnl_usd"] == pytest.approx(-30.0)


def test_simulation_profit_and_loss_counts_use_requested_net_pnl_boundaries(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    paper = PaperTradingService(database)
    session_id = paper.ensure_simulation_session()["id"]
    now = datetime.now(timezone.utc)

    insert_closed(database, position_id="profit-edge", strategy_key="rules_only", session_id=session_id, pnl=10.0, now=now)
    insert_closed(database, position_id="loss-edge-excluded", strategy_key="rules_only", session_id=session_id, pnl=2.5, now=now)
    insert_closed(database, position_id="loss-edge-included", strategy_key="rules_only", session_id=session_id, pnl=2.49, now=now)

    account = paper.simulation_status()["accounts"]["rules_only"]
    assert account["trade_count"] == 3
    assert account["profit_count"] == 1
    assert account["loss_count"] == 1


def test_rules_only_quality_is_positive_prevalence_with_full_recall(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    paper = PaperTradingService(database)
    session = paper.ensure_simulation_session()
    started = datetime.fromisoformat(str(session["started_at"])).astimezone(timezone.utc)
    samples = SampleRepository(database)
    for index, tag in enumerate((1, 0, 1), start=1):
        samples.insert(
            SampleRecord(
                address=f"rules-quality-{index}",
                token_type="near_completion",
                entry_time=int((started + timedelta(seconds=index)).timestamp()),
                entry_price=1.0,
                liquidity=10_000.0,
                features={"age": float(index)},
                tag=tag,
                label_status="mature",
            )
        )

    account = paper.simulation_status()["accounts"]["rules_only"]
    assert account["precision"] == pytest.approx(2 / 3)
    assert account["recall"] == pytest.approx(1.0)


def test_simulation_audit_has_four_rows_per_session(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    seed_top3(database)
    paper = PaperTradingService(database)
    session_id = paper.ensure_simulation_session()["id"]
    now = datetime.now(timezone.utc)
    insert_closed(database, position_id="audit-model1", strategy_key="model_1", session_id=session_id, pnl=1.0, now=now, model_id="model-1")
    insert_closed(database, position_id="audit-model1-wrong-generation", strategy_key="model_1", session_id=session_id, pnl=99.0, now=now, model_id="model-2")
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
