from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.app.config import Settings
from backend.app.database import Database, utc_now_iso
from backend.app.repositories.models import ModelRepository
from backend.app.scheduler.service import TrainingScheduler
from backend.app.services.training import TrainingService


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "scheduler.db")
    database.initialize()
    return database


def make_settings(tmp_path: Path, *, retries: int = 2, min_mature: int = 0) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        background_workers_enabled=False,
        training_timezone="Asia/Shanghai",
        training_weekday=6,
        training_hour=17,
        training_minute=0,
        training_max_retries=retries,
        modeling_min_mature_samples=min_mature,
    )


def set_health(database: Database, state: str) -> None:
    database.set_runtime_state("model_health_status", {"state": state})


def test_weekly_schedule_is_sunday_1700_bjt(tmp_path: Path) -> None:
    scheduler = TrainingScheduler(make_database(tmp_path), make_settings(tmp_path))
    tz = ZoneInfo("Asia/Shanghai")

    before = datetime(2026, 8, 9, 16, 59, tzinfo=tz)
    after = datetime(2026, 8, 9, 17, 1, tzinfo=tz)

    assert scheduler.schedule_mode() == "weekly"
    assert scheduler.next_scheduled_at(before) == datetime(2026, 8, 9, 17, 0, tzinfo=tz)
    assert scheduler.most_recent_scheduled_at(after) == datetime(2026, 8, 9, 17, 0, tzinfo=tz)
    assert scheduler.next_scheduled_at(after) == datetime(2026, 8, 16, 17, 0, tzinfo=tz)


