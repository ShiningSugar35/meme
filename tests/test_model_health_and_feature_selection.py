from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.ml.registry import ModelRegistry
from backend.app.ml.types import ModelBundle, ThresholdSet
from backend.app.repositories.models import ModelRepository
from backend.app.repositories.samples import SampleRecord, SampleRepository
from backend.app.services.model_health import ModelHealthService
from backend.app.services.training import TrainingService


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "model-health.db")
    database.initialize()
    return database


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        background_workers_enabled=False,
        model_monitor_window_days=7,
        model_monitor_min_predictions=5,
        model_monitor_min_selected=3,
        model_degraded_ratio=0.70,
        model_monitor_cooldown_hours=24,
        min_precision=0.20,
    )


def seed_champion(database: Database, now: datetime, *, utility_unit: str = "usd") -> str:
    model_id = "champion-health"
    ModelRepository(database).register(
        {
            "id": model_id,
            "version": model_id,
            "algorithm": "logistic_regression",
            "status": "candidate",
            "early_stage": False,
            "trained_at": (now - timedelta(days=10)).isoformat(),
            "training_window_start": int((now - timedelta(days=120)).timestamp()),
            "training_window_end": int((now - timedelta(days=8)).timestamp()),
            "validation_window_start": int((now - timedelta(days=7)).timestamp()),
            "validation_window_end": int((now - timedelta(days=1)).timestamp()),
            "feature_names": ["price", "price_change_1h"],
            "parameters": {},
            "thresholds": {"aggressive": 0.2, "balanced": 0.5, "conservative": 0.8},
            "metrics": {
                "precision": 0.50,
                "recall": 0.40,
                "final_recent_window": {
                    "cumulative_pnl_usd": 10.0 if utility_unit == "usd" else None,
                    "proxy_pnl": 1.0 if utility_unit != "usd" else None,
                    "selected_capital": 100.0,
                    "utility_unit": utility_unit,
                },
            },
            "artifact_path": "ml_models/not-needed-for-health.joblib",
        }
    )
    ModelRepository(database).promote(model_id)
    return model_id


def seed_recent_predictions(
    database: Database,
    model_id: str,
    now: datetime,
    tags: list[int],
    *,
    utility_eligible: bool,
) -> None:
    repo = SampleRepository(database)
    for index, tag in enumerate(tags):
        entry = now - timedelta(hours=len(tags) - index)
        final_close = 1.25 if tag == 2 else None
        repo.insert(
            SampleRecord(
                address=f"health-token-{index}",
                entry_time=int(entry.timestamp()),
                entry_price=1.0,
                liquidity=10_000.0 if utility_eligible else None,
                liquidity_estimated=not utility_eligible,
                utility_eligible=utility_eligible,
                features={"price_change_1h": float(index)},
                final_close_ratio=final_close,
                tag=tag,
                label_status="mature",
                terminal_return_estimated=not utility_eligible and tag == 2,
            )
        )
        sample_id = database.fetch_one(
            "SELECT id FROM samples WHERE address=? ORDER BY id DESC LIMIT 1",
            (f"health-token-{index}",),
        )["id"]
        database.execute(
            """
            INSERT INTO predictions(sample_id,model_id,probability,profile,threshold,selected,predicted_at)
            VALUES(?,?,0.9,'balanced',0.5,1,?)
            """,
            (sample_id, model_id, entry.isoformat()),
        )


def test_feature_selection_defaults_exclude_optional_liquidity_feature(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    service = TrainingService(database, make_settings(tmp_path))

    assert "price" in service.normalize_feature_selection(None)
    assert "ln(liquidity_usd)" not in service.normalize_feature_selection(None)
    selected = service.normalize_feature_selection(["price", "ln(liquidity_usd)"])
    assert selected == ("price", "ln(liquidity_usd)")
    with pytest.raises(ValueError):
        service.normalize_feature_selection(["launchpad"])

    run_id = service.create_run("manual", feature_names=list(selected))
    row = database.fetch_one("SELECT request_json FROM training_runs WHERE id=?", (run_id,))
    assert json.loads(row["request_json"])["feature_names"] == list(selected)


def test_legacy_economics_never_trigger_degraded_retraining(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    model_id = seed_champion(database, now, utility_unit="legacy_proxy")
    seed_recent_predictions(database, model_id, now, [0, 0, 0, 0, 1], utility_eligible=False)

    report = ModelHealthService(database, settings).evaluate(now=now)

    assert report.state == "insufficient_real_utility"
    assert not report.degraded
    assert report.training_run_id is None
    assert database.fetch_one("SELECT COUNT(*) AS n FROM training_runs")["n"] == 0


def test_real_usd_degradation_queues_one_retraining_with_champion_features(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    model_id = seed_champion(database, now)
    # Four -10% trades and one +60% trade: $10 PnL on $250 selected capital,
    # ROI 4%, below 70% of the 10% training baseline.
    seed_recent_predictions(database, model_id, now, [0, 0, 0, 0, 1], utility_eligible=True)

    first = ModelHealthService(database, settings).evaluate(now=now)
    second = ModelHealthService(database, settings).evaluate(now=now + timedelta(minutes=1))

    assert first.degraded
    assert first.state == "degraded_retraining_queued"
    assert first.training_run_id is not None
    run = database.fetch_one("SELECT trigger,status,request_json FROM training_runs WHERE id=?", (first.training_run_id,))
    assert run["trigger"] == "degraded"
    assert run["status"] == "queued"
    assert json.loads(run["request_json"])["feature_names"] == ["price", "price_change_1h"]
    assert second.training_run_id is None
    assert second.state == "degraded_waiting"


def test_healthy_real_usd_window_does_not_retrain(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    model_id = seed_champion(database, now)
    seed_recent_predictions(database, model_id, now, [1, 1, 1, 1, 0], utility_eligible=True)

    report = ModelHealthService(database, settings).evaluate(now=now)

    assert report.state == "healthy"
    assert not report.degraded
    assert report.training_run_id is None


def test_model_rollback_requires_loadable_retired_artifact_and_switches_atomically(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    registry = ModelRegistry(tmp_path / "models")
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)

    def register(model_id: str) -> None:
        bundle = ModelBundle(
            model_id=model_id,
            algorithm="logistic_regression",
            estimator={"fixture": model_id},
            feature_names=("price",),
            thresholds=ThresholdSet(0.2, 0.4, 0.8),
            created_at=now,
            early_stage=True,
            training_start=now - timedelta(days=10),
            training_end=now - timedelta(days=1),
        )
        artifact = registry.save(bundle)
        ModelRepository(database).register(
            {
                "id": model_id,
                "version": model_id,
                "algorithm": "logistic_regression",
                "status": "candidate",
                "early_stage": True,
                "trained_at": now.isoformat(),
                "feature_names": ["price"],
                "parameters": {},
                "thresholds": bundle.thresholds.as_dict(),
                "metrics": {},
                "artifact_path": str(artifact),
            }
        )

    register("rollback-old")
    register("rollback-new")
    models = ModelRepository(database)
    models.promote("rollback-old")
    models.promote("rollback-new")
    assert models.get("rollback-old")["status"] == "retired"

    restored = TrainingService(database, settings).rollback_model("rollback-old")

    assert restored["id"] == "rollback-old"
    assert models.get("rollback-old")["status"] == "champion"
    assert models.get("rollback-new")["status"] == "retired"
