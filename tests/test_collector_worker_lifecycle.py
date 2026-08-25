from __future__ import annotations

from pathlib import Path
import time

import pytest

from backend.app.collector.errors import CollectorError, CollectorRateLimitError
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.services.collector_worker import CollectorWorker


class StopAfterFinalizeService:
    def __init__(self, worker: CollectorWorker) -> None:
        self.worker = worker
        self.provider = object()
        self.finalize_calls = 0
        self.collect_calls = 0

    async def finalize_due(self):
        self.finalize_calls += 1
        self.worker.stop()
        return type("Finalization", (), {"finalized": 2})()

    async def collect_once(self, *, limit: int, event_sink=None):
        self.collect_calls += 1
        raise AssertionError("monitor-only collector must never call discovery")


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "collector-lifecycle.db")
    database.initialize()
    return database


@pytest.mark.asyncio
async def test_monitor_only_collector_is_label_only_and_never_runs_position_exits(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        background_workers_enabled=False,
        collector_enabled=False,
        paper_market_monitor_enabled=True,
        collector_poll_seconds=15,
    )
    worker = CollectorWorker(database, settings, monitor_only=True)
    service = StopAfterFinalizeService(worker)
    worker._build = lambda: service  # type: ignore[method-assign]

    await worker.run_forever()

    assert service.finalize_calls == 1
    assert service.collect_calls == 0
    status = database.get_runtime_state("collector_status")
    assert status["state"] == "stopped"
    assert status["mode"] == "monitor_only"
    assert "paper_positions_checked" not in status
    assert status["finalized"] == 2


def _wrapped_rate_limit(reset_at: int) -> CollectorError:
    try:
        raise CollectorError("wrapped rate limit") from CollectorRateLimitError(
            "rate limited", reset_at=reset_at
        )
    except CollectorError as exc:
        return exc


def test_rate_limit_circuit_is_persisted_and_survives_worker_recreation(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = Settings(_env_file=None, app_env="test", sqlite_path=str(tmp_path / "unused.db"))
    worker = CollectorWorker(database, settings)
    now = int(time.time())

    first = worker._open_rate_limit_circuit(_wrapped_rate_limit(now + 120), stage="discovery")

    assert first["state"] == "open"
    assert first["streak"] == 1
    assert first["backoff_seconds"] == 300.0
    assert first["next_probe_epoch"] >= now + 299
    persisted = database.get_runtime_state("collector_rate_limit_circuit")
    assert persisted["streak"] == 1

    recreated = CollectorWorker(database, settings)
    assert recreated._rate_limit_streak == 1
    assert recreated._rate_limit_remaining() > 0


def test_repeated_rate_limit_doubles_quiet_window_and_success_closes_circuit(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = Settings(_env_file=None, app_env="test", sqlite_path=str(tmp_path / "unused.db"))
    worker = CollectorWorker(database, settings)
    now = int(time.time())

    first = worker._open_rate_limit_circuit(_wrapped_rate_limit(now + 30), stage="discovery")
    second = worker._open_rate_limit_circuit(_wrapped_rate_limit(now + 30), stage="half_open_collection_probe")

    assert first["backoff_seconds"] == 300.0
    assert second["streak"] == 2
    assert second["backoff_seconds"] == 600.0
    assert second["next_probe_epoch"] >= now + 599

    worker._close_rate_limit_circuit()
    persisted = database.get_runtime_state("collector_rate_limit_circuit")
    assert persisted["state"] == "closed"
    assert persisted["streak"] == 0
    assert worker._rate_limit_remaining() == 0.0


def test_rate_limit_recurrence_during_probation_continues_previous_streak(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = Settings(_env_file=None, app_env="test", sqlite_path=str(tmp_path / "unused.db"))
    worker = CollectorWorker(database, settings)
    now = int(time.time())

    first = worker._open_rate_limit_circuit(_wrapped_rate_limit(now + 30), stage="discovery")
    worker._close_rate_limit_circuit()
    relapse = worker._open_rate_limit_circuit(_wrapped_rate_limit(now + 30), stage="discovery")

    assert first["streak"] == 1
    assert relapse["streak"] == 2
    assert relapse["backoff_seconds"] == 600.0
