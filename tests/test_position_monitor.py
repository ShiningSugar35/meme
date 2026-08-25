from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.collector.constants import SOL_WRAPPED_MINT
from backend.app.collector.errors import CollectorRateLimitError
from backend.app.collector.models import Kline
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.services.paper_position_monitor import PaperPositionMonitor
from backend.app.services.paper_trading import PaperTradingService
from backend.app.services.platform_configuration import PlatformConfigurationService
from backend.app.services.position_monitor import (
    DexScreenerPositionMarketProvider, PositionMonitorService, PositionMonitorWorker,
)
from backend.app.services.sol_price import CoinbaseSolKlineProvider
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




class FailingRateLimitedMarketProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def token_bundle(self, address: str):
        self.calls.append(address)
        raise CollectorRateLimitError("all GMGN slots cooling", reset_at=1_800_000_000)


class PositionFallbackProvider(CurrentMarketProvider):
    async def token_bundle(self, address: str):
        self.calls.append(address)
        return {
            "token_info": {
                "data": {
                    "address": address,
                    "price_usd": self.price,
                    "liquidity_usd": self.liquidity,
                    "market_cap": 123_456.0,
                }
            },
            "_market_source": "dexscreener_public_token_pairs",
        }


class _DexResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _DexClient:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.paths: list[str] = []

    async def get(self, path: str):
        self.paths.append(path)
        return _DexResponse(self.payload)


@pytest.mark.asyncio
async def test_dexscreener_position_fallback_selects_highest_liquidity_base_pair() -> None:
    address = "fallback-mint"
    client = _DexClient([
        {
            "chainId": "solana",
            "baseToken": {"address": address},
            "quoteToken": {"address": "quote-a"},
            "priceUsd": "0.50",
            "liquidity": {"usd": 5000},
            "marketCap": 100000,
        },
        {
            "chainId": "solana",
            "baseToken": {"address": address},
            "quoteToken": {"address": "quote-b"},
            "priceUsd": "0.55",
            "liquidity": {"usd": 9000},
            "marketCap": 110000,
        },
        {
            "chainId": "solana",
            "baseToken": {"address": "other"},
            "quoteToken": {"address": address},
            "priceUsd": "999",
            "liquidity": {"usd": 999999},
        },
    ])
    provider = DexScreenerPositionMarketProvider(client=client, requests_per_second=100.0)

    bundle = await provider.token_bundle(address)

    data = bundle["token_info"]["data"]
    assert data["price_usd"] == pytest.approx(0.55)
    assert data["liquidity_usd"] == pytest.approx(9000.0)
    assert bundle["_market_source"] == "dexscreener_public_token_pairs"
    assert client.paths == [f"/token-pairs/v1/solana/{address}"]


