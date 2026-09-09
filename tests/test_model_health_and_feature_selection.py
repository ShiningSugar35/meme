from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.app.collector.constants import LabelPolicy
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.ml.decision_policy import DECISION_POLICY_VERSION
from backend.app.ml.economics import ECONOMIC_OBJECTIVE_VERSION
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
        model_degraded_ratio=0.70,
        model_monitor_cooldown_hours=24,
    )


def seed_top3(database: Database, now: datetime, *, baseline_capture: float = 0.50) -> list[str]:
    repo = ModelRepository(database)
    ids = []
    active = []
    for slot in (1, 2, 3):
        model_id = f"health-model-{slot}"
        ids.append(model_id)
        repo.register(
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
                "parameters": {
                    "requested_feature_pool": ["price", "price_change_1h"],
                    "label_version": LabelPolicy().label_version,
                    "economic_objective_version": ECONOMIC_OBJECTIVE_VERSION,
                    "decision_policy_version": DECISION_POLICY_VERSION,
                },
                "thresholds": {"decision": 0.5},
                "metrics": {"final_recent_window": {"economic_capture": baseline_capture}},
                "artifact_path": "ml_models/not-needed-for-health.joblib",
            }
        )
        active.append({"id": model_id, "composite_score": 0.6 - slot * 0.05, "threshold": 0.5, "metrics": {}})
    repo.set_active_models(active)
    database.execute(
        "UPDATE active_model_slots SET selected_at=?",
        ((now - timedelta(days=1)).isoformat(),),
    )
    return ids


def seed_recent_predictions(database: Database, model_ids: list[str], now: datetime, tags: list[int]) -> None:
    repo = SampleRepository(database)
    for index, tag in enumerate(tags):
        entry = now - timedelta(hours=len(tags) - index)
        repo.insert(
            SampleRecord(
                address=f"health-token-{index}",
                token_type="new_creation",
                entry_time=int(entry.timestamp()),
                entry_price=1.0,
                liquidity=10_000.0,
                features={"price_change_1h": float(index)},
                tag=tag,
                label_status="mature",
            )
        )
        sample_id = int(database.fetch_one("SELECT id FROM samples WHERE address=? ORDER BY id DESC LIMIT 1", (f"health-token-{index}",))["id"])
        for slot, model_id in enumerate(model_ids, start=1):
            database.execute(
                """
                INSERT INTO predictions(
                    sample_id,model_id,probability,strategy_key,threshold,selected,
                    decision_policy_version,decision_reason,predicted_at
                ) VALUES(?,?,0.9,?,0.5,1,?,'selected',?)
                """,
                (sample_id, model_id, f"model_{slot}", DECISION_POLICY_VERSION, entry.isoformat()),
            )


