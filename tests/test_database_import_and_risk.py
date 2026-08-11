from __future__ import annotations

import asyncio
import csv
from pathlib import Path

from backend.app.collector.labels import PriceWindowResult
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.repositories.samples import SampleRecord, SampleRepository
from backend.app.risk.service import RiskService
from backend.app.services.collector_worker import SqliteCollectorSink
from backend.app.services.csv_importer import CsvImporter
from backend.app.services.sample_export import SampleExportService


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.initialize()
    return database


def test_csv_import_is_idempotent_and_migrates_legacy_terminal(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    path = tmp_path / "legacy.csv"
    fields = [
        "address", "name", "symbol", "type", "time", "age", "launchpad", "price",
        "price_2h_max/price", "price_2h_min/price", "fresh_wallet_rate", "tag",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "address": "token-a", "name": "A", "symbol": "A", "type": "new_creation",
            "time": "1700000000", "age": "1.2", "launchpad": "Pump.fun", "price": "0.1",
            "price_2h_max/price": "1.30", "price_2h_min/price": "0.91",
            "fresh_wallet_rate": "0.1", "tag": "1",
        })
    importer = CsvImporter(database)
    first = importer.import_file(path)
    second = importer.import_file(path)
    assert first.inserted_rows == 1
    assert first.legacy_terminal_rows == 1
    assert second.inserted_rows == 0
    row = database.fetch_one("SELECT * FROM samples")
    assert row["tag"] == 0
    assert row["final_close_ratio"] == 1.25
    assert row["terminal_return_estimated"] == 0
    assert row["utility_eligible"] == 0
    assert row["gross_return_rate"] == -0.10
    assert row["return_source"] == "legacy_binary_rule"
    assert row["exit_reason"] == "legacy_negative_or_timeout"


def test_label_finalization_preserves_legacy_utility_ineligibility(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    repository = SampleRepository(database)
    repository.insert(
        SampleRecord(
            address="fixture-mint-legacy-pending",
            entry_time=1_800_000_000,
            entry_price=1.0,
            liquidity=None,
            liquidity_estimated=True,
            utility_eligible=False,
            features={"price_change_1h": None},
            label_status="pending",
            label_source="legacy_csv_migration",
        )
    )
    result = PriceWindowResult(
        address="fixture-mint-legacy-pending",
        entry_time=1_800_000_000,
        label_version="sl090_tp160_h2_binary_v3",
        tag=0,
        exit_reason="window_timeout_negative",
        max_price_ratio=1.3,
        min_price_ratio=0.95,
        final_close_ratio=1.25,
        first_take_profit_at=None,
        first_stop_loss_at=None,
        price_change_1h=0.1,
        price_change_5m=0.02,
    )

    asyncio.run(SqliteCollectorSink(database).save_label(result))

    row = database.fetch_one(
        "SELECT label_status,utility_eligible,terminal_return_estimated,final_close_ratio FROM samples WHERE address=?",
        ("fixture-mint-legacy-pending",),
    )
    assert row["label_status"] == "mature"
    assert row["utility_eligible"] == 0
    assert row["terminal_return_estimated"] == 0
    assert row["final_close_ratio"] == 1.25


def test_same_token_is_independent_at_different_entry_times(tmp_path: Path) -> None:
    repository = SampleRepository(make_database(tmp_path))
    base = dict(address="token-a", entry_price=1.0, features={"x": 1.0})
    assert repository.insert(SampleRecord(entry_time=100, **base))
    assert repository.insert(SampleRecord(entry_time=100 + 2 * 60 * 60 + 1, **base))
    assert not repository.insert(SampleRecord(entry_time=100, **base))


def test_sample_export_contains_all_and_only_mature_tagged_rows(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    repository = SampleRepository(database)
    repository.insert(SampleRecord(
        address="mature-token", entry_time=100, entry_price=1.0,
        launchpad="Pump.fun", features={"age": 5.0}, tag=1, label_status="mature",
    ))
    repository.insert(SampleRecord(
        address="pending-token", entry_time=200, entry_price=1.0,
        launchpad="letsbonk", features={"age": 6.0}, tag=None, label_status="pending",
    ))

    text, count = SampleExportService(database).render_csv()

    assert count == 1
    rows = list(csv.DictReader(text.splitlines()))
    assert len(rows) == 1
    assert rows[0]["address"] == "mature-token"
    assert rows[0]["launchpad"] == "Pump.fun"
    assert rows[0]["tag"] == "1"
    assert rows[0]["age"] == "5.0"


def test_risk_limits_and_duplicate_live_position(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        max_open_positions=10,
        consecutive_loss_limit=5,
    )
    database.set_runtime_state("live_trading_enabled", True)
    database.execute(
        """
        INSERT INTO positions(id,token_address,account_kind,status,entry_time,expires_at,invested_usd)
        VALUES('p1','token-a','live','open','2026-01-01T00:00:00+00:00','2026-01-01T02:00:00+00:00',50)
        """
    )
    service = RiskService(database, settings)
    duplicate = service.check_new_position(
        token_address="token-a", liquidity_usd=10_000, account_kind="live",
        available_usd=500, wallet_total_usd=500, sol_balance=1,
    )
    assert not duplicate.allowed
    assert duplicate.reason == "duplicate_live_token_position"
    allowed = service.check_new_position(
        token_address="token-b", liquidity_usd=4_900, account_kind="live",
        available_usd=500, wallet_total_usd=500, sol_balance=1,
    )
    assert allowed.allowed
    assert allowed.investment_usd == 49

