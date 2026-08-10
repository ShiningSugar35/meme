from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app.collector.models import Kline
from backend.app.config import Settings
from backend.app.database import Database, utc_now_iso
from backend.app.repositories.models import ModelRepository
from backend.app.repositories.samples import SampleRecord, SampleRepository
from backend.app.services.paper_position_monitor import PaperPositionMonitor
from backend.app.services.paper_trading import PaperTradingService
from backend.app.trading.simulator.types import ExecutionQuote, FailureCategory


class ExitQuoteProvider:
    def __init__(self, outcomes: list[bool] | None = None) -> None:
        self.outcomes = list(outcomes or [True] * 20)
        self.requests = []

    def quote(self, request):
        self.requests.append(request)
        success = self.outcomes.pop(0) if self.outcomes else True
        if not success:
            return ExecutionQuote(
                success=False,
                fill_price=None,
                gross_usd=0.0,
                fee_usd=0.0,
                network_fee_sol=0.001,
                slippage_bps=0.0,
                latency_ms=25,
                failure_category=FailureCategory.NETWORK,
                message="simulated network failure",
            )
        return ExecutionQuote(
            success=True,
            fill_price=request.reference_price,
            gross_usd=request.amount_usd,
            fee_usd=0.0,
            network_fee_sol=0.0,
            slippage_bps=0.0,
            latency_ms=25,
        )


class FakeKlineProvider:
    def __init__(self, klines: list[Kline]) -> None:
        self.items = klines
        self.calls: list[tuple[str, int, int]] = []

    async def klines(self, address: str, from_ts: int, to_ts: int):
        self.calls.append((address, from_ts, to_ts))
        return self.items


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        background_workers_enabled=False,
        simulation_enabled=True,
        paper_market_monitor_enabled=True,
    )


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "paper.db")
    database.initialize()
    return database


def seed_position(
    database: Database,
    *,
    address: str,
    position_id: str,
    account_kind: str,
    opened_at: datetime,
    entry_price: float = 1.0,
    liquidity: float = 10_000.0,
) -> None:
    samples = SampleRepository(database)
    samples.insert(
        SampleRecord(
            address=address,
            entry_time=int(opened_at.timestamp()),
            entry_price=entry_price,
            liquidity=liquidity,
            features={"age": 1.0},
        )
    )
    sample_id = database.fetch_one(
        "SELECT id FROM samples WHERE address=? ORDER BY id DESC LIMIT 1", (address,)
    )["id"]
    model_id = f"model-{position_id}"
    ModelRepository(database).register(
        {
            "id": model_id,
            "version": model_id,
            "algorithm": "logistic_regression",
            "status": "candidate",
            "early_stage": True,
            "trained_at": opened_at.isoformat(),
            "feature_names": ["age"],
            "parameters": {},
            "thresholds": {"aggressive": 0.2, "balanced": 0.4, "conservative": 0.8},
            "metrics": {},
            "artifact_path": "ml_models/fake.joblib",
        }
    )
    profile = (
        "aggressive"
        if account_kind == "shadow_aggressive"
        else "conservative"
        if account_kind == "shadow_conservative"
        else "balanced"
    )
    cursor = database.fetch_one("SELECT COALESCE(MAX(id), 0) + 1 AS id FROM predictions")
    prediction_id = int(cursor["id"])
    database.execute(
        """
        INSERT INTO predictions(id,sample_id,model_id,probability,profile,threshold,selected,predicted_at)
        VALUES(?,?,?,?,?,?,1,?)
        """,
        (prediction_id, sample_id, model_id, 0.9, profile, 0.4, opened_at.isoformat()),
    )
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,profile,status,prediction_id,model_id,
            entry_time,expires_at,invested_usd,token_amount,entry_price,
            stop_loss_price,take_profit_price,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            position_id,
            address,
            account_kind,
            profile,
            "open",
            prediction_id,
            model_id,
            opened_at.isoformat(),
            (opened_at + timedelta(hours=2)).isoformat(),
            50.0,
            50.0 / entry_price,
            entry_price,
            entry_price * 0.9,
            entry_price * 1.6,
            json.dumps({"entry_fee_usd": 0.0}),
        ),
    )
    database.set_runtime_state(
        f"portfolio_account:{account_kind}",
        {
            "cash_usd": 950.0,
            "sol_fee_reserve": 0.1,
            "initial_cash_usd": 1000.0,
            "initial_sol_fee_reserve": 0.1,
            "source": "test",
            "updated_at": utc_now_iso(),
        },
    )