def test_insufficient_data_accelerates_schedule_to_daily_1700_bjt(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    set_health(database, "insufficient_data")
    scheduler = TrainingScheduler(database, make_settings(tmp_path))
    tz = ZoneInfo("Asia/Shanghai")

    before = datetime(2026, 8, 10, 16, 59, tzinfo=tz)
    after = datetime(2026, 8, 10, 17, 1, tzinfo=tz)

    assert scheduler.schedule_mode() == "daily"
    assert scheduler.next_scheduled_at(before) == datetime(2026, 8, 10, 17, 0, tzinfo=tz)
    assert scheduler.most_recent_scheduled_at(after) == datetime(2026, 8, 10, 17, 0, tzinfo=tz)
    assert scheduler.next_scheduled_at(after) == datetime(2026, 8, 11, 17, 0, tzinfo=tz)


@pytest.mark.parametrize(
    "state",
    ("active_top3_not_ready", "active_top3_contract_stale", "deployment_certification_blocked", "drift_detected"),
)
def test_recovery_states_accelerate_schedule_to_daily(tmp_path: Path, state: str) -> None:
    database = make_database(tmp_path)
    set_health(database, state)
    scheduler = TrainingScheduler(database, make_settings(tmp_path))

    assert scheduler.schedule_mode() == "daily"


@pytest.mark.asyncio
async def test_sample_floor_disables_automatic_training_and_rollover_gate(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path, min_mature=1_000)
    scheduler = TrainingScheduler(database, settings)
    now = datetime(2026, 8, 10, 17, 1, tzinfo=ZoneInfo("Asia/Shanghai"))

    run_id = await scheduler._schedule_if_due(startup=False, now=now)

    assert run_id is None
    assert scheduler.schedule_mode() == "data_collection_only"
    assert database.fetch_one("SELECT COUNT(*) AS n FROM training_runs")["n"] == 0
    assert database.get_runtime_state("model_entries_paused_for_rollover") is False
    status = database.get_runtime_state("scheduler_status")
    assert status["automatic_training_eligible"] is False
    assert status["mature_samples"] == 0
    assert status["min_mature_samples"] == 1_000
    assert status["next_training_at"] is None


def test_due_day_entry_gate_freezes_at_1600_until_new_generation_activates(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    scheduler = TrainingScheduler(database, make_settings(tmp_path))
    tz = ZoneInfo("Asia/Shanghai")
    # The previous weekly generation activated after the prior Sunday due point.
    database.set_runtime_state(
        "last_model_activation_at",
        datetime(2026, 8, 2, 18, 0, tzinfo=tz).astimezone(timezone.utc).isoformat(),
    )

    assert not scheduler.refresh_entry_gate(now=datetime(2026, 8, 9, 15, 59, tzinfo=tz))
    assert scheduler.refresh_entry_gate(now=datetime(2026, 8, 9, 16, 0, tzinfo=tz))
    assert scheduler.refresh_entry_gate(now=datetime(2026, 8, 9, 17, 30, tzinfo=tz))
    assert database.get_runtime_state("model_entries_paused_for_rollover") is True

    database.set_runtime_state(
        "last_model_activation_at",
        datetime(2026, 8, 9, 17, 31, tzinfo=tz).astimezone(timezone.utc).isoformat(),
    )
    assert not scheduler.refresh_entry_gate(now=datetime(2026, 8, 9, 17, 32, tzinfo=tz))
    assert database.get_runtime_state("model_entries_paused_for_rollover") is False


def test_daily_recovery_completed_attempt_releases_old_due_until_next_freeze(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    set_health(database, "active_top3_contract_stale")
    scheduler = TrainingScheduler(database, make_settings(tmp_path))
    tz = ZoneInfo("Asia/Shanghai")
    database.set_runtime_state(
        "last_training_completed_at",
        datetime(2026, 8, 25, 9, 51, tzinfo=tz).astimezone(timezone.utc).isoformat(),
    )

    assert not scheduler.refresh_entry_gate(now=datetime(2026, 8, 25, 10, 0, tzinfo=tz))
    assert database.get_runtime_state("model_entries_paused_for_rollover") is False
    assert scheduler.refresh_entry_gate(now=datetime(2026, 8, 25, 16, 0, tzinfo=tz))
    gate = database.get_runtime_state("model_entry_rollover_gate")
    assert gate["scheduled_for"] == "2026-08-25T09:00:00+00:00"


@pytest.mark.asyncio
async def test_startup_catchup_is_unique_for_same_weekly_schedule(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    scheduler = TrainingScheduler(database, settings)
    now = datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    first = await scheduler._schedule_if_due(startup=True, now=now)
    second = await scheduler._schedule_if_due(startup=True, now=now)

    assert first == second
    rows = database.fetch_all("SELECT * FROM training_runs")
    assert len(rows) == 1
    assert rows[0]["trigger"] == "startup_catchup"
    assert rows[0]["status"] == "queued"
    assert rows[0]["retry_count"] == 0
    assert rows[0]["scheduled_for"] == "2026-08-09T09:00:00+00:00"


@pytest.mark.asyncio
async def test_insufficient_data_due_cycle_queues_daily_training_at_1700(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    set_health(database, "insufficient_data")
    scheduler = TrainingScheduler(database, make_settings(tmp_path))
    now = datetime(2026, 8, 10, 17, 1, tzinfo=ZoneInfo("Asia/Shanghai"))

    run_id = await scheduler._schedule_if_due(startup=False, now=now)
    row = database.fetch_one(
        "SELECT trigger,status,scheduled_for FROM training_runs WHERE id=?", (run_id,)
    )

    assert row == {
        "trigger": "daily",
        "status": "queued",
        "scheduled_for": "2026-08-10T09:00:00+00:00",
    }
    assert database.get_runtime_state("model_entries_paused_for_rollover") is True


@pytest.mark.asyncio
async def test_scheduled_training_uses_persisted_feature_pool_not_champion_subset(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    repository = ModelRepository(database)
    active = []
    for slot in (1, 2, 3):
        model_id = f"schedule-model-{slot}"
        repository.register(
            {
                "id": model_id,
                "version": model_id,
                "algorithm": "logistic_regression",
                "status": "candidate",
                "early_stage": True,
                "trained_at": "2026-08-01T00:00:00+00:00",
                "feature_names": ["price", "price_change_1h", "ln(liquidity_usd)"],
                "parameters": {},
                "thresholds": {"decision": 0.4},
                "metrics": {},
                "artifact_path": "ml_models/not-needed.joblib",
            }
        )
        active.append({"id": model_id, "composite_score": 1.0 - slot * 0.1, "threshold": 0.4, "metrics": {}})
    repository.set_active_models(active)
    TrainingService(database, settings).save_feature_selection(
        ["ln(age+1)", "ln(price+1)", "price_change_1h", "ln(liquidity_usd)"]
    )

    run_id = await TrainingScheduler(database, settings)._schedule_if_due(
        startup=False,
        now=datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    row = database.fetch_one("SELECT request_json FROM training_runs WHERE id=?", (run_id,))
    assert json.loads(row["request_json"])["feature_names"] == [
        "ln(age+1)",
        "momentum_accel_1m_vs_5m",
        "ln(marketcap+1)",
    ]


@pytest.mark.asyncio
async def test_failed_schedule_retries_same_run_only_to_limit(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path, retries=2)
    scheduler = TrainingScheduler(database, settings)
    now = datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    first = await scheduler._schedule_if_due(startup=True, now=now)
    database.execute(
        "UPDATE training_runs SET status='failed', completed_at=?, error_message='fixture failure' WHERE id=?",
        (utc_now_iso(), first),
    )
    second = await scheduler._schedule_if_due(startup=True, now=now)
    database.execute(
        "UPDATE training_runs SET status='failed', completed_at=?, error_message='fixture failure' WHERE id=?",
        (utc_now_iso(), first),
    )
    third = await scheduler._schedule_if_due(startup=True, now=now)
    database.execute(
        "UPDATE training_runs SET status='failed', completed_at=?, error_message='fixture failure' WHERE id=?",
        (utc_now_iso(), first),
    )
    fourth = await scheduler._schedule_if_due(startup=True, now=now)

    assert first == second == third == fourth
    rows = database.fetch_all("SELECT id,status,retry_count FROM training_runs")
    assert rows == [{"id": first, "status": "failed", "retry_count": 2}]
    status = database.get_runtime_state("scheduler_status")
    assert status["state"] == "degraded"
    assert status["retry_count"] == 2
    assert status["retry_limit"] == 2
