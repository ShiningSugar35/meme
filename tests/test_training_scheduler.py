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


def make_settings(tmp_path: Path, *, retries: int = 2) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        background_workers_enabled=False,
        training_timezone="Asia/Shanghai",
        training_weekday=6,
        training_hour=3,
        training_minute=0,
        training_max_retries=retries,
    )


def test_weekly_schedule_is_sunday_0300_bjt(tmp_path: Path) -> None:
    scheduler = TrainingScheduler(make_database(tmp_path), make_settings(tmp_path))
    tz = ZoneInfo("Asia/Shanghai")

    before = datetime(2026, 8, 9, 2, 59, tzinfo=tz)
    after = datetime(2026, 8, 9, 3, 1, tzinfo=tz)

    assert scheduler.next_scheduled_at(before) == datetime(2026, 8, 9, 3, 0, tzinfo=tz)
    assert scheduler.most_recent_scheduled_at(after) == datetime(2026, 8, 9, 3, 0, tzinfo=tz)
    assert scheduler.next_scheduled_at(after) == datetime(2026, 8, 16, 3, 0, tzinfo=tz)


@pytest.mark.asyncio
async def test_startup_catchup_is_unique_for_same_schedule(monkeypatch, tmp_path: Path) -> None:
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
    assert rows[0]["scheduled_for"] == "2026-08-08T19:00:00+00:00"


@pytest.mark.asyncio
async def test_scheduled_training_inherits_champion_feature_schema(monkeypatch, tmp_path: Path) -> None:
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

    def complete(self, run_id: str) -> None:
        self.database.execute(
            "UPDATE training_runs SET status='completed', completed_at=? WHERE id=?",
            (utc_now_iso(), run_id),
        )

    monkeypatch.setattr(TrainingService, "run", complete)
    run_id = await TrainingScheduler(database, settings)._schedule_if_due(
        startup=False,
        now=datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    row = database.fetch_one("SELECT request_json FROM training_runs WHERE id=?", (run_id,))
    assert json.loads(row["request_json"])["feature_names"] == [
        "price",
        "ln(liquidity_usd)",
        "price_change_1h",
    ]


@pytest.mark.asyncio
async def test_failed_schedule_retries_same_run_only_to_limit(monkeypatch, tmp_path: Path) -> None:
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
