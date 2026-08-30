from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from backend.app.collector.constants import LabelPolicy
from backend.app.collector.models import Kline
from backend.app.config import Settings
from backend.app.database import Database, utc_now_iso
from backend.app.ml.calibration import SigmoidCalibrator
from backend.app.ml.decision_policy import (
    DECISION_POLICY_VERSION,
    DEFAULT_AGE_POLICY_VERSION,
    DEPLOYMENT_CERTIFICATION_VERSION,
)
from backend.app.ml.economics import ECONOMIC_OBJECTIVE_VERSION
from backend.app.ml.sparse_budget import SparseBudgetSelection
from backend.app.ml.registry import ModelRegistry
from backend.app.ml.types import ModelBundle, ThresholdSet
from backend.app.repositories.models import ModelRepository
from backend.app.repositories.samples import SampleRecord, SampleRepository
from backend.app.services.drift import DriftDecision
from backend.app.services.paper_position_monitor import PaperPositionMonitor
from backend.app.services.paper_trading import PaperTradingService
from backend.app.services.prediction import PredictionService


def seed_sol_price(database: Database, at: datetime, price: float = 180.0) -> None:
    database.execute(
        "INSERT OR REPLACE INTO asset_usd_prices(asset,observed_at,price_usd,source,recorded_at) VALUES('SOL',?,?,?,?)",
        (int(at.timestamp()), price, "test", utc_now_iso()),
    )


class ConstantEstimator:
    classes_ = np.asarray([0, 1])

    def __init__(self, probability: float) -> None:
        self.probability = probability

    def predict_proba(self, frame):
        return np.asarray([[1.0 - self.probability, self.probability] for _ in range(len(frame))])


class ConstantRiskModel:
    def __init__(self, probability: float = 0.10) -> None:
        self.probability = probability

    def predict_probability(self, row) -> float:
        return self.probability


def current_bundle_kwargs(*, risk_probability: float = 0.10) -> dict:
    return {
        "calibrator": SigmoidCalibrator(
            method="chronological_sigmoid_platt_v1",
            coefficient=1.0, intercept=0.0, source_rows=100, source_index_hash="test",
        ),
        "sparse_budget": SparseBudgetSelection(
            budget_fraction=0.10, budget_threshold=0.25, policy_base_threshold=0.25,
            oos_selected_count=10, precision=0.40, wilson_lower_bound=0.20,
            profit_units=2.0, sample_count=100,
        ),
        "decision_policy_version": DECISION_POLICY_VERSION,
        "age_policy_version": DEFAULT_AGE_POLICY_VERSION,
        "label_version": LabelPolicy().label_version,
        "economic_objective_version": ECONOMIC_OBJECTIVE_VERSION,
        "execution_risk_model": ConstantRiskModel(risk_probability),
        "drift_reference": {
            "version": "test", "reference_rows": 100, "reference_label_prior": 0.20,
            "feature_stats": {}, "certified": True,
        },
    }


def current_deployment_certification(*, qualified: bool = True) -> dict:
    return {
        "version": DEPLOYMENT_CERTIFICATION_VERSION,
        "deployment_fit_scope": "final_train_only_certified_instance",
        "qualified_deployment_evidence": qualified,
    }


def current_model_parameters(*, qualified: bool = True) -> dict:
    return {
        "label_version": LabelPolicy().label_version,
        "economic_objective_version": ECONOMIC_OBJECTIVE_VERSION,
        "decision_policy_version": DECISION_POLICY_VERSION,
        "age_policy_version": DEFAULT_AGE_POLICY_VERSION,
        "deployment_fit_scope": "final_train_only_certified_instance",
        "deployment_certification": current_deployment_certification(
            qualified=qualified
        ),
    }


