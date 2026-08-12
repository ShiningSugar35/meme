from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.database import Database, utc_now_iso
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


def seed_sol_price(database: Database, at: datetime, price: float = 180.0) -> None:
    database.execute(
        "INSERT OR REPLACE INTO asset_usd_prices(asset,observed_at,price_usd,source,recorded_at) VALUES('SOL',?,?,?,?)",
        (int(at.timestamp()), price, "test", utc_now_iso()),
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
    sample_id = int(database.fetch_one("SELECT id FROM samples ORDER BY id DESC LIMIT 1")["id"])
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
            "thresholds": {"decision": 0.4},
            "metrics": {},
            "artifact_path": "ml_models/test.joblib",
        }
    )
    database.execute(
        """
        INSERT INTO predictions(sample_id,model_id,probability,strategy_key,threshold,selected,predicted_at)
        VALUES(?,?,0.9,'model_1',0.4,1,?)
        """,
        (sample_id, model_id, now.isoformat()),
    )
    prediction_id = int(database.fetch_one("SELECT id FROM predictions ORDER BY id DESC LIMIT 1")["id"])
    return sample_id, prediction_id, model_id


def test_simulation_session_initializes_four_equal_accounts_and_reset_changes_id(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    service = PaperTradingService(database, make_settings(tmp_path))

    first = service.simulation_status()
    same = service.simulation_status()
    reset = service.reset_simulation()

    assert first["session"]["id"] == same["session"]["id"]
    assert reset["session"]["id"] != first["session"]["id"]
    assert set(reset["accounts"]) == {"model_1", "model_2", "model_3", "rules_only"}
    for account in reset["accounts"].values():
        assert account["cash_usd"] == pytest.approx(1000.0)
        assert "sol_fee_reserve" not in account
        assert account["accounting_currency"] == "USD"
        assert account["open_positions"] == 0
        assert account["realized_pnl_usd"] == pytest.approx(0.0)
    history = service.simulation_history()
    assert len(history) == 2
    assert history[0]["id"] == reset["session"]["id"]
    assert history[0]["status"] == "active"
    assert history[0]["created_reason"] == "manual_reset"
    assert history[1]["id"] == first["session"]["id"]
    assert history[1]["status"] == "closed"


def test_simulation_reset_is_blocked_with_open_strategy_position(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    service = PaperTradingService(database, make_settings(tmp_path))
    session = service.ensure_simulation_session()
    now = datetime.now(timezone.utc)
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,strategy_key,status,simulation_session_id,
            entry_time,expires_at,invested_usd
        ) VALUES('open-model','token','simulation','model_1','open',?,?,?,50)
        """,
        (session["id"], now.isoformat(), (now + timedelta(hours=2)).isoformat()),
    )
    with pytest.raises(ValueError, match="cannot reset"):
        service.reset_simulation()


def test_successful_open_persists_position_trade_and_strategy_account(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    sample_id, prediction_id, model_id = seed_prediction(database, now)
    seed_sol_price(database, now, 180.0)
    seed_sol_price(database, now + timedelta(minutes=1), 999.0)
    service = PaperTradingService(
        database,
        settings,
        quote_provider=FixedProvider(ExecutionQuote(True, 1.0, 50.0, 0.5, 0.001, 0.0, 10)),
    )

    result = service.open_from_prediction(
        sample_id=sample_id,
        prediction_id=prediction_id,
        model_id=model_id,
        strategy_key="model_1",
    )

    assert result.opened
    session = database.get_runtime_state("simulation_session")
    position = database.fetch_one(
        "SELECT simulation_session_id,strategy_key,status FROM positions WHERE id=?",
        (result.position_id,),
    )
    account = database.get_runtime_state("portfolio_strategy:model_1")
    trade = database.fetch_one(
        "SELECT status,network_fee_sol,sol_usd_price,network_fee_usd FROM trades WHERE position_id=? AND side='buy'",
        (result.position_id,),
    )
    assert position == {"simulation_session_id": session["id"], "strategy_key": "model_1", "status": "open"}
    assert account["session_id"] == session["id"]
    assert account["cash_usd"] == pytest.approx(949.32)
    assert "sol_fee_reserve" not in account
    assert trade == {
        "status": "confirmed",
        "network_fee_sol": pytest.approx(0.001),
        "sol_usd_price": pytest.approx(180.0),
        "network_fee_usd": pytest.approx(0.18),
    }


def test_failed_chain_execution_charges_network_fee_without_creating_position(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    sample_id, prediction_id, model_id = seed_prediction(database, now)
    seed_sol_price(database, now, 180.0)
    service = PaperTradingService(
        database,
        settings,
        quote_provider=FixedProvider(
            ExecutionQuote(False, None, 0.0, 0.0, 0.001, 0.0, 10, FailureCategory.CHAIN_REJECTED, "rejected")
        ),
    )

    result = service.open_from_prediction(
        sample_id=sample_id,
        prediction_id=prediction_id,
        model_id=model_id,
        strategy_key="model_1",
    )

    assert not result.opened
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions")["n"] == 0
    account = database.get_runtime_state("portfolio_strategy:model_1")
    assert account["cash_usd"] == pytest.approx(999.82)
    assert "sol_fee_reserve" not in account
    trade = database.fetch_one("SELECT status,network_fee_sol,sol_usd_price,network_fee_usd FROM trades")
    assert trade == {
        "status": "failed",
        "network_fee_sol": pytest.approx(0.001),
        "sol_usd_price": pytest.approx(180.0),
        "network_fee_usd": pytest.approx(0.18),
    }


def test_rules_only_can_open_without_prediction(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    service = PaperTradingService(database, make_settings(tmp_path), quote_provider=FixedProvider(ExecutionQuote(True, 1.0, 50.0, 0.0, 0.0, 0.0, 1)))
    now = datetime.now(timezone.utc)
    SampleRepository(database).insert(SampleRecord(address="rules-token", entry_time=int(now.timestamp()), entry_price=1.0, liquidity=10_000.0, features={"age": 3.0}))
    sample_id = int(database.fetch_one("SELECT id FROM samples WHERE address='rules-token'")["id"])
    result = service.open_rule_only(sample_id=sample_id)
    assert result.opened
    row = database.fetch_one("SELECT strategy_key,prediction_id,sample_id FROM positions WHERE id=?", (result.position_id,))
    assert row == {"strategy_key": "rules_only", "prediction_id": None, "sample_id": sample_id}
