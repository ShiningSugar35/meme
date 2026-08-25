from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.services.paper_position_monitor import PaperPositionMonitor
from backend.app.services.paper_trading import PaperTradingService
from backend.app.services.platform_configuration import PlatformConfigurationService
from backend.app.services.position_monitor import PositionMonitorService, PositionMonitorWorker
from backend.app.trading.live.models import ExecutionResult, OrderStatus
from backend.app.trading.simulator.jupiter_probe import RouteProbeResult


class CurrentMarketProvider:
    def __init__(self, price: float, *, liquidity: float = 10_000.0) -> None:
        self.price = price
        self.liquidity = liquidity
        self.calls: list[str] = []

    async def token_bundle(self, address: str):
        self.calls.append(address)
        return {
            "token_info": {
                "data": {
                    "address": address,
                    "price": self.price,
                    "liquidity": self.liquidity,
                    "decimals": 6,
                    "circulating_supply": 100_000.0,
                }
            }
        }


class VariableDelayMarketProvider:
    def __init__(self, delays: dict[str, float], *, price: float = 0.89, liquidity: float = 10_000.0) -> None:
        self.delays = delays
        self.price = price
        self.liquidity = liquidity

    async def token_bundle(self, address: str):
        await asyncio.sleep(self.delays.get(address, 0.0))
        return {
            "token_info": {
                "data": {
                    "address": address,
                    "price": self.price,
                    "liquidity": self.liquidity,
                    "decimals": 6,
                }
            }
        }


class FixedRouteProbe:
    def __init__(self, out_amount_raw: int = 44_000_000, *, price_impact_pct: float | None = None) -> None:
        self.out_amount_raw = out_amount_raw
        self.price_impact_pct = price_impact_pct
        self.calls: list[dict] = []

    async def quote_sell(self, **kwargs):
        self.calls.append(kwargs)
        return RouteProbeResult(
            "quoted",
            "jupiter-test",
            out_amount_raw=self.out_amount_raw,
            price_impact_pct=self.price_impact_pct,
            route_count=1,
        )


class DelayedRouteProbe:
    def __init__(self, delay_seconds: float = 0.12, out_amount_raw: int = 44_000_000) -> None:
        self.delay_seconds = delay_seconds
        self.out_amount_raw = out_amount_raw
        self.calls: list[dict] = []

    async def quote_sell(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(self.delay_seconds)
        return RouteProbeResult(
            "quoted",
            "jupiter-test",
            out_amount_raw=self.out_amount_raw,
            route_count=1,
            latency_ms=int(round(self.delay_seconds * 1000)),
            quoted_at=datetime.now(timezone.utc).isoformat(),
        )


class FixedSolPrice:
    def price_at(self, occurred_at):
        return SimpleNamespace(
            price_usd=180.0,
            observed_at=int(occurred_at.timestamp()),
            source="test",
        )


class RefreshingSolPrice:
    def __init__(self) -> None:
        self.fresh = False
        self.refresh_calls = 0

    def price_at(self, occurred_at, *, max_age_seconds=None):
        if not self.fresh:
            return None
        return SimpleNamespace(price_usd=180.0, observed_at=int(occurred_at), source="test-refresh")

    async def refresh(self, provider, *, now_ts=None):
        self.refresh_calls += 1
        self.fresh = True
        return SimpleNamespace(price_usd=180.0, observed_at=int(now_ts or time.time()), source="test-refresh")


class FakeLiveService:
    def __init__(self) -> None:
        self.intents = []

    async def execute(self, intent):
        self.intents.append(intent)
        return ExecutionResult(
            client_order_id=intent.client_order_id,
            order_id="order-1",
            status=OrderStatus.CONFIRMED,
            attempts=1,
            tx_hash="tx-1",
        )


def make_settings(tmp_path: Path, *, dry_run: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        background_workers_enabled=False,
        simulation_enabled=True,
        paper_market_monitor_enabled=True,
        position_monitor_enabled=True,
        position_monitor_poll_seconds=4.0,
        dry_run=dry_run,
        wallet_public_key="wallet-test",
    )


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "monitor.db")
    database.initialize()
    return database