def seed_top3(database: Database, model_dir, now: datetime, *, probability: float = 0.60, risk_probability: float = 0.10, training_end: datetime | None = None, activation_at: datetime | None = None, feature_names=("feature_a",), qualified_slots=(1, 2, 3)) -> None:
    registry = ModelRegistry(model_dir)
    models = ModelRepository(database)
    thresholds = (0.20, 0.50, 0.80)
    active = []
    qualified_slot_set = {int(slot) for slot in qualified_slots}
    for slot in (1, 2, 3):
        model_id = f"model-{slot}"
        qualified = slot in qualified_slot_set
        certificate = current_deployment_certification(qualified=qualified)
        bundle = ModelBundle(
            model_id=model_id,
            algorithm="constant",
            estimator=ConstantEstimator(probability),
            feature_names=tuple(feature_names),
            thresholds=ThresholdSet(decision=thresholds[slot - 1]),
            created_at=now,
            early_stage=True,
            training_start=now - timedelta(days=10),
            training_end=training_end or (now - timedelta(seconds=20)),
            **current_bundle_kwargs(risk_probability=risk_probability),
        )
        artifact = registry.save(bundle)
        models.register(
            {
                "id": model_id,
                "version": model_id,
                "algorithm": "constant",
                "status": "candidate",
                "early_stage": True,
                "trained_at": now.isoformat(),
                "training_window_start": int((now - timedelta(days=10)).timestamp()),
                "training_window_end": int(bundle.training_end.timestamp()),
                "validation_window_start": None,
                "validation_window_end": None,
                "feature_names": list(feature_names),
                "parameters": current_model_parameters(qualified=qualified),
                "thresholds": bundle.thresholds.as_dict(),
                "metrics": {
                    "composite_score": 0.8 - slot * 0.1,
                    "deployment_certification": certificate,
                },
                "artifact_path": str(artifact),
            }
        )
        active.append({"id": model_id, "composite_score": 0.8 - slot * 0.1, "threshold": thresholds[slot - 1], "metrics": {"deployment_certification": certificate}})
    models.set_active_models(active)
    selected_at = (activation_at or (now - timedelta(seconds=30))).isoformat()
    database.execute("UPDATE active_model_slots SET selected_at=?", (selected_at,))