def test_deprecated_absolute_liquidity_feature_is_filtered(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    service = TrainingService(database, make_settings(tmp_path))
    defaults = service.normalize_feature_selection(None)
    assert "ln(price+1)" not in defaults
    assert "price_change_1h" not in defaults
    assert "top_bot_degen_percentage" not in defaults
    assert "dexscr_ad" not in defaults
    assert "dexscr_trending_bar" not in defaults
    assert "momentum_accel_1m_vs_5m" in defaults
    assert "ln(liquidity_usd)" not in defaults
    selected = service.normalize_feature_selection(["price", "ln(liquidity_usd)"])
    assert selected == ("ln(marketcap/liquidity)",)
    migrated = service.normalize_feature_selection(["price_change_1h", "top_bot_degen_percentage"])
    assert migrated == ("bot_degen_rate", "momentum_accel_1m_vs_5m")
    with pytest.raises(ValueError):
        service.normalize_feature_selection(["ln(liquidity_usd)"])
    with pytest.raises(ValueError):
        service.normalize_feature_selection(["launchpad"])
    run_id = service.create_run("manual", feature_names=list(selected))
    row = database.fetch_one("SELECT request_json FROM training_runs WHERE id=?", (run_id,))
    assert json.loads(row["request_json"])["feature_names"] == list(selected)


def test_v9_feature_pool_migration_keeps_v8_choices_and_enrolls_current_candidates(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    database.set_runtime_state(
        "model_training_feature_pool:event1m_regime_v8",
        ["ln(marketcap+1)", "price_change_1h", "monitor_private_pump_buy_ratio_15m"],
    )
    selected = TrainingService(database, make_settings(tmp_path)).configured_feature_selection()
    assert "ln(marketcap+1)" not in selected
    assert "ln(marketcap/liquidity)" in selected
    assert "momentum_accel_1m_vs_5m" in selected
    assert "buy_swap_ratio_1h" in selected
    assert "ln(creator_launches_24h+1)" in selected
    assert "monitor_fomo_buy_ratio_15m" in selected
    assert "monitor_private_fomo_buy_ratio_15m" in selected
    assert "monitor_private_pump_buy_ratio_15m" not in selected
    assert "monitor_source_coverage" not in selected
    assert "monitor_private_source_coverage" not in selected


def test_insufficient_recent_predictions_do_not_queue_retraining(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    model_ids = seed_top3(database, now)
    seed_recent_predictions(database, model_ids, now, [0, 1, 0])
    report = ModelHealthService(database, settings).evaluate(now=now)
    assert report.state == "insufficient_data"
    assert not report.degraded
    assert report.training_run_id is None
    assert database.fetch_one("SELECT COUNT(*) AS n FROM training_runs")["n"] == 0


def test_active_model_drift_marks_health_for_scheduled_update(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    model_ids = seed_top3(database, now)
    seed_recent_predictions(database, model_ids, now, [1, 0, 1, 0, 1])
    database.set_runtime_state(
        f"model_drift:{model_ids[1]}",
        {"state": "caution", "reasons": ["feature_psi_caution"]},
    )

    report = ModelHealthService(database, settings).evaluate(now=now)

    assert report.state == "drift_detected"
    assert not report.degraded
    assert report.training_run_id is None
    assert "accelerate scheduled model update" in report.reason


def test_fixed_payoff_degradation_is_reported_without_out_of_band_retraining(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    model_ids = seed_top3(database, now, baseline_capture=0.50)
    # One win + four losses => (6-4)/(6*1)=0.333 < 70% * 0.50.
    seed_recent_predictions(database, model_ids, now, [0, 0, 0, 0, 1])
    first = ModelHealthService(database, settings).evaluate(now=now)
    second = ModelHealthService(database, settings).evaluate(now=now + timedelta(minutes=1))
    assert first.degraded
    assert first.state == "degraded"
    assert first.training_run_id is None
    assert second.training_run_id is None
    # Automatic retraining cadence is owned by TrainingScheduler so every
    # update obeys the 16:00 freeze / 17:00 train / flat-then-activate lifecycle.
    assert database.fetch_one("SELECT COUNT(*) AS n FROM training_runs")["n"] == 0


def test_healthy_fixed_payoff_window_does_not_retrain(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    model_ids = seed_top3(database, now, baseline_capture=0.50)
    seed_recent_predictions(database, model_ids, now, [1, 1, 1, 1, 0])
    report = ModelHealthService(database, settings).evaluate(now=now)
    assert report.state == "healthy"
    assert not report.degraded
    assert report.training_run_id is None


def test_rank1_rollback_requires_loadable_retired_artifact_and_rebuilds_top3(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    registry = ModelRegistry(tmp_path / "models")
    repo = ModelRepository(database)
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)

    def register(model_id: str, with_artifact: bool = False) -> None:
        artifact = tmp_path / "models" / f"{model_id}.joblib"
        if with_artifact:
            bundle = ModelBundle(
                model_id=model_id,
                algorithm="logistic_regression",
                estimator={"fixture": model_id},
                feature_names=("price",),
                thresholds=ThresholdSet(decision=0.4),
                created_at=now,
                early_stage=True,
                training_start=now - timedelta(days=10),
                training_end=now - timedelta(days=1),
            )
            artifact = registry.save(bundle)
        repo.register({"id": model_id, "version": model_id, "algorithm": "logistic_regression", "status": "candidate", "early_stage": True, "trained_at": now.isoformat(), "feature_names": ["price"], "parameters": {}, "thresholds": {"decision": 0.4}, "metrics": {}, "artifact_path": str(artifact)})

    for model_id in ("rollback-old", "support-2", "support-3"):
        register(model_id, with_artifact=model_id == "rollback-old")
    repo.set_active_models([
        {"id": "rollback-old", "composite_score": 0.6, "threshold": 0.4, "metrics": {}},
        {"id": "support-2", "composite_score": 0.5, "threshold": 0.4, "metrics": {}},
        {"id": "support-3", "composite_score": 0.4, "threshold": 0.4, "metrics": {}},
    ])
    register("rollback-new")
    repo.set_active_models([
        {"id": "rollback-new", "composite_score": 0.7, "threshold": 0.4, "metrics": {}},
        {"id": "support-2", "composite_score": 0.5, "threshold": 0.4, "metrics": {}},
        {"id": "support-3", "composite_score": 0.4, "threshold": 0.4, "metrics": {}},
    ])
    assert repo.get("rollback-old")["status"] == "retired"

    restored = TrainingService(database, settings).rollback_model("rollback-old")
    assert restored["id"] == "rollback-old"
    assert repo.active_models()[0]["id"] == "rollback-old"
    assert len(repo.active_models()) == 3

def test_stale_phase16_contract_fails_model_health_closed(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    seed_top3(database, now)
    database.execute(
        "UPDATE models SET parameters_json='{}' WHERE id IN ('health-model-1','health-model-2','health-model-3')"
    )
    report = ModelHealthService(database, settings).evaluate(now=now)
    assert report.state == "active_top3_contract_stale"
    assert not report.degraded
    assert report.training_run_id is None
