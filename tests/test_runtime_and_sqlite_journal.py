from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.services.order_journal import SqliteOrderJournal
from backend.app.services.runtime import RuntimeService
from backend.app.trading.live.models import (
    ExecutionResult,
    OrderStatus,
    Submission,
    SwapIntent,
    TradeSide,
)


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.initialize()
    return database


def test_live_start_requires_valid_two_click_challenge(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        dry_run=False,
        wallet_public_key="1234567890abcdefghij",
        trading_provider="gmgn_cli",
    )
    service = RuntimeService(database, settings)
    prepared = service.prepare_live_start()
    assert prepared.can_confirm
    with pytest.raises(ValueError):
        service.confirm_live_start("x" * 24)
    prepared = service.prepare_live_start()
    status = service.confirm_live_start(prepared.challenge)
    assert status["live_trading_enabled"] is True
    with pytest.raises(ValueError):
        service.confirm_live_start(prepared.challenge)


@pytest.mark.asyncio
async def test_sqlite_order_journal_survives_new_instance(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    intent = SwapIntent(
        chain="sol",
        wallet_address="wallet-address",
        input_token="sol-token",
        output_token="meme-token",
        input_amount_raw="1000000",
        side=TradeSide.BUY,
        client_order_id="client-1",
    )
    first = SqliteOrderJournal(database)
    await first.reserve(intent)
    await first.mark_submitted("client-1", Submission("order-1", OrderStatus.PENDING), 0)

    second = SqliteOrderJournal(database)
    restored = await second.get("client-1")
    assert restored is not None
    assert restored.order_id == "order-1"
    assert restored.state == "submitted"

    result = ExecutionResult("client-1", "order-1", OrderStatus.CONFIRMED, 1, tx_hash="hash-1")
    await second.mark_result("client-1", result)
    final = await SqliteOrderJournal(database).get("client-1")
    assert final is not None and final.result is not None
    assert final.result.status is OrderStatus.CONFIRMED

