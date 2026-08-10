from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.database import Database, utc_now_iso
from backend.app.services.training import TrainingService
from backend.app.services.training_worker import TrainingWorker


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "training-worker.db")
    database.initialize()
    return database


def make_settings(tmp_path: Path, *, retries: int = 2) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        background_workers_enabled=False,
        training_max_retries=retries,
        training_worker_poll_seconds=1,
    )


def insert_run(database: Database, *, run_id: str, status: str, retry_count: int = 0) -> None:
    database.execute(
        """
        INSERT INTO training_runs(
            id,trigger,status,requested_at,request_json,retry_count,started_at
        ) VALUES(?, 'manual', ?, ?, '{"feature_names":["price"]}', ?, ?)
        """,
        (
            run_id,
            status,
            utc_now_iso(),
            retry_count,
            utc_now_iso() if status == "running" else None,
        ),
    )


def test_restart_requeues_interrupted_run_with_bounded_retry(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    worker = TrainingWorker(database, make_settings(tmp_path, retries=2))
    insert_run(database, run_id="recover-me", status="running", retry_count=0)
    insert_run(database, run_id="exhausted", status="running", retry_count=2)

    report = worker.recover_interrupted_runs()

    assert report.recovered == 1
    assert report.exhausted == 1
    recovered = database.fetch_one(
        "SELECT status,retry_count,started_at,error_message FROM training_runs WHERE id='recover-me'"
    )
    exhausted = database.fetch_one(
        "SELECT status,retry_count,error_message FROM training_runs WHERE id='exhausted'"
    )
    assert recovered["status"] == "queued"
    assert recovered["retry_count"] == 1
    assert recovered["started_at"] is None
    assert "process restart" in recovered["error_message"]
    assert exhausted["status"] == "failed"
    assert exhausted["retry_count"] == 2
    assert "retry limit exhausted" in exhausted["error_message"]


@pytest.mark.asyncio
async def test_worker_executes_oldest_queued_run_and_leaves_next_for_following_cycle(
    monkeypatch, tmp_path: Path
) -> None:
    database = make_database(tmp_path)
    worker = TrainingWorker(database, make_settings(tmp_path))
    insert_run(database, run_id="first", status="queued")
    insert_run(database, run_id="second", status="queued")
    database.execute(
        "UPDATE training_runs SET requested_at='2026-01-01T00:00:00+00:00' WHERE id='first'"
    )
    database.execute(
        "UPDATE training_runs SET requested_at='2026-01-02T00:00:00+00:00' WHERE id='second'"
    )
    executed: list[str] = []

    def complete(self: TrainingService, run_id: str) -> None:
        executed.append(run_id)
        self.database.execute(
            "UPDATE training_runs SET status='completed',completed_at=? WHERE id=?",
            (utc_now_iso(), run_id),
        )

    monkeypatch.setattr(TrainingService, "run", complete)
    worker.service = TrainingService(database, worker.settings)

    first = await worker.run_once()
    second_status = database.fetch_one(
        "SELECT status FROM training_runs WHERE id='second'"
    )["status"]
    second = await worker.run_once()

    assert first == "first"
    assert second == "second"
    assert executed == ["first", "second"]
    assert second_status == "queued"


def test_training_service_does_not_discard_queued_run_when_another_is_running(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    service = TrainingService(database, make_settings(tmp_path))
    insert_run(database, run_id="running", status="running")
    insert_run(database, run_id="waiting", status="queued")

    service.run("waiting")

    waiting = database.fetch_one(
        "SELECT status,completed_at,error_message FROM training_runs WHERE id='waiting'"
    )
    assert waiting == {"status": "queued", "completed_at": None, "error_message": None}