def seed_paper(database: Database, *, position_id: str, address: str, opened: datetime, strategy_key: str = "model_1") -> None:
    session_id = PaperTradingService(database).ensure_simulation_session()["id"]
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,strategy_key,status,simulation_session_id,
            entry_time,expires_at,invested_usd,token_amount,entry_price,
            stop_loss_price,take_profit_price,metadata_json
        ) VALUES(?,?, 'simulation',?,'open',?,?,?,?,?,?,?,?, '{}')
        """,
        (
            position_id,
            address,
            strategy_key,
            session_id,
            opened.isoformat(),
            (opened + timedelta(hours=1)).isoformat(),
            50.0,
            50.0,
            1.0,
            0.9,
            1.6,
        ),
    )


def seed_live(database: Database, *, position_id: str, address: str, opened: datetime) -> None:
    metadata = {
        "token_amount_raw": "50000000",
        "exit_output_token": "usdc-test",
    }
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,strategy_key,status,
            entry_time,expires_at,invested_usd,token_amount,entry_price,
            stop_loss_price,take_profit_price,metadata_json
        ) VALUES(?,?, 'live',NULL,'open',?,?,?,?,?,?,?,?)
        """,
        (
            position_id,
            address,
            opened.isoformat(),
            (opened + timedelta(hours=1)).isoformat(),
            50.0,
            50.0,
            1.0,
            0.9,
            1.6,
            json.dumps(metadata),
        ),
    )


