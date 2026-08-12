from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.database import Database, utc_now_iso
from backend.app.repositories.models import ModelRepository
from backend.app.services.paper_trading import PaperTradingService
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


def _register_generation(database: Database, prefix: str, *, activate: bool) -> list[dict]:
    repo = ModelRepository(database)
    generation: list[dict] = []
    for slot, algorithm in enumerate(("decision_tree", "random_forest", "xgboost"), start=1):
        model_id = f"{prefix}-{slot}"
        repo.register(
            {
                "id": model_id,
                "version": model_id,
                "algorithm": algorithm,
                "status": "candidate",
                "early_stage": True,
                "trained_at": utc_now_iso(),
                "feature_names": ["price"],
                "parameters": {},
                "thresholds": {"decision": 0.2 + slot * 0.01},
                "metrics": {"composite_score": 0.8 - slot * 0.1},
                "artifact_path": f"ml_models/{model_id}.joblib",
            }
        )
        generation.append(
            {
                "id": model_id,
                "algorithm": algorithm,
                "composite_score": 0.8 - slot * 0.1,
                "threshold": 0.2 + slot * 0.01,
                "metrics": {},
            }
        )
    if activate:
        repo.set_active_models(generation)
    return generation


@pytest.mark.asyncio
async def test_completed_candidate_waits_for_flat_survives_worker_restart_and_resets_generation(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    current = _register_generation(database, "current", activate=True)
    paper = PaperTradingService(database, settings)
    session_id = paper.ensure_simulation_session()["id"]
    for strategy in ("model_1", "model_2", "model_3"):
        state = paper.ensure_account(strategy)
        state["cash_usd"] = 777.0
        database.set_runtime_state(f"portfolio_strategy:{strategy}", state)

    now = datetime.now(timezone.utc)
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,strategy_key,status,simulation_session_id,
            model_id,entry_time,expires_at,invested_usd,net_pnl_usd
        ) VALUES('old-open','old-token','simulation','model_1','open',?,?,?,?,50,NULL)
        """,
        (
            session_id,
            current[0]["id"],
            now.isoformat(),
            (now + timedelta(hours=2)).isoformat(),
        ),
    )
    candidate = _register_generation(database, "candidate", activate=False)
    run_id = "pending-generation"
    database.execute(
        """
        INSERT INTO training_runs(
            id,trigger,status,requested_at,completed_at,request_json,promoted,summary_json
        ) VALUES(?, 'manual', 'completed', ?, ?, '{}', 0, ?)
        """,
        (
            run_id,
            utc_now_iso(),
            utc_now_iso(),
            json.dumps({
                "top_models": candidate,
                "activation": {"status": "waiting_for_flat"},
            }),
        ),
    )

    first_worker = TrainingWorker(database, settings)
    assert await first_worker.run_once() is None
    assert [model["id"] for model in ModelRepository(database).active_models()] == [item["id"] for item in current]
    assert database.get_runtime_state("model_entries_paused_for_rollover") is True
    assert database.get_runtime_state("model_rollover_status")["state"] == "waiting_for_flat"
    assert database.fetch_one("SELECT promoted FROM training_runs WHERE id=?", (run_id,))["promoted"] == 0

    # Simulate the old generation closing while the process is down. A fresh
    # TrainingWorker instance must recover the durable candidate and activate it.
    database.execute(
        "UPDATE positions SET status='closed',exit_time=?,net_pnl_usd=-50 WHERE id='old-open'",
        (utc_now_iso(),),
    )
    restarted_worker = TrainingWorker(database, settings)
    assert await restarted_worker.run_once() is None

    active = ModelRepository(database).active_models()
    assert [model["id"] for model in active] == [item["id"] for item in candidate]
    assert database.fetch_one("SELECT promoted FROM training_runs WHERE id=?", (run_id,))["promoted"] == 1
    assert database.get_runtime_state("model_entries_paused_for_rollover") is False
    status = PaperTradingService(database, settings).simulation_status()
    for slot in (1, 2, 3):
        account = status["accounts"][f"model_{slot}"]
        assert account["cash_usd"] == pytest.approx(1000.0)
        assert account["invested_usd"] == pytest.approx(0.0)
        assert account["realized_pnl_usd"] == pytest.approx(0.0)
        assert account["total_fees_usd"] == pytest.approx(0.0)
        assert account["open_positions"] == 0
        assert account["trade_count"] == 0
        assert account["precision"] is None
        assert account["recall"] is None
        assert account["model_id"] == candidate[slot - 1]["id"]