@pytest.mark.asyncio
async def test_simulation_position_prefers_dexscreener_without_spending_gmgn_quota(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed_paper(database, position_id="paper-fallback", address="fallback-token", opened=opened)
    gmgn = FailingRateLimitedMarketProvider()
    fallback = PositionFallbackProvider(0.95, liquidity=12_000.0)
    service = PositionMonitorService(database, settings)

    report = await service.run_cycle(gmgn, fallback_provider=fallback)

    assert report.market_requests == 1
    assert report.market_data_failures == 0
    assert report.market_fallbacks == 1
    assert report.blocked_positions == 0
    assert gmgn.calls == []
    assert fallback.calls == ["fallback-token"]
    row = database.fetch_one("SELECT status,metadata_json FROM positions WHERE id='paper-fallback'")
    assert row["status"] == "open"
    snapshot = json.loads(row["metadata_json"])["market_snapshot"]
    assert snapshot["price"] == pytest.approx(0.95)
    assert snapshot["market_data_source"] == "dexscreener_public_token_pairs"
    assert snapshot["market_cap_source"] == "dexscreener_public_token_pairs_direct"


@pytest.mark.asyncio
async def test_live_position_never_uses_public_dexscreener_fallback(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed_live(database, position_id="live-no-public-fallback", address="live-token", opened=opened)
    gmgn = FailingRateLimitedMarketProvider()
    fallback = PositionFallbackProvider(0.89, liquidity=12_000.0)
    service = PositionMonitorService(database, settings)

    report = await service.run_cycle(gmgn, fallback_provider=fallback)

    assert gmgn.calls == ["live-token"]
    assert fallback.calls == []
    assert report.market_data_failures == 1
    assert report.blocked_positions == 1

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


class _CoinbaseResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class _CoinbaseClient:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def get(self, path: str, *, params=None):
        self.calls.append((path, dict(params or {})))
        return _CoinbaseResponse(self.payload)


@pytest.mark.asyncio
async def test_coinbase_sol_fee_provider_uses_only_closed_one_minute_candles() -> None:
    target = 1_800_000_180
    payload = [
        [target - 180, 99.0, 102.0, 100.0, 101.0, 123.0],
        [target - 60, 100.0, 103.0, 101.0, 102.0, 456.0],
        [target, 101.0, 104.0, 102.0, 103.0, 789.0],
    ]
    client = _CoinbaseClient(payload)
    provider = CoinbaseSolKlineProvider(client=client)

    rows = await provider.klines(SOL_WRAPPED_MINT, target - 300, target)

    assert [row.timestamp for row in rows] == [target - 120, target]
    assert [row.close for row in rows] == [101.0, 102.0]
    assert client.calls[0][0] == "/products/SOL-USD/candles"


class _FailingSolKlineProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def klines(self, address: str, from_ts: int, to_ts: int):
        self.calls += 1
        raise RuntimeError("provider unavailable")


class _HealthySolKlineProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def klines(self, address: str, from_ts: int, to_ts: int):
        self.calls += 1
        assert address == SOL_WRAPPED_MINT
        return [Kline(timestamp=to_ts - 10, high=102.0, low=100.0, close=101.25, open=100.5)]


@pytest.mark.asyncio
async def test_worker_prefers_coinbase_for_fee_time_sol_usd_without_gmgn_call(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed_paper(database, position_id="paper-sol-coinbase", address="sol-fallback-token", opened=opened)
    worker = PositionMonitorWorker(database, settings)
    gmgn = _FailingSolKlineProvider()
    coinbase = _HealthySolKlineProvider()
    worker._sol_provider = gmgn
    worker._sol_fallback_provider = coinbase

    error = await worker._refresh_sol_usd_for_open_simulation_positions()

    assert error is None
    assert coinbase.calls == 1
    assert gmgn.calls == 0
    row = database.fetch_one(
        "SELECT price_usd,source FROM asset_usd_prices WHERE asset='SOL' ORDER BY observed_at DESC LIMIT 1"
    )
    assert row["price_usd"] == pytest.approx(101.25)
    assert row["source"] == "coinbase_exchange_public_sol_usd_1m"
    status = database.get_runtime_state("sol_usd_price_status")
    assert status["state"] == "ready"
    assert status["source"] == "coinbase_exchange_public_sol_usd_1m"


@pytest.mark.asyncio
async def test_worker_uses_gmgn_only_if_coinbase_sol_fee_source_fails(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    seed_paper(database, position_id="paper-sol-gmgn-fallback", address="sol-gmgn-token", opened=opened)
    worker = PositionMonitorWorker(database, settings)
    gmgn = _HealthySolKlineProvider()
    coinbase = _FailingSolKlineProvider()
    worker._sol_provider = gmgn
    worker._sol_fallback_provider = coinbase

    error = await worker._refresh_sol_usd_for_open_simulation_positions()

    assert error is None
    assert coinbase.calls == 1
    assert gmgn.calls == 1
    row = database.fetch_one(
        "SELECT source FROM asset_usd_prices WHERE asset='SOL' ORDER BY observed_at DESC LIMIT 1"
    )
    assert row["source"] == "gmgn_1m_kline"
