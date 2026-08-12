from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from backend.app.collector.models import Kline
from backend.app.config import Settings
from backend.app.database import Database, utc_now_iso
from backend.app.ml.registry import ModelRegistry
from backend.app.ml.types import ModelBundle, ThresholdSet
from backend.app.repositories.models import ModelRepository
from backend.app.repositories.samples import SampleRecord, SampleRepository
from backend.app.services.paper_position_monitor import PaperPositionMonitor
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


def seed_top3(database: Database, model_dir, now: datetime, *, probability: float = 0.60, training_end: datetime | None = None, activation_at: datetime | None = None, feature_names=("feature_a",)) -> None:
    registry = ModelRegistry(model_dir)
    models = ModelRepository(database)
    thresholds = (0.20, 0.50, 0.80)
    active = []
    for slot in (1, 2, 3):
        model_id = f"model-{slot}"
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
                "parameters": {},
                "thresholds": bundle.thresholds.as_dict(),
                "metrics": {"composite_score": 0.8 - slot * 0.1},
                "artifact_path": str(artifact),
            }
        )
        active.append({"id": model_id, "composite_score": 0.8 - slot * 0.1, "threshold": thresholds[slot - 1], "metrics": {}})
    models.set_active_models(active)
    selected_at = (activation_at or (now - timedelta(seconds=30))).isoformat()
    database.execute("UPDATE active_model_slots SET selected_at=?", (selected_at,))


def test_prediction_cycle_scores_top3_opens_models_and_rule_baseline_idempotently(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        sqlite_path=str(tmp_path / "test.db"),
        background_workers_enabled=False,
        signal_max_age_seconds=300,
        paper_market_monitor_enabled=False,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seed_sol_price(database, now - timedelta(seconds=10))
    seed_sol_price(database, now + timedelta(hours=1) - timedelta(seconds=10))
    seed_top3(database, tmp_path / "models", now)
    repository = SampleRepository(database)
    repository.insert(
        SampleRecord(
            address="Token111111111111111111111111111111111111",
            token_type="new_creation",
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
    repository.update_label(sample_id, tag=1, max_ratio=1.6, min_ratio=0.95, final_close_ratio=1.6)
    third = service.run_cycle(now=now + timedelta(hours=1, seconds=1))
    assert third.paper_positions_settled == 3
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions WHERE status='closed'")["n"] == 3


def test_stale_oos_signals_and_rule_baseline_are_not_retroactively_filled(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    settings = Settings(_env_file=None, sqlite_path=str(tmp_path / "test.db"), background_workers_enabled=False, signal_max_age_seconds=60)
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


def test_rollover_gate_blocks_all_four_simulation_strategies(tmp_path):
    database = Database(tmp_path / "rollover.db")
    database.initialize()
    settings = Settings(
        _env_file=None,
        sqlite_path=str(tmp_path / "rollover.db"),
        background_workers_enabled=False,
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
    assert result.blocked_signals == 4
    assert result.rule_positions_opened == 0
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions WHERE strategy_key='rules_only'")["n"] == 0
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
        )
        artifact = registry.save(bundle)
        models.register({"id": model_id, "version": model_id, "algorithm": "constant", "status": "candidate", "early_stage": True, "trained_at": now.isoformat(), "training_window_start": int((now - timedelta(days=10)).timestamp()), "training_window_end": int((now - timedelta(minutes=1)).timestamp()), "feature_names": ["price", "feature_a"], "parameters": {}, "thresholds": bundle.thresholds.as_dict(), "metrics": {}, "artifact_path": str(artifact)})
        active.append({"id": model_id, "composite_score": 0.8 - slot * 0.1, "threshold": bundle.threshold, "metrics": {}})
    models.set_active_models(active)

    entry = now - timedelta(seconds=10)
    database.execute("UPDATE active_model_slots SET selected_at=?", ((entry - timedelta(seconds=1)).isoformat(),))
    seed_sol_price(database, entry)
    SampleRepository(database).insert(
        SampleRecord(
            address="Asset333333333333333333333333333333333333",
            token_type="new_creation",
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
    assert {row["exit_reason"] for row in rows} == {"take_profit_1_6x"}
    assert len({row["simulation_session_id"] for row in rows}) == 1
