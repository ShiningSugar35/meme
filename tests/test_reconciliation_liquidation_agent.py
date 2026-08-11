from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app.api.routes import router
from backend.app.config import Settings
from backend.app.database import Database, utc_now_iso
from backend.app.services.agent_service import AgentService
from backend.app.services.liquidation import LiquidationService
from backend.app.services.order_journal import SqliteOrderJournal
from backend.app.services.paper_trading import PaperTradingService
from backend.app.services.reconciliation import StartupReconciliationService
from backend.app.services.runtime import RuntimeService
from backend.app.trading.live.models import (
    ExecutionResult,
    OrderSnapshot,
    OrderStatus,
    Submission,
    SwapIntent,
    TradeSide,
)
from backend.app.trading.simulator.types import ExecutionQuote


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.initialize()
    return database


def make_settings(tmp_path: Path, *, dry_run: bool = False) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        dry_run=dry_run,
        wallet_public_key="wallet-address-123456789",
        trading_provider="gmgn_cli",
        background_workers_enabled=False,
    )


def make_intent(client_order_id: str) -> SwapIntent:
    return SwapIntent(
        chain="sol",
        wallet_address="wallet-address-123456789",
        input_token="SOL",
        output_token="TOKEN",
        input_amount_raw="1000000",
        side=TradeSide.BUY,
        client_order_id=client_order_id,
    )


class QueryOnlyProvider:
    name = "fake_query"

    def __init__(self, snapshots: dict[str, OrderSnapshot] | None = None) -> None:
        self.snapshots = snapshots or {}
        self.queries: list[str] = []

    async def get_order(self, order_id: str) -> OrderSnapshot:
        self.queries.append(order_id)
        return self.snapshots[order_id]


class FixedQuoteProvider:
    def quote(self, request) -> ExecutionQuote:
        return ExecutionQuote(
            success=True,
            fill_price=request.reference_price * 0.99,
            gross_usd=request.amount_usd,
            fee_usd=0.10,
            network_fee_sol=0.00001,
            slippage_bps=100.0,
            latency_ms=25,
        )


class SequencedLiveExecutor:
    def __init__(self, statuses: list[OrderStatus]) -> None:
        self.statuses = list(statuses)
        self.client_order_ids: list[str] = []

    async def execute(self, intent: SwapIntent, *, allow_authorized_exit: bool = False) -> ExecutionResult:
        assert allow_authorized_exit is True
        self.client_order_ids.append(intent.client_order_id)
        status = self.statuses.pop(0)
        return ExecutionResult(
            client_order_id=intent.client_order_id,
            order_id="provider-order-1",
            status=status,
            attempts=1,
            tx_hash="tx-1" if status is OrderStatus.CONFIRMED else None,
        )


def insert_sample(database: Database, *, address: str, entry_time: int, price: float = 1.0) -> None:
    now = utc_now_iso()
    database.execute(
        """
        INSERT INTO samples(
            sample_key, address, entry_time, entry_price, liquidity,
            features_json, collected_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (f"sample-{address}-{entry_time}", address, entry_time, price, 10_000.0, "{}", now, now),
    )


def insert_position(
    database: Database,
    *,
    position_id: str,
    address: str,
    account_kind: str,
    metadata_json: str = "{}",
) -> None:
    now = datetime.now(timezone.utc)
    profile = "balanced" if account_kind in {"paper", "live"} else (
        "aggressive" if account_kind == "shadow_aggressive" else "conservative"
    )
    database.execute(
        """
        INSERT INTO positions(
            id, token_address, account_kind, profile, status, entry_time, expires_at,
            invested_usd, token_amount, entry_price, stop_loss_price, take_profit_price,
            metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            position_id,
            address,
            account_kind,
            profile,
            "open",
            now.isoformat(),
            (now + timedelta(hours=2)).isoformat(),
            50.0,
            50.0,
            1.0,
            0.9,
            1.6,
            metadata_json,
        ),
    )


