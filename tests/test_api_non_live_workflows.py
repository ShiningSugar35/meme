from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api import routes as routes_module
from backend.app.database import Database


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "api.db")
    database.initialize()
    return database


def make_client(monkeypatch, database: Database) -> TestClient:
    monkeypatch.setattr(routes_module, "get_database", lambda: database)
    app = FastAPI()
    app.include_router(routes_module.router)
    return TestClient(app)


def test_model_training_api_creates_durable_queue_item(monkeypatch, tmp_path: Path) -> None:
    database = make_database(tmp_path)
    client = make_client(monkeypatch, database)

    models = client.get("/api/models")
    assert models.status_code == 200
    catalog = models.json()["feature_catalog"]
    assert "ln(age+1)" in catalog["default_features"]
    assert "ln(price+1)" in catalog["default_features"]
    assert "price" not in catalog["available_features"]
    assert "ln(liquidity_usd)" not in catalog["default_features"]
    assert "ln(liquidity_usd)" in catalog["available_features"]

    response = client.post(
        "/api/models/train",
        json={"reason": "api-smoke", "features": ["ln(price+1)", "price_change_1h"]},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    row = database.fetch_one(
        "SELECT status,request_json FROM training_runs WHERE id=?",
        (run_id,),
    )
    assert row is not None and row["status"] == "queued"
    assert '"ln(price+1)"' in row["request_json"]
    assert '"price_change_1h"' in row["request_json"]


def test_simulation_api_keeps_current_and_historical_sessions_separate(monkeypatch, tmp_path: Path) -> None:
    database = make_database(tmp_path)
    client = make_client(monkeypatch, database)

    first = client.get("/api/simulation")
    assert first.status_code == 200
    first_id = first.json()["session"]["id"]
    reset = client.post("/api/simulation/reset")
    assert reset.status_code == 200
    second_id = reset.json()["session"]["id"]
    assert second_id != first_id

    history = client.get("/api/simulation/history").json()["items"]
    assert history[0]["id"] == second_id
    assert history[0]["status"] == "active"
    assert history[1]["id"] == first_id
    assert history[1]["status"] == "closed"


def test_agent_api_rejects_live_proposals_and_executes_only_after_human_approval(
    monkeypatch, tmp_path: Path
) -> None:
    database = make_database(tmp_path)
    client = make_client(monkeypatch, database)

    unsafe = client.post(
        "/api/agent/proposals",
        json={"proposal_type": "live_buy", "payload": {"token": "blocked"}},
    )
    assert unsafe.status_code == 422
    assert database.fetch_one("SELECT COUNT(*) AS n FROM agent_proposals")["n"] == 0

    safe = client.post(
        "/api/agent/proposals",
        json={"proposal_type": "pause_new_entries", "payload": {"reason": "human gate test"}},
    )
    assert safe.status_code == 202
    proposal_id = safe.json()["id"]
    assert database.get_runtime_state("new_entries_paused", False) is False

    approved = client.post(
        f"/api/agent/proposals/{proposal_id}/approve",
        json={"note": "approved in smoke test"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "executed"
    assert database.get_runtime_state("new_entries_paused") is True

    resume = client.post(
        "/api/agent/proposals",
        json={"proposal_type": "resume_new_entries", "payload": {}},
    )
    resume_id = resume.json()["id"]
    rejected = client.post(
        f"/api/agent/proposals/{resume_id}/reject",
        json={"note": "stay paused"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert database.get_runtime_state("new_entries_paused") is True

    paths = {route.path for route in routes_module.router.routes}
    assert "/api/agent/execute" not in paths
    assert "/api/agent/liquidate" not in paths