def test_stale_economic_objective_keeps_models_off_and_rules_only_running(tmp_path):
    database = Database(tmp_path / "stale-objective.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        sqlite_path=str(tmp_path / "stale-objective.db"),
        background_workers_enabled=False,
        modeling_min_mature_samples=0,
        signal_max_age_seconds=300,
        paper_market_monitor_enabled=False,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seed_sol_price(database, now - timedelta(seconds=10))
    seed_top3(database, tmp_path / "stale-models", now)
    database.execute("UPDATE models SET parameters_json='{}'")
    SampleRepository(database).insert(
        SampleRecord(
            address="StaleObjective111111111111111111111111111111",
            token_type="new_creation",
            age_minutes=30.0,
            entry_time=int((now - timedelta(seconds=10)).timestamp()),
            entry_price=1.0,
            liquidity=10_000.0,
            features={"feature_a": 1.0},
            label_status="pending",
        )
    )

    result = PredictionService(database, settings).run_cycle(now=now)

    assert result.reason == "active_top3_contract_stale"
    assert result.predictions_written == 0
    assert result.model_positions_opened == 0
    assert result.rule_positions_opened == 1
    assert database.fetch_one("SELECT COUNT(*) AS n FROM predictions")["n"] == 0
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions WHERE strategy_key='rules_only'")["n"] == 1


def test_prediction_cycle_scores_top3_opens_models_and_rule_baseline_idempotently(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        sqlite_path=str(tmp_path / "test.db"),
        background_workers_enabled=False,
        modeling_min_mature_samples=0,
        signal_max_age_seconds=300,
        paper_market_monitor_enabled=False,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seed_sol_price(database, now - timedelta(seconds=10))
    seed_sol_price(database, now + timedelta(minutes=90) - timedelta(seconds=10))
    seed_top3(database, tmp_path / "models", now)
    repository = SampleRepository(database)
    repository.insert(
        SampleRecord(
            address="Token111111111111111111111111111111111111",
            token_type="new_creation",
            age_minutes=30.0,
            entry_time=int((now - timedelta(seconds=10)).timestamp()),
            entry_price=1.0,
            liquidity=4_000.0,
            launchpad="Pump.fun",
            features={"feature_a": 7.0},
            label_status="pending",
        )
    )

    service = PredictionService(database, settings)
    first = service.run_cycle(now=now)
    assert first.samples_scored == 3
    assert first.predictions_written == 3
    assert first.signals_selected == 2
    assert first.model_positions_opened == 2
    assert first.rule_positions_opened == 1
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions")["n"] == 3
    assert {row["strategy_key"] for row in database.fetch_all("SELECT strategy_key FROM positions")} == {"model_1", "model_2", "rules_only"}

    second = service.run_cycle(now=now + timedelta(seconds=1))
    assert second.predictions_written == 0
    assert second.model_positions_opened == 0
    assert second.rule_positions_opened == 0
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions")["n"] == 3

    sample_id = int(database.fetch_one("SELECT id FROM samples")["id"])
    repository.update_label(sample_id, tag=1, max_ratio=1.8, min_ratio=0.95, final_close_ratio=1.8)
    third = service.run_cycle(now=now + timedelta(minutes=90, seconds=1))
    assert third.paper_positions_settled == 3
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions WHERE status='closed'")["n"] == 3


def test_stale_oos_signals_and_rule_baseline_are_not_retroactively_filled(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    settings = Settings(_env_file=None, sqlite_path=str(tmp_path / "test.db"), background_workers_enabled=False, modeling_min_mature_samples=0, signal_max_age_seconds=60)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seed_top3(
        database,
        tmp_path / "models",
        now,
        probability=0.99,
        training_end=now - timedelta(hours=3),
        activation_at=now - timedelta(hours=3),
    )
    SampleRepository(database).insert(
        SampleRecord(
            address="Token222222222222222222222222222222222222",
            token_type="new_creation",
            age_minutes=30.0,
            entry_time=int((now - timedelta(hours=2)).timestamp()),
            entry_price=1.0,
            liquidity=5_000.0,
            features={"feature_a": 1.0},
        )
    )
    result = PredictionService(database, settings).run_cycle(now=now)
    assert result.predictions_written == 3
    assert result.stale_signals == 3  # rules-only only considers the current fresh-admission window
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions")["n"] == 0


def test_rollover_gate_blocks_models_but_rules_only_continues(tmp_path):
    database = Database(tmp_path / "rollover.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        sqlite_path=str(tmp_path / "rollover.db"),
        background_workers_enabled=False,
        modeling_min_mature_samples=0,
        signal_max_age_seconds=300,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seed_top3(database, tmp_path / "models", now, probability=0.99)
    sample_at = now - timedelta(seconds=10)
    seed_sol_price(database, sample_at)
    SampleRepository(database).insert(
        SampleRecord(
            address="TokenRollover11111111111111111111111111111111",
            token_type="new_creation",
            age_minutes=30.0,
            entry_time=int(sample_at.timestamp()),
            entry_price=1.0,
            liquidity=10_000.0,
            features={"feature_a": 1.0},
        )
    )
    database.set_runtime_state("model_entries_paused_for_rollover", True)

    result = PredictionService(database, settings).run_cycle(now=now)

    assert result.predictions_written == 3
    assert result.model_positions_opened == 0
    assert result.rule_positions_opened == 1
    assert result.blocked_signals == 3
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions WHERE strategy_key='rules_only'")["n"] == 1
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions WHERE strategy_key LIKE 'model_%'")["n"] == 0


class _OneCycleMarket:
    def __init__(self, lines):
        self.lines = lines
        self.calls = 0

    async def klines(self, address, from_ts, to_ts):
        self.calls += 1
        return self.lines


def test_prediction_to_four_strategy_market_exit_e2e(tmp_path):
    database = Database(tmp_path / "e2e.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        background_workers_enabled=False,
        modeling_min_mature_samples=0,
        signal_max_age_seconds=300,
        paper_market_monitor_enabled=True,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # all three models choose the sample
    registry = ModelRegistry(tmp_path / "models")
    models = ModelRepository(database)
    active = []
    for slot in (1, 2, 3):
        model_id = f"model-e2e-{slot}"
        bundle = ModelBundle(
            model_id=model_id,
            algorithm="constant",
            estimator=ConstantEstimator(0.99),
            feature_names=("price", "feature_a"),
            thresholds=ThresholdSet(decision=0.2 + slot * 0.05),
            created_at=now,
            early_stage=True,
            training_start=now - timedelta(days=10),
            training_end=now - timedelta(minutes=1),
            **current_bundle_kwargs(),
        )
        artifact = registry.save(bundle)
        models.register({"id": model_id, "version": model_id, "algorithm": "constant", "status": "candidate", "early_stage": True, "trained_at": now.isoformat(), "training_window_start": int((now - timedelta(days=10)).timestamp()), "training_window_end": int((now - timedelta(minutes=1)).timestamp()), "feature_names": ["price", "feature_a"], "parameters": current_model_parameters(), "thresholds": bundle.thresholds.as_dict(), "metrics": {}, "artifact_path": str(artifact)})
        active.append({"id": model_id, "composite_score": 0.8 - slot * 0.1, "threshold": bundle.threshold, "metrics": {}})
    models.set_active_models(active)

    entry = now - timedelta(seconds=10)
    database.execute("UPDATE active_model_slots SET selected_at=?", ((entry - timedelta(seconds=1)).isoformat(),))
    seed_sol_price(database, entry)
    SampleRepository(database).insert(
        SampleRecord(
            address="Asset333333333333333333333333333333333333",
            token_type="new_creation",
            age_minutes=30.0,
            entry_time=int(entry.timestamp()),
            entry_price=1.0,
            liquidity=10_000.0,
            features={"feature_a": 1.0},
        )
    )

    scored = PredictionService(database, settings).run_cycle(now=now)
    assert scored.predictions_written == 3
    assert scored.model_positions_opened == 3
    assert scored.rule_positions_opened == 1
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions WHERE status='open'")["n"] == 4
    snapshots = database.fetch_all(
        "SELECT exit_policy_version,stop_loss_ratio,take_profit_ratio,max_holding_seconds FROM positions"
    )
    assert all(row["exit_policy_version"] == "m90_tp180_sl090_v2" for row in snapshots)
    assert all(row["stop_loss_ratio"] == pytest.approx(0.9) for row in snapshots)
    assert all(row["take_profit_ratio"] == pytest.approx(1.8) for row in snapshots)
    assert all(row["max_holding_seconds"] == 5400 for row in snapshots)

    trigger = int((entry + timedelta(minutes=1)).timestamp())
    market = _OneCycleMarket([Kline(trigger, 1.9, 0.95, 1.7)])
    monitored = __import__("asyncio").run(
        PaperPositionMonitor(database, settings).run_cycle(market, now_ts=int((entry + timedelta(minutes=2)).timestamp()))
    )
    assert monitored.market_requests == 1
    assert monitored.closed_positions == 4
    assert market.calls == 1
    rows = database.fetch_all("SELECT strategy_key,status,exit_reason,simulation_session_id FROM positions")
    assert all(row["status"] == "closed" for row in rows)
    assert {row["strategy_key"] for row in rows} == {"model_1", "model_2", "model_3", "rules_only"}
    assert {row["exit_reason"] for row in rows} == {"take_profit_1_8x"}
    assert len({row["simulation_session_id"] for row in rows}) == 1

def test_execution_risk_above_ceiling_blocks_models_but_rules_only_continues(tmp_path):
    database = Database(tmp_path / "high-risk.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        sqlite_path=str(tmp_path / "high-risk.db"),
        background_workers_enabled=False,
        modeling_min_mature_samples=0,
        signal_max_age_seconds=300,
        paper_market_monitor_enabled=False,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seed_sol_price(database, now - timedelta(seconds=10))
    seed_top3(database, tmp_path / "high-risk-models", now, probability=0.99, risk_probability=0.90)
    SampleRepository(database).insert(
        SampleRecord(
            address="HighRisk11111111111111111111111111111111111",
            token_type="new_creation",
            age_minutes=30.0,
            entry_time=int((now - timedelta(seconds=10)).timestamp()),
            entry_price=1.0,
            liquidity=10_000.0,
            features={"feature_a": 1.0},
        )
    )
    result = PredictionService(database, settings).run_cycle(now=now)

    assert result.predictions_written == 3
    assert result.model_positions_opened == 0
    assert result.rule_positions_opened == 1
    reasons = {row["decision_reason"] for row in database.fetch_all("SELECT decision_reason FROM predictions")}
    assert reasons == {"execution_risk_above_ceiling"}


def test_severe_drift_is_monitoring_only_for_paper(monkeypatch, tmp_path):
    database = Database(tmp_path / "severe-drift.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        sqlite_path=str(tmp_path / "severe-drift.db"),
        background_workers_enabled=False,
        modeling_min_mature_samples=0,
        signal_max_age_seconds=300,
        paper_market_monitor_enabled=False,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seed_sol_price(database, now - timedelta(seconds=10))
    seed_top3(database, tmp_path / "severe-drift-models", now, probability=0.99)
    SampleRepository(database).insert(
        SampleRecord(
            address="SevereDrift111111111111111111111111111111111",
            token_type="new_creation",
            age_minutes=30.0,
            entry_time=int((now - timedelta(seconds=10)).timestamp()),
            entry_price=1.0,
            liquidity=10_000.0,
            features={"feature_a": 1.0},
        )
    )
    service = PredictionService(database, settings)
    severe = DriftDecision(
        state="severe",
        reasons=("test_severe",),
        label_prior_delta=0.2,
        shifted_feature_fraction=0.8,
        recent_rows=100,
        recent_selected=20,
        recent_precision=0.1,
        recent_profit_units=-5.0,
    )
    monkeypatch.setattr(service.drift, "evaluate", lambda **kwargs: severe)
    result = service.run_cycle(now=now)

    assert result.predictions_written == 3
    assert result.model_positions_opened == 3
    assert result.rule_positions_opened == 1
    rows = database.fetch_all("SELECT selected,decision_reason,drift_state FROM predictions ORDER BY id")
    assert len(rows) == 3
    assert all(int(row["selected"]) == 1 for row in rows)
    assert all(row["decision_reason"] == "selected" for row in rows)
    assert all(row["drift_state"] == "severe" for row in rows)


def test_age_60_to_120_uses_model_threshold_without_abstain(tmp_path):
    database = Database(tmp_path / "age-abstain.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        sqlite_path=str(tmp_path / "age-abstain.db"),
        background_workers_enabled=False,
        modeling_min_mature_samples=0,
        signal_max_age_seconds=300,
        paper_market_monitor_enabled=False,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seed_sol_price(database, now - timedelta(seconds=10))
    seed_top3(database, tmp_path / "age-abstain-models", now, probability=0.99)
    SampleRepository(database).insert(
        SampleRecord(
            address="AgeAbstain111111111111111111111111111111111",
            token_type="new_creation",
            age_minutes=90.0,
            entry_time=int((now - timedelta(seconds=10)).timestamp()),
            entry_price=1.0,
            liquidity=10_000.0,
            features={"feature_a": 1.0},
        )
    )
    result = PredictionService(database, settings).run_cycle(now=now)

    assert result.predictions_written == 3
    assert result.model_positions_opened == 3
    assert result.rule_positions_opened == 1
    rows = database.fetch_all("SELECT threshold,policy_base_threshold,age_probability_floor,selected,decision_reason FROM predictions ORDER BY id")
    assert len(rows) == 3
    assert all(float(row["threshold"]) == pytest.approx(float(row["policy_base_threshold"])) for row in rows)
    assert all(float(row["age_probability_floor"]) == pytest.approx(float(row["policy_base_threshold"])) for row in rows)
    assert all(int(row["selected"]) == 1 and row["decision_reason"] == "selected" for row in rows)


def test_only_individually_qualified_models_can_open_entries(tmp_path):
    database = Database(tmp_path / "qualified-only.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        sqlite_path=str(tmp_path / "qualified-only.db"),
        background_workers_enabled=False,
        modeling_min_mature_samples=0,
        signal_max_age_seconds=300,
        paper_market_monitor_enabled=False,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seed_sol_price(database, now - timedelta(seconds=10))
    seed_top3(
        database,
        tmp_path / "qualified-models",
        now,
        probability=0.99,
        qualified_slots=(1,),
    )
    sample = SampleRecord(
        address="qualified-only",
        entry_time=int((now - timedelta(seconds=10)).timestamp()),
        entry_price=1.0,
        features={"feature_a": 1.0},
        age_minutes=30.0,
        liquidity=10_000.0,
    )
    setattr(sample, "token_" + "type", "new_" + "creation")
    SampleRepository(database).insert(sample)

    result = PredictionService(database, settings).run_cycle(now=now)

    assert result.model_ids == ("model-1", "model-2", "model-3")
    assert result.tradable_model_ids == ("model-1",)
    assert result.shadow_model_ids == ("model-2", "model-3")
    assert result.predictions_written == 1
    assert result.shadow_predictions_written == 2
    assert result.shadow_signals_selected == 2
    assert result.signals_selected == 1
    assert result.model_positions_opened == 1
    assert result.rule_positions_opened == 1
    strategies = {row["strategy_key"] for row in database.fetch_all("SELECT strategy_key FROM positions")}
    assert strategies == {"model_1", "rules_only"}
    reasons = {
        row["strategy_key"]: row["decision_reason"]
        for row in database.fetch_all("SELECT strategy_key,decision_reason FROM predictions")
    }
    assert reasons["model_1"] == "selected"
    assert reasons["shadow_model_2"] == "shadow_selected"
    assert reasons["shadow_model_3"] == "shadow_selected"




def test_blocked_generation_shadow_is_non_trading_idempotent_and_adaptive_isolated(tmp_path):
    database = Database(tmp_path / "blocked-shadow.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        sqlite_path=str(tmp_path / "blocked-shadow.db"),
        background_workers_enabled=False,
        modeling_min_mature_samples=0,
        signal_max_age_seconds=300,
        paper_market_monitor_enabled=False,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    completed_at = now - timedelta(seconds=30)
    seed_sol_price(database, now - timedelta(seconds=10))
    seed_top3(
        database,
        tmp_path / "blocked-shadow-models",
        now,
        probability=0.99,
        qualified_slots=(),
    )
    database.execute("DELETE FROM active_model_slots")
    top_models = [
        {"id": f"model-{slot}", "algorithm": "constant"}
        for slot in (1, 2, 3)
    ]
    database.execute(
        """
        INSERT INTO training_runs(
            id,trigger,status,requested_at,completed_at,request_json,promoted,summary_json
        ) VALUES('blocked-shadow-run','manual','completed',?,?, '{}',0,?)
        """,
        (
            completed_at.isoformat(),
            completed_at.isoformat(),
            __import__('json').dumps({
                "decision_policy_version": DECISION_POLICY_VERSION,
                "top_models": top_models,
                "deployment_certification": {
                    "version": DEPLOYMENT_CERTIFICATION_VERSION,
                    "deployment_fit_scope": "final_train_only_certified_instance",
                    "eligible": False,
                    "blockers": ["no_positive_end_to_end_evidence"],
                },
                "activation": {"status": "blocked_certification"},
            }),
        ),
    )
    sample = SampleRecord(
        address="blocked-shadow-sample",
        entry_time=int((now - timedelta(seconds=10)).timestamp()),
        entry_price=1.0,
        features={"feature_a": 1.0},
        age_minutes=30.0,
        liquidity=10_000.0,
    )
    setattr(sample, "token_" + "type", "new_" + "creation")
    sample_id = SampleRepository(database).insert(sample)

    service = PredictionService(database, settings)
    first = service.run_cycle(now=now)
    assert first.shadow_model_ids == ("model-1", "model-2", "model-3")
    assert first.shadow_predictions_written == 3
    assert first.shadow_signals_selected == 3
    assert first.model_positions_opened == 0
    assert first.rule_positions_opened == 1
    assert {row["strategy_key"] for row in database.fetch_all("SELECT strategy_key FROM predictions")} == {
        "shadow_model_1", "shadow_model_2", "shadow_model_3"
    }
    assert {row["strategy_key"] for row in database.fetch_all("SELECT strategy_key FROM positions")} == {"rules_only"}

    second = service.run_cycle(now=now + timedelta(seconds=1))
    assert second.shadow_predictions_written == 0
    assert database.fetch_one("SELECT COUNT(*) AS n FROM predictions")["n"] == 3

    database.execute(
        "UPDATE samples SET label_status='mature',tag=1,label_version=? WHERE id=?",
        (LabelPolicy().label_version, sample_id),
    )
    service.run_cycle(now=now + timedelta(hours=2))
    assert service.adaptive.settle_feedback() == 0
    assert database.fetch_one("SELECT COUNT(*) AS n FROM adaptive_policy_feedback")["n"] == 0
    shadow_health = database.get_runtime_state("shadow_model_health")
    assert shadow_health["state"] == "observing"
    assert all(item["mature_predictions"] == 1 for item in shadow_health["models"])


def test_simulation_ignores_legacy_cash_balance_and_keeps_notional_trading(tmp_path):
    database = Database(tmp_path / "unlimited-notional.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        sqlite_path=str(tmp_path / "unlimited-notional.db"),
        background_workers_enabled=False,
        modeling_min_mature_samples=0,
        signal_max_age_seconds=300,
        paper_market_monitor_enabled=False,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    entry = now - timedelta(seconds=10)
    seed_sol_price(database, entry)
    seed_top3(database, tmp_path / "unlimited-notional-models", now, probability=0.99)
    paper = PaperTradingService(database, settings)
    session = paper.ensure_simulation_session()
    for strategy in ("model_1", "model_2", "model_3", "rules_only"):
        database.set_runtime_state(
            f"portfolio_strategy:{strategy}",
            {
                "session_id": session["id"],
                "strategy_key": strategy,
                "cash_usd": -999.0,
                "initial_cash_usd": 1000.0,
                "source": "legacy_finite_cash",
            },
        )
    SampleRepository(database).insert(
        SampleRecord(
            address="UnlimitedNotional111111111111111111111111111111",
            token_type="new_creation",
            age_minutes=30.0,
            entry_time=int(entry.timestamp()),
            entry_price=1.0,
            liquidity=10_000.0,
            features={"feature_a": 1.0},
        )
    )

    result = PredictionService(database, settings).run_cycle(now=now)

    assert result.model_positions_opened == 3
    assert result.rule_positions_opened == 1
    status = paper.simulation_status()
    for account in status["accounts"].values():
        assert account["capital_mode"] == "unlimited_notional"
        assert "cash_usd" not in account
        assert "initial_cash_usd" not in account