@pytest.mark.asyncio
async def test_startup_reconciliation_queries_original_provider_order_only(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    journal = SqliteOrderJournal(database)
    await journal.reserve(make_intent("client-confirm"))
    await journal.mark_submitted(
        "client-confirm",
        Submission("order-1", OrderStatus.PENDING),
        0,
    )
    provider = QueryOnlyProvider(
        {"order-1": OrderSnapshot("order-1", OrderStatus.CONFIRMED, tx_hash="tx-1")}
    )

    report = await StartupReconciliationService(
        database,
        make_settings(tmp_path),
        provider=provider,
    ).reconcile_all_pending()

    assert report.confirmed == 1
    assert report.remaining_unknown == 0
    assert provider.queries == ["order-1"]
    row = database.fetch_one("SELECT status, journal_state FROM trades WHERE client_order_id='client-confirm'")
    assert row == {"status": "confirmed", "journal_state": "terminal"}


@pytest.mark.asyncio
async def test_submission_started_trade_stays_unresolved_even_in_dry_run(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    journal = SqliteOrderJournal(database)
    await journal.reserve(make_intent("client-ambiguous"))
    await journal.mark_submission_started("client-ambiguous", 0)
    provider = QueryOnlyProvider()

    report = await StartupReconciliationService(
        database,
        make_settings(tmp_path, dry_run=True),
        provider=provider,
    ).reconcile_all_pending()

    assert report.unresolved_without_order_id == 1
    assert report.remaining_unknown == 1
    assert provider.queries == []
    row = database.fetch_one(
        "SELECT status, journal_state, failure_code FROM trades WHERE client_order_id='client-ambiguous'"
    )
    assert row == {
        "status": "pending",
        "journal_state": "submission_unknown",
        "failure_code": "SUBMISSION_STARTED_AMBIGUOUS",
    }
    assert database.get_runtime_state("new_entries_paused") is True
    assert database.get_runtime_state("new_entries_pause_reason") == "startup_reconciliation_required"


@pytest.mark.asyncio
async def test_pre_submit_intent_is_safely_aborted_on_startup(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    await SqliteOrderJournal(database).reserve(make_intent("client-pre-submit"))
    provider = QueryOnlyProvider()

    report = await StartupReconciliationService(
        database,
        make_settings(tmp_path),
        provider=provider,
    ).reconcile_all_pending()

    assert report.pre_submit_aborted == 1
    assert report.remaining_unknown == 0
    assert provider.queries == []
    row = database.fetch_one(
        "SELECT status, journal_state, failure_code FROM trades WHERE client_order_id='client-pre-submit'"
    )
    assert row == {
        "status": "cancelled",
        "journal_state": "terminal",
        "failure_code": "PRE_SUBMIT_ABORTED",
    }


@pytest.mark.asyncio
async def test_legacy_reserved_state_remains_fail_closed(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    await SqliteOrderJournal(database).reserve(make_intent("client-legacy-reserved"))
    database.execute(
        "UPDATE trades SET journal_state='reserved' WHERE client_order_id='client-legacy-reserved'"
    )

    report = await StartupReconciliationService(
        database,
        make_settings(tmp_path),
        provider=QueryOnlyProvider(),
    ).reconcile_all_pending()

    assert report.remaining_unknown == 1
    row = database.fetch_one(
        "SELECT status, journal_state, failure_code FROM trades WHERE client_order_id='client-legacy-reserved'"
    )
    assert row == {
        "status": "pending",
        "journal_state": "submission_unknown",
        "failure_code": "STARTUP_RESERVED_AMBIGUOUS",
    }


@pytest.mark.asyncio
async def test_live_prepare_is_blocked_by_unresolved_trade(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    await SqliteOrderJournal(database).reserve(make_intent("client-block-live"))
    runtime = RuntimeService(database, make_settings(tmp_path))

    prepared = runtime.prepare_live_start()

    assert prepared.can_confirm is False
    assert prepared.summary["unresolved_orders"] == 1
    assert prepared.blocker == "unresolved orders require reconciliation before live trading"


def test_liquidation_scope_keeps_simulation_and_live_actions_separate(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    session_id = PaperTradingService(database, settings).ensure_simulation_session()["id"]
    insert_position(database, position_id="scope-paper", address="paper-scope-token", account_kind="paper")
    database.execute(
        "UPDATE positions SET simulation_session_id=? WHERE id='scope-paper'",
        (session_id,),
    )
    insert_position(database, position_id="scope-live", address="live-scope-token", account_kind="live")
    database.set_runtime_state("live_trading_enabled", True)
    runtime = RuntimeService(database, settings)

    prepared = runtime.prepare_liquidation("simulation")
    queued = runtime.confirm_liquidation(prepared.challenge, "simulation")
    job = database.get_runtime_state("liquidation_job")

    assert prepared.summary["scope"] == "simulation"
    assert prepared.summary["position_count"] == 1
    assert prepared.summary["live_position_count"] == 0
    assert queued["scope"] == "simulation"
    assert job["position_ids"] == ["scope-paper"]
    assert database.get_runtime_state("live_trading_enabled") is True
    assert database.get_runtime_state("new_entries_paused", False) is False
    assert database.get_runtime_state("simulation_entries_paused") is True


@pytest.mark.asyncio
async def test_paper_liquidation_uses_sell_quote_and_closes_position(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    address = "paper-token"
    now = datetime.now(timezone.utc)
    insert_sample(database, address=address, entry_time=int(now.timestamp()) - 1, price=1.10)
    insert_position(database, position_id="paper-1", address=address, account_kind="paper")
    database.set_runtime_state(
        "portfolio_account:paper",
        {
            "cash_usd": 950.0,
            "sol_fee_reserve": 0.1,
            "initial_cash_usd": 1000.0,
            "initial_sol_fee_reserve": 0.1,
            "source": "test",
            "updated_at": utc_now_iso(),
        },
    )
    runtime = RuntimeService(database, settings)
    prepared = runtime.prepare_liquidation()
    queued = runtime.confirm_liquidation(prepared.challenge)
    paper = PaperTradingService(database, settings, quote_provider=FixedQuoteProvider())

    report = await LiquidationService(
        database,
        settings,
        paper_service=paper,
    ).process_active_job()

    assert report is not None
    assert report.job_id == queued["id"]
    assert report.status == "completed"
    assert report.closed_positions == 1
    position = database.fetch_one("SELECT status, exit_reason FROM positions WHERE id='paper-1'")
    assert position == {"status": "closed", "exit_reason": "liquidate_all"}
    trade = database.fetch_one(
        "SELECT status, side FROM trades WHERE client_order_id='paper-1:liquidate_all'"
    )
    assert trade == {"status": "confirmed", "side": "sell"}


@pytest.mark.asyncio
async def test_live_liquidation_without_atomic_amount_never_fakes_closed(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    insert_position(database, position_id="live-missing-atomic", address="live-token", account_kind="live")
    runtime = RuntimeService(database, settings)
    prepared = runtime.prepare_liquidation()
    runtime.confirm_liquidation(prepared.challenge)
    executor = SequencedLiveExecutor([OrderStatus.CONFIRMED])

    report = await LiquidationService(
        database,
        settings,
        live_executor=executor,
    ).process_active_job()

    assert report is not None and report.status == "blocked"
    assert report.closed_positions == 0
    assert report.blocked_positions == 1
    assert executor.client_order_ids == []
    position = database.fetch_one("SELECT status, exit_time FROM positions WHERE id='live-missing-atomic'")
    assert position == {"status": "open", "exit_time": None}


@pytest.mark.asyncio
async def test_pending_live_liquidation_resumes_same_order_id_after_restart_cycle(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    insert_position(
        database,
        position_id="live-resume",
        address="live-token-resume",
        account_kind="live",
        metadata_json='{"quantity_atomic":"123456","quote_token":"SOL"}',
    )
    runtime = RuntimeService(database, settings)
    database.set_runtime_state("live_trading_enabled", True)
    prepared = runtime.prepare_liquidation()
    queued = runtime.confirm_liquidation(prepared.challenge)
    assert database.get_runtime_state("live_trading_enabled") is False
    executor = SequencedLiveExecutor([OrderStatus.PENDING, OrderStatus.CONFIRMED])
    service = LiquidationService(database, settings, live_executor=executor)

    first = await service.process_active_job()
    assert first is not None and first.status == "running"
    assert first.pending_positions == 1
    assert database.fetch_one("SELECT status FROM positions WHERE id='live-resume'")["status"] == "closing"

    second = await service.process_active_job()
    assert second is not None and second.status == "completed"
    assert second.closed_positions == 1
    assert executor.client_order_ids == [
        f"{queued['id']}:live-resume:sell",
        f"{queued['id']}:live-resume:sell",
    ]
    position = database.fetch_one("SELECT status, exit_reason FROM positions WHERE id='live-resume'")
    assert position == {"status": "closed", "exit_reason": "liquidate_all"}


def test_agent_routes_require_human_approval_and_never_expose_live_execution(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    service = AgentService(database, settings)

    created = service.create_proposal("pause_new_entries", {"reason": "manual review"})
    context = service.get_context()

    assert created["status"] == "pending_approval"
    assert service.list_proposals(limit=1)[0]["id"] == created["id"]
    assert context["access_level"] == "read_only_and_human_approved_non_live_actions"
    assert "live_buy" in context["explicitly_forbidden"]
    with pytest.raises(ValueError, match="unsafe"):
        service.create_proposal("live_buy", {"token": "unsafe"})

    approved = service.approve_proposal(created["id"], note="human approved")
    assert approved["status"] == "executed"
    assert approved["result"]["status"] == "paused"
    assert database.get_runtime_state("new_entries_paused") is True

    rejected = service.create_proposal("resume_new_entries", {})
    rejected = service.reject_proposal(rejected["id"], note="keep paused")
    assert rejected["status"] == "rejected"
    assert database.get_runtime_state("new_entries_paused") is True

    paths = {route.path for route in router.routes}
    assert "/api/agent/context" in paths
    assert "/api/agent/proposals" in paths
    assert "/api/agent/proposals/{proposal_id}/approve" in paths
    assert "/api/agent/proposals/{proposal_id}/reject" in paths
    assert "/api/agent/execute" not in paths
    assert "/api/agent/liquidate" not in paths
