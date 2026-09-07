from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api import routes as routes_module
from backend.app.database import Database
from backend.app.services import platform_configuration as platform_configuration_module


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
    assert "ln(price+1)" not in catalog["default_features"]
    assert "ln(marketcap/liquidity)" in catalog["available_features"]
    assert "momentum_accel_1m_vs_5m" in catalog["default_features"]
    assert catalog["selected_features"] == catalog["default_features"]
    assert "price" not in catalog["available_features"]
    assert "price_change_1h" not in catalog["available_features"]
    assert "top_bot_degen_percentage" not in catalog["available_features"]
    assert "ln(liquidity_usd)" not in catalog["default_features"]
    assert len(catalog["available_features"]) == 61
    assert "monitor_private_pump_buy_ratio_15m" not in catalog["available_features"]
    assert len(catalog["default_features"]) == 29
    assert "price_change_1m" in catalog["available_features"]
    assert "price_change_1m" not in catalog["default_features"]
    assert "price_change_2m" not in catalog["available_features"]
    assert "ln(swaps_1m+1)" not in catalog["available_features"]
    assert "ln(volume_2m+1)" not in catalog["available_features"]
    assert "creator_token_status" not in catalog["available_features"]
    assert "volume_acceleration_2m" not in catalog["available_features"]
    assert "ln(liquidity_usd)" not in catalog["available_features"]

    saved = client.put(
        "/api/models/feature-selection",
        json={"features": ["ln(price+1)", "price_change_1h"]},
    )
    assert saved.status_code == 200
    assert saved.json()["selected_features"] == [
        "momentum_accel_1m_vs_5m",
        "ln(marketcap/liquidity)",
    ]
    assert client.get("/api/models").json()["feature_catalog"]["selected_features"] == [
        "momentum_accel_1m_vs_5m",
        "ln(marketcap/liquidity)",
    ]

    response = client.post(
        "/api/models/train",
        json={"reason": "api-smoke"},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    row = database.fetch_one(
        "SELECT status,request_json FROM training_runs WHERE id=?",
        (run_id,),
    )
    assert row is not None and row["status"] == "queued"
    assert '"ln(marketcap/liquidity)"' in row["request_json"]
    assert '"momentum_accel_1m_vs_5m"' in row["request_json"]
    assert '"ln(price+1)"' not in row["request_json"]
    assert '"price_change_1h"' not in row["request_json"]


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


def test_configuration_api_never_returns_plaintext_credentials(monkeypatch, tmp_path: Path) -> None:
    database = make_database(tmp_path)
    env_path = tmp_path / ".env"
    first = "[TEST_KEY_ONE]"
    second = "[TEST_KEY_TWO]"
    added_value = "[TEST_KEY_THREE]"
    env_path.write_text(
        f"GMGN_API_KEY_1={first}\nJUPITER_API_KEY_1={second}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(platform_configuration_module, "ENV_PATH", env_path)
    client = make_client(monkeypatch, database)

    initial = client.get("/api/configuration")
    assert initial.status_code == 200
    assert first not in initial.text
    assert second not in initial.text
    assert initial.json()["derived"]["gmgn_key_count"] == 1
    assert initial.json()["derived"]["jupiter_exit_concurrency"] == 1

    added = client.post(
        "/api/configuration/providers/gmgn/credentials",
        json={"credential": added_value},
    )
    assert added.status_code == 200
    assert added_value not in added.text
    assert added.json()["derived"]["gmgn_key_count"] == 2

    saved = client.put(
        "/api/configuration/runtime",
        json={"position_monitor_poll_seconds": 3, "gmgn_global_rps": 10},
    )
    assert saved.status_code == 200
    assert saved.json()["runtime"]["position_monitor_poll_seconds"] == 3
    assert saved.json()["runtime"]["gmgn_global_rps"] == 10

    removed = client.delete("/api/configuration/providers/gmgn/credentials/1")
    assert removed.status_code == 200
    assert added_value not in removed.text
    assert removed.json()["derived"]["gmgn_key_count"] == 1
