from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.repositories.models import ModelRepository
from backend.app.repositories.samples import SampleRecord, SampleRepository
from backend.app.services.paper_trading import PaperTradingService
from backend.app.trading.simulator.types import ExecutionQuote, FailureCategory


class FixedProvider:
    def __init__(self, quote: ExecutionQuote) -> None:
        self.result = quote

    def quote(self, request):
        return self.result


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "simulation.db")
    database.initialize()
    return database


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        background_workers_enabled=False,
        simulation_enabled=True,
    )


def seed_prediction(database: Database, now: datetime) -> tuple[int, int, str]:
    repo = SampleRepository(database)
    repo.insert(
        SampleRecord(
            address="session-token",
            entry_time=int(now.timestamp()),
            entry_price=1.0,
            liquidity=10_000.0,
            features={"age": 1.0},
        )
    )
    sample_id = database.fetch_one("SELECT id FROM samples ORDER BY id DESC LIMIT 1")["id"]
    model_id = "session-model"
    ModelRepository(database).register(
        {
            "id": model_id,
            "version": model_id,
            "algorithm": "logistic_regression",
            "status": "candidate",
            "early_stage": True,
            "trained_at": (now - timedelta(days=1)).isoformat(),
            "feature_names": ["age"],
            "parameters": {},
            "thresholds": {"aggressive": 0.2, "balanced": 0.4, "conservative": 0.8},
            "metrics": {},
            "artifact_path": "ml_models/test.joblib",
        }
    )
    database.execute(
        """
        INSERT INTO predictions(sample_id,model_id,probability,profile,threshold,selected,predicted_at)
        VALUES(?,?,0.9,'balanced',0.4,1,?)
        """,
        (sample_id, model_id, now.isoformat()),
    )
    prediction_id = database.fetch_one("SELECT id FROM predictions ORDER BY id DESC LIMIT 1")["id"]
    return int(sample_id), int(prediction_id), model_id


def test_simulation_session_initializes_three_equal_accounts_and_reset_changes_id(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    service = PaperTradingService(database, make_settings(tmp_path))

    first = service.simulation_status()
    same = service.simulation_status()
    reset = service.reset_simulation()

    assert first["session"]["id"] == same["session"]["id"]
    assert reset["session"]["id"] != first["session"]["id"]
    assert set(reset["accounts"]) == {"paper", "shadow_aggressive", "shadow_conservative"}
    for account in reset["accounts"].values():
        assert account["cash_usd"] == pytest.approx(1000.0)
        assert account["sol_fee_reserve"] == pytest.approx(0.1)
        assert account["open_positions"] == 0
        assert account["realized_pnl_usd"] == pytest.approx(0.0)
    history = service.simulation_history()
    assert len(history) == 2
    assert history[0]["id"] == reset["session"]["id"]
    assert history[0]["status"] == "active"
    assert history[0]["created_reason"] == "manual_reset"
    assert history[1]["id"] == first["session"]["id"]
    assert history[1]["status"] == "closed"
    assert history[1]["ended_at"] is not None


def test_simulation_reset_is_blocked_with_open_position(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    service = PaperTradingService(database, make_settings(tmp_path))
    session = service.ensure_simulation_session()
    now = datetime.now(timezone.utc)
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,profile,status,simulation_session_id,
            entry_time,expires_at,invested_usd
        ) VALUES('open-paper','token','paper','balanced','open',?,?,?,50)
        """,
        (session["id"], now.isoformat(), (now + timedelta(hours=2)).isoformat()),
    )

    with pytest.raises(ValueError, match="cannot reset"):
        service.reset_simulation()


def test_successful_open_persists_position_trade_and_account_in_same_session(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    sample_id, prediction_id, model_id = seed_prediction(database, now)
    service = PaperTradingService(
        database,
        settings,
        quote_provider=FixedProvider(
            ExecutionQuote(True, 1.0, 50.0, 0.5, 0.001, 0.0, 10)
        ),
    )

    result = service.open_from_prediction(
        sample_id=sample_id,
        prediction_id=prediction_id,
        model_id=model_id,
        profile="balanced",
        account="paper",
    )

    assert result.opened
    session = database.get_runtime_state("simulation_session")
    position = database.fetch_one(
        "SELECT simulation_session_id,status FROM positions WHERE id=?", (result.position_id,)
    )
    account = database.get_runtime_state("portfolio_account:paper")
    trade = database.fetch_one(
        "SELECT status,network_fee_sol FROM trades WHERE position_id=? AND side='buy'",
        (result.position_id,),
    )
    assert position == {"simulation_session_id": session["id"], "status": "open"}
    assert account["session_id"] == session["id"]
    assert account["cash_usd"] == pytest.approx(949.5)
    assert account["sol_fee_reserve"] == pytest.approx(0.099)
    assert trade == {"status": "confirmed", "network_fee_sol": pytest.approx(0.001)}


def test_failed_chain_execution_charges_network_fee_without_creating_position(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    sample_id, prediction_id, model_id = seed_prediction(database, now)
    service = PaperTradingService(
        database,
        settings,
        quote_provider=FixedProvider(
            ExecutionQuote(
                False,
                None,
                0.0,
                0.0,
                0.001,
                0.0,
                10,
                FailureCategory.CHAIN_REJECTED,
                "rejected",
            )
        ),
    )

    result = service.open_from_prediction(
        sample_id=sample_id,
        prediction_id=prediction_id,
        model_id=model_id,
        profile="balanced",
        account="paper",
    )

    assert not result.opened
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions")["n"] == 0
    account = database.get_runtime_state("portfolio_account:paper")
    assert account["cash_usd"] == pytest.approx(1000.0)
    assert account["sol_fee_reserve"] == pytest.approx(0.099)
    trade = database.fetch_one("SELECT status,network_fee_sol FROM trades")
    assert trade == {"status": "failed", "network_fee_sol": pytest.approx(0.001)}