@pytest.mark.asyncio
async def test_monitor_groups_same_token_and_same_bar_stop_loss_wins(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    opened = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
    seed_position(database, address="same-token", position_id="paper-1", account_kind="paper", opened_at=opened)
    seed_position(
        database,
        address="same-token",
        position_id="shadow-1",
        account_kind="shadow_aggressive",
        opened_at=opened,
    )
    quote_provider = ExitQuoteProvider()
    monitor = PaperPositionMonitor(
        database,
        settings,
        paper_service=PaperTradingService(database, settings, quote_provider=quote_provider),
    )
    market = FakeKlineProvider(
        [Kline(int((opened + timedelta(minutes=5)).timestamp()), 1.7, 0.8, 1.2)]
    )

    report = await monitor.run_cycle(
        market, now_ts=int((opened + timedelta(minutes=6)).timestamp())
    )

    assert report.checked_positions == 2
    assert report.market_requests == 1
    assert report.closed_positions == 2
    assert len(market.calls) == 1
    rows = database.fetch_all("SELECT status,exit_reason,net_pnl_usd FROM positions ORDER BY id")
    assert {row["exit_reason"] for row in rows} == {"stop_loss_0_9x"}
    assert all(row["status"] == "closed" for row in rows)
    assert all(row["net_pnl_usd"] == pytest.approx(-5.0) for row in rows)


@pytest.mark.asyncio
async def test_failed_exit_persists_trigger_and_retries_without_new_market_decision(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    opened = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
    seed_position(database, address="retry-token", position_id="paper-retry", account_kind="paper", opened_at=opened)
    quote_provider = ExitQuoteProvider([False, True])
    paper = PaperTradingService(database, settings, quote_provider=quote_provider)
    monitor = PaperPositionMonitor(database, settings, paper_service=paper)
    market = FakeKlineProvider(
        [Kline(int((opened + timedelta(minutes=5)).timestamp()), 1.7, 0.95, 1.6)]
    )

    first = await monitor.run_cycle(
        market, now_ts=int((opened + timedelta(minutes=6)).timestamp())
    )
    assert first.pending_positions == 1
    row = database.fetch_one("SELECT status,metadata_json FROM positions WHERE id='paper-retry'")
    assert row["status"] == "closing"
    pending = json.loads(row["metadata_json"])["paper_exit_pending"]
    assert pending["reason"] == "take_profit_1_6x"
    assert pending["attempt_count"] == 1
    assert pending["network_fee_sol_charged"] == pytest.approx(0.001)
    assert database.get_runtime_state("portfolio_account:paper")["sol_fee_reserve"] == pytest.approx(0.099)

    restarted_monitor = PaperPositionMonitor(database, settings, paper_service=paper)
    second = await restarted_monitor.run_cycle(
        market, now_ts=int((opened + timedelta(minutes=7)).timestamp())
    )
    assert second.closed_positions == 1
    assert len(market.calls) == 1
    final = database.fetch_one("SELECT status,exit_reason FROM positions WHERE id='paper-retry'")
    assert final == {"status": "closed", "exit_reason": "take_profit_1_6x"}


@pytest.mark.asyncio
async def test_timeout_uses_last_close_at_or_before_two_hours(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    opened = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
    seed_position(database, address="timeout-token", position_id="paper-timeout", account_kind="paper", opened_at=opened)
    quote_provider = ExitQuoteProvider()
    monitor = PaperPositionMonitor(
        database,
        settings,
        paper_service=PaperTradingService(database, settings, quote_provider=quote_provider),
    )
    market = FakeKlineProvider(
        [
            Kline(int((opened + timedelta(hours=1, minutes=59)).timestamp()), 1.2, 0.95, 1.1),
            Kline(int((opened + timedelta(hours=2)).timestamp()), 1.2, 0.95, 1.12),
        ]
    )

    report = await monitor.run_cycle(
        market, now_ts=int((opened + timedelta(hours=2, minutes=1)).timestamp())
    )

    assert report.closed_positions == 1
    row = database.fetch_one(
        "SELECT status,exit_reason,exit_price FROM positions WHERE id='paper-timeout'"
    )
    assert row["status"] == "closed"
    assert row["exit_reason"] == "timeout_2h"
    assert row["exit_price"] == pytest.approx(1.12)