@pytest.mark.asyncio
async def test_current_price_above_stop_stays_open_without_kline_lookup(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed_paper(database, position_id="paper-open", address="same-token", opened=opened)
    provider = CurrentMarketProvider(0.95)
    paper = PaperTradingService(database, settings, sol_price_service=FixedSolPrice())
    service = PositionMonitorService(
        database,
        settings,
        paper_service=paper,
        paper_monitor=PaperPositionMonitor(database, settings, paper_service=paper),
    )

    report = await service.run_cycle(provider)

    assert report.checked_positions == 1
    assert report.market_requests == 1
    assert provider.calls == ["same-token"]
    row = database.fetch_one("SELECT status,exit_reason FROM positions WHERE id='paper-open'")
    assert row == {"status": "open", "exit_reason": None}


@pytest.mark.asyncio
async def test_current_price_stop_triggers_same_cycle_executable_quote(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed_paper(database, position_id="paper-stop", address="stop-token", opened=opened)
    provider = CurrentMarketProvider(0.89)
    route_probe = FixedRouteProbe(out_amount_raw=44_000_000, price_impact_pct=0.0123)
    paper = PaperTradingService(database, settings, sol_price_service=FixedSolPrice())
    paper_monitor = PaperPositionMonitor(
        database,
        settings,
        paper_service=paper,
        route_probe=route_probe,
    )
    service = PositionMonitorService(
        database,
        settings,
        paper_service=paper,
        paper_monitor=paper_monitor,
    )

    report = await service.run_cycle(provider)

    assert report.paper_closed == 1
    assert len(route_probe.calls) == 1
    row = database.fetch_one(
        "SELECT status,exit_reason,exit_price,metadata_json FROM positions WHERE id='paper-stop'"
    )
    assert row["status"] == "closed"
    assert row["exit_reason"] == "stop_loss_0_9x"
    assert row["exit_price"] == pytest.approx(0.88)
    metadata = json.loads(row["metadata_json"])
    assert metadata["execution_policy_version"] == "h1_route_aware_v1"
    assert metadata["exit_route_probe"]["state"] == "quoted"
    assert metadata["exit_trigger_at"] >= int(opened.timestamp())
    assert metadata["exit_execution_deviation_bps"] == pytest.approx((0.88 - 0.89) / 0.89 * 10_000)
    sell = database.fetch_one(
        "SELECT slippage_bps,slippage_cost_usd,requested_amount FROM trades "
        "WHERE position_id='paper-stop' AND side='sell'"
    )
    assert sell["slippage_bps"] == pytest.approx(123.0)
    assert sell["slippage_cost_usd"] == pytest.approx(float(sell["requested_amount"]) * 0.0123)


@pytest.mark.asyncio
async def test_live_current_price_trigger_respects_dry_run_gate(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path, dry_run=True)
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed_live(database, position_id="live-gated", address="live-token", opened=opened)
    database.set_runtime_state("live_trading_enabled", True)
    live = FakeLiveService()
    service = PositionMonitorService(database, settings, live_service=live)

    report = await service.run_cycle(CurrentMarketProvider(0.89))

    assert report.blocked_positions == 1
    assert live.intents == []
    row = database.fetch_one("SELECT status,metadata_json FROM positions WHERE id='live-gated'")
    assert row["status"] == "open"
    metadata = json.loads(row["metadata_json"])
    assert metadata["last_live_exit_signal"]["blocked_reason"] == "live_execution_not_armed"


@pytest.mark.asyncio
async def test_armed_live_trigger_executes_asynchronously_without_blocking_cycle(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path, dry_run=False)
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed_live(database, position_id="live-exit", address="live-token", opened=opened)
    database.set_runtime_state("live_trading_enabled", True)
    live = FakeLiveService()
    service = PositionMonitorService(database, settings, live_service=live)

    report = await service.run_cycle(CurrentMarketProvider(0.89))
    assert report.live_triggered == 1
    await service.shutdown()

    assert len(live.intents) == 1
    row = database.fetch_one("SELECT status,exit_reason,metadata_json FROM positions WHERE id='live-exit'")
    assert row["status"] == "closed"
    assert row["exit_reason"] == "stop_loss_0_9x"
    metadata = json.loads(row["metadata_json"])
    assert metadata["live_exit_execution"]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_same_asset_paper_exits_quote_concurrently_and_record_execution_delay(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    for strategy in ("model_1", "model_2", "model_3", "rules_only"):
        seed_paper(database, position_id=f"paper-{strategy}", address="shared-asset", opened=opened, strategy_key=strategy)
    provider = CurrentMarketProvider(0.89)
    route_probe = DelayedRouteProbe(delay_seconds=0.12)
    paper = PaperTradingService(database, settings, sol_price_service=FixedSolPrice())
    paper_monitor = PaperPositionMonitor(database, settings, paper_service=paper, route_probe=route_probe)
    service = PositionMonitorService(database, settings, paper_service=paper, paper_monitor=paper_monitor)
    config_env = tmp_path / "monitor-config.env"
    config_env.write_text(
        "JUPITER_API_KEY_1=one\nJUPITER_API_KEY_2=two\nJUPITER_API_KEY_3=three\nJUPITER_API_KEY_4=four\n",
        encoding="utf-8",
    )
    service.configuration = PlatformConfigurationService(database, env_path=config_env)

    started = time.monotonic()
    report = await service.run_cycle(provider)
    elapsed = time.monotonic() - started

    assert report.paper_closed == 4
    assert report.market_requests == 1
    assert len(route_probe.calls) == 4
    assert elapsed < 0.35
    rows = database.fetch_all("SELECT metadata_json FROM positions WHERE strategy_key IN ('model_1','model_2','model_3','rules_only') ORDER BY id")
    assert len(rows) == 4
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        assert metadata["exit_quote_latency_ms"] == 120
        assert metadata["exit_execution_delay_ms"] >= 0
        assert isinstance(metadata["exit_execution_deviation_bps"], float)
        assert metadata["exit_fee_occurred_at"] == metadata["exit_route_probe"]["quoted_at"]

@pytest.mark.asyncio
async def test_fast_market_response_exits_before_slow_asset_returns(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed_paper(database, position_id="paper-fast", address="fast-token", opened=opened)
    seed_paper(database, position_id="paper-slow", address="slow-token", opened=opened)
    provider = VariableDelayMarketProvider({"fast-token": 0.01, "slow-token": 0.30})
    route_probe = FixedRouteProbe(out_amount_raw=44_000_000, price_impact_pct=0.01)
    paper = PaperTradingService(database, settings, sol_price_service=FixedSolPrice())
    paper_monitor = PaperPositionMonitor(
        database, settings, paper_service=paper, route_probe=route_probe
    )
    service = PositionMonitorService(
        database, settings, paper_service=paper, paper_monitor=paper_monitor
    )

    task = asyncio.create_task(service.run_cycle(provider))
    await asyncio.sleep(0.10)

    assert database.fetch_one("SELECT status FROM positions WHERE id='paper-fast'")["status"] == "closed"
    assert database.fetch_one("SELECT status FROM positions WHERE id='paper-slow'")["status"] == "open"

    report = await task
    assert report.paper_closed == 2
    assert database.fetch_one("SELECT status FROM positions WHERE id='paper-slow'")["status"] == "closed"


@pytest.mark.asyncio
async def test_worker_refreshes_sol_fee_fact_independently_of_collector(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed_paper(database, position_id="paper-sol-refresh", address="sol-refresh-token", opened=opened)
    worker = PositionMonitorWorker(database, settings)
    fake = RefreshingSolPrice()
    worker._sol_price = fake
    worker._sol_provider = object()

    first = await worker._refresh_sol_usd_for_open_simulation_positions()
    second = await worker._refresh_sol_usd_for_open_simulation_positions()

    assert first is None
    assert second is None
    assert fake.refresh_calls == 1
