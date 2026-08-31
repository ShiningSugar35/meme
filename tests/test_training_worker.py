from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app.collector.constants import LabelPolicy
from backend.app.config import Settings
from backend.app.database import Database, utc_now_iso
from backend.app.ml.decision_policy import (
    DECISION_POLICY_VERSION,
    DEFAULT_AGE_POLICY_VERSION,
    DEPLOYMENT_CERTIFICATION_VERSION,
)
from backend.app.ml.economics import ECONOMIC_OBJECTIVE_VERSION
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


def _deployment_certificate() -> dict:
    return {
        "version": DEPLOYMENT_CERTIFICATION_VERSION,
        "deployment_fit_scope": "final_train_only_certified_instance",
        "qualified_deployment_evidence": True,
    }


def _register_generation(database: Database, prefix: str, *, activate: bool) -> list[dict]:
    repo = ModelRepository(database)
    generation: list[dict] = []
    for slot, algorithm in enumerate(("decision_tree", "random_forest", "xgboost"), start=1):
        model_id = f"{prefix}-{slot}"
        certificate = _deployment_certificate()
        repo.register(
            {
                "id": model_id,
                "version": model_id,
                "algorithm": algorithm,
                "status": "candidate",
                "early_stage": True,
                "trained_at": utc_now_iso(),
                "feature_names": ["price"],
                "parameters": {
                    "label_version": LabelPolicy().label_version,
                    "economic_objective_version": ECONOMIC_OBJECTIVE_VERSION,
                    "decision_policy_version": DECISION_POLICY_VERSION,
                    "age_policy_version": DEFAULT_AGE_POLICY_VERSION,
                    "deployment_fit_scope": "final_train_only_certified_instance",
                    "deployment_certification": certificate,
                },
                "thresholds": {"decision": 0.2 + slot * 0.01},
                "metrics": {
                    "composite_score": 0.8 - slot * 0.1,
                    "execution_risk": {"certified": True},
                    "deployment_certification": certificate,
                },
                "artifact_path": f"ml_models/{model_id}.joblib",
            }
        )
        generation.append(
            {
                "id": model_id,
                "algorithm": algorithm,
                "composite_score": 0.8 - slot * 0.1,
                "threshold": 0.2 + slot * 0.01,
                "metrics": {"deployment_certification": certificate},
            }
        )
    if activate:
        repo.set_active_models(generation)
    return generation


@pytest.mark.asyncio
async def test_completed_candidate_waits_for_model_flat_but_rules_only_remains_continuous(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    current = _register_generation(database, "current", activate=True)
    paper = PaperTradingService(database, settings)
    session_id = paper.ensure_simulation_session()["id"]

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
    database.execute(
        """
        INSERT INTO positions(
            id,token_address,account_kind,strategy_key,status,simulation_session_id,
            entry_time,expires_at,invested_usd,net_pnl_usd
        ) VALUES('rules-open','rules-token','simulation','rules_only','open',?,?,?,50,NULL)
        """,
        (
            session_id,
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
                "deployment_certification": {
                    "version": DEPLOYMENT_CERTIFICATION_VERSION,
                    "deployment_fit_scope": "final_train_only_certified_instance",
                    "eligible": True,
                    "blockers": [],
                },
                "activation": {"status": "waiting_for_flat"},
            }),
        ),
    )

    first_worker = TrainingWorker(database, settings)
    assert await first_worker.run_once() is None
    assert [model["id"] for model in ModelRepository(database).active_models()] == [item["id"] for item in current]
    assert database.get_runtime_state("model_entries_paused_for_rollover") is True
    assert database.get_runtime_state("model_rollover_status")["state"] == "waiting_for_flat"

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
    assert status["session"]["id"] == session_id
    assert status["accounts"]["rules_only"]["open_positions"] == 1
    assert status["accounts"]["rules_only"]["capital_mode"] == "unlimited_notional"
    assert "cash_usd" not in status["accounts"]["rules_only"]
    for slot in (1, 2, 3):
        account = status["accounts"][f"model_{slot}"]
        assert account["capital_mode"] == "unlimited_notional"
        assert "cash_usd" not in account
        assert account["invested_usd"] == pytest.approx(0.0)
        assert account["realized_pnl_usd"] == pytest.approx(0.0)
        assert account["total_fees_usd"] == pytest.approx(0.0)
        assert account["open_positions"] == 0
        assert account["trade_count"] == 0
        assert account["precision"] is None
        assert account["recall"] is None
        assert account["model_id"] == candidate[slot - 1]["id"]

def test_contract_upgrade_invalidates_waiting_generation_and_releases_stale_pause(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    current = _register_generation(database, "current-version", activate=True)
    candidate = _register_generation(database, "obsolete-pending", activate=False)
    run_id = "obsolete-v6-generation"
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
                "deployment_certification": {
                    "version": "phase16_final_recent_certification_v6",
                    "deployment_fit_scope": "final_train_only_certified_instance",
                    "eligible": True,
                    "blockers": [],
                },
                "activation": {"status": "waiting_for_flat"},
            }),
        ),
    )
    database.set_runtime_state("model_entries_paused_for_rollover", True)
    database.set_runtime_state(
        "model_entry_rollover_gate",
        {"paused": True, "reason": "candidate_models_waiting_for_all_simulation_positions_to_close"},
    )
    database.set_runtime_state(
        "model_rollover_status",
        {"state": "waiting_for_flat", "run_id": run_id},
    )

    assert TrainingService(database, settings).promote_pending_if_flat(run_id=run_id) is None

    assert [model["id"] for model in ModelRepository(database).active_models()] == [item["id"] for item in current]
    assert database.fetch_one("SELECT promoted FROM training_runs WHERE id=?", (run_id,))["promoted"] == 0
    assert database.get_runtime_state("model_entries_paused_for_rollover") is False
    gate = database.get_runtime_state("model_entry_rollover_gate")
    assert gate["paused"] is False
    assert gate["reason"] == "pending_generation_no_longer_matches_current_contract"
    rollover = database.get_runtime_state("model_rollover_status")
    assert rollover["state"] == "invalidated_pending"
    assert rollover["run_id"] == run_id


