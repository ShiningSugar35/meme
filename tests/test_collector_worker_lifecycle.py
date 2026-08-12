from __future__ import annotations

from pathlib import Path

import pytest

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
