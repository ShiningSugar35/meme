from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.services.collector_worker import CollectorWorker
from backend.app.services.paper_position_monitor import PaperMonitorCycle


class FakeService:
    def __init__(self) -> None:
        self.provider = object()
        self.finalize_calls = 0
        self.collect_calls = 0

    async def finalize_due(self):
        self.finalize_calls += 1
        return type("Finalization", (), {"finalized": 2})()

    async def collect_once(self, *, limit: int):
        self.collect_calls += 1
        raise AssertionError("monitor-only worker must never call discovery")


class StopAfterMonitor:
    def __init__(self, worker: CollectorWorker) -> None:
        self.worker = worker
        self.calls = 0

    async def run_cycle(self, provider):
        self.calls += 1
        self.worker.stop()
        return PaperMonitorCycle(
            checked_positions=3,
            market_requests=1,
            closed_positions=1,
            pending_positions=1,
            blocked_positions=0,
            open_positions=1,
            skipped_liquidation_positions=0,
            completed_at="2026-08-10T00:00:00+00:00",
        )


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "collector-lifecycle.db")
    database.initialize()
    return database


@pytest.mark.asyncio
async def test_monitor_only_worker_runs_exits_and_labels_without_discovery(tmp_path: Path) -> None:
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
    service = FakeService()
    worker._build = lambda: service  # type: ignore[method-assign]
    monitor = StopAfterMonitor(worker)
    worker._paper_monitor = monitor  # type: ignore[assignment]

    await worker.run_forever()

    assert monitor.calls == 1
    assert service.finalize_calls == 1
    assert service.collect_calls == 0
    status = database.get_runtime_state("collector_status")
    assert status["state"] == "stopped"
    assert status["mode"] == "monitor_only"
    assert status["paper_positions_checked"] == 3
    assert status["paper_positions_closed"] == 1
    assert status["finalized"] == 2