def test_rollover_recovers_after_models_switch_before_run_commit(monkeypatch, tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    _register_generation(database, "current-crash", activate=True)
    session_id = PaperTradingService(database, settings).ensure_simulation_session()["id"]
    candidate = _register_generation(database, "candidate-crash", activate=False)
    run_id = "pending-crash-recovery"
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
                "deployment_certification": {
                    "version": DEPLOYMENT_CERTIFICATION_VERSION,
                    "deployment_fit_scope": "final_train_only_certified_instance",
                    "eligible": True,
                    "blockers": [],
                },
                "activation": {"status": "waiting_for_flat"},
            }),
        ),
    )
    service = TrainingService(database, settings)
    original_reset = PaperTradingService.reset_model_accounts_for_activation

    def crash_before_account_reset(self, *args, **kwargs):
        raise RuntimeError("simulated crash after active model swap")

    monkeypatch.setattr(PaperTradingService, "reset_model_accounts_for_activation", crash_before_account_reset)
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.promote_pending_if_flat(run_id=run_id)

    assert [row["id"] for row in ModelRepository(database).active_models()] == [row["id"] for row in candidate]
    assert database.fetch_one("SELECT promoted FROM training_runs WHERE id=?", (run_id,))["promoted"] == 0
    assert database.get_runtime_state("model_entries_paused_for_rollover") is True
    assert database.get_runtime_state("model_rollover_status")["state"] == "activating"

    monkeypatch.setattr(PaperTradingService, "reset_model_accounts_for_activation", original_reset)
    recovered = TrainingService(database, settings).promote_pending_if_flat(run_id=run_id)
    assert recovered == run_id
    assert database.fetch_one("SELECT promoted FROM training_runs WHERE id=?", (run_id,))["promoted"] == 1
    assert database.get_runtime_state("model_entries_paused_for_rollover") is False
    status = PaperTradingService(database, settings).simulation_status()
    assert status["session"]["id"] == session_id
    assert status["accounts"]["rules_only"]["capital_mode"] == "unlimited_notional"
    assert TrainingService(database, settings).promote_pending_if_flat(run_id=run_id) is None

def test_rollover_allows_current_certificate_when_execution_risk_is_uncertified_shadow(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    _register_generation(database, "current-risk-shadow", activate=True)
    candidate = _register_generation(database, "candidate-risk-shadow", activate=False)
    for item in candidate:
        row = database.fetch_one("SELECT metrics_json FROM models WHERE id=?", (item["id"],))
        metrics = json.loads(row["metrics_json"] or "{}")
        metrics["execution_risk"] = {"certified": False, "reason": "shadow_unavailable"}
        database.execute(
            "UPDATE models SET metrics_json=? WHERE id=?",
            (json.dumps(metrics), item["id"]),
        )
    run_id = "risk-shadow-does-not-block-rollover"
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
                "deployment_certification": {
                    "version": DEPLOYMENT_CERTIFICATION_VERSION,
                    "deployment_fit_scope": "final_train_only_certified_instance",
                    "eligible": True,
                    "blockers": [],
                },
                "activation": {"status": "waiting_for_flat"},
            }),
        ),
    )

    promoted = TrainingService(database, settings).promote_pending_if_flat(run_id=run_id)

    assert promoted == run_id
    assert database.fetch_one("SELECT promoted FROM training_runs WHERE id=?", (run_id,))["promoted"] == 1
    assert [row["id"] for row in ModelRepository(database).active_models()] == [row["id"] for row in candidate]


def test_rollover_refuses_candidate_without_deployment_certification(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    _register_generation(database, "current-uncert", activate=True)
    candidate = _register_generation(database, "candidate-uncert", activate=False)
    run_id = "uncertified-candidate"
    database.execute(
        """
        INSERT INTO training_runs(
            id,trigger,status,requested_at,completed_at,request_json,promoted,summary_json
        ) VALUES(?, 'manual', 'completed', ?, ?, '{}', 0, ?)
        """,
        (
            run_id, utc_now_iso(), utc_now_iso(),
            json.dumps({
                "top_models": candidate,
                "deployment_certification": {"eligible": False, "blockers": ["final_drift_severe"]},
                "activation": {"status": "blocked_certification"},
            }),
        ),
    )
    assert TrainingService(database, settings).promote_pending_if_flat(run_id=run_id) is None
    assert database.fetch_one("SELECT promoted FROM training_runs WHERE id=?", (run_id,))["promoted"] == 0
    assert [row["id"] for row in ModelRepository(database).active_models()] != [row["id"] for row in candidate]
