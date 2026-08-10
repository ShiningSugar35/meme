from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from backend.app.collector.models import Kline
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.ml.registry import ModelRegistry
from backend.app.ml.types import ModelBundle, ThresholdSet
from backend.app.repositories.models import ModelRepository
from backend.app.repositories.samples import SampleRecord, SampleRepository
from backend.app.services.paper_position_monitor import PaperPositionMonitor
from backend.app.services.prediction import PredictionService


class ConstantEstimator:
    classes_ = np.asarray([0, 1])

    def __init__(self, probability: float) -> None:
        self.probability = probability

    def predict_proba(self, frame):
        return np.asarray(
            [[1.0 - self.probability, self.probability] for _ in range(len(frame))]
        )


def test_prediction_cycle_is_idempotent_and_settles_paper_positions(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    settings = Settings(
        sqlite_path=str(tmp_path / "test.db"),
        background_workers_enabled=False,
        signal_max_age_seconds=300,
        paper_market_monitor_enabled=False,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    model_id = "model-test"
    bundle = ModelBundle(
        model_id=model_id,
        algorithm="constant",
        estimator=ConstantEstimator(0.60),
        feature_names=("feature_a",),
        thresholds=ThresholdSet(aggressive=0.20, balanced=0.40, conservative=0.80),
        created_at=now,
        early_stage=True,
        training_start=now - timedelta(days=10),
        training_end=now - timedelta(seconds=20),
    )
    artifact = ModelRegistry(tmp_path / "models").save(bundle)
    models = ModelRepository(database)
    models.register(
        {
            "id": model_id,
            "version": model_id,
            "algorithm": "constant",
            "status": "candidate",
            "early_stage": True,
            "trained_at": now.isoformat(),
            "training_window_start": int((now - timedelta(days=10)).timestamp()),
            "training_window_end": int((now - timedelta(seconds=20)).timestamp()),
            "validation_window_start": int((now - timedelta(days=2)).timestamp()),
            "validation_window_end": int((now - timedelta(days=1)).timestamp()),
            "feature_names": ["feature_a"],
            "parameters": {},
            "thresholds": bundle.thresholds.as_dict(),
            "metrics": {},
            "artifact_path": str(artifact),
        }
    )
    models.promote(model_id)

    repository = SampleRepository(database)
    assert repository.insert(
        SampleRecord(
            address="Token111111111111111111111111111111111111",
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
    assert first.samples_scored == 1
    assert first.predictions_written == 3
    assert first.signals_selected == 2
    assert first.paper_positions_opened == 2
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions")["n"] == 2

    second = service.run_cycle(now=now + timedelta(seconds=1))
    assert second.predictions_written == 0
    assert second.paper_positions_opened == 0
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions")["n"] == 2

    sample_id = database.fetch_one("SELECT id FROM samples")["id"]
    repository.update_label(
        sample_id,
        tag=1,
        max_ratio=1.6,
        min_ratio=0.95,
        final_close_ratio=1.6,
    )
    third = service.run_cycle(now=now + timedelta(hours=2, seconds=1))
    assert third.paper_positions_settled == 2
    assert database.fetch_one(
        "SELECT COUNT(*) AS n FROM positions WHERE status='closed'"
    )["n"] == 2


def test_stale_oos_signal_is_scored_but_not_retroactively_filled(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    settings = Settings(
        sqlite_path=str(tmp_path / "test.db"),
        background_workers_enabled=False,
        signal_max_age_seconds=60,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    model_id = "model-stale"
    bundle = ModelBundle(
        model_id=model_id,
        algorithm="constant",
        estimator=ConstantEstimator(0.99),
        feature_names=("feature_a",),
        thresholds=ThresholdSet(0.2, 0.4, 0.8),
        created_at=now,
        early_stage=True,
        training_start=now - timedelta(days=10),
        training_end=now - timedelta(hours=3),
    )
    artifact = ModelRegistry(tmp_path / "models").save(bundle)
    models = ModelRepository(database)
    models.register(
        {
            "id": model_id,
            "version": model_id,
            "algorithm": "constant",
            "status": "candidate",
            "early_stage": True,
            "trained_at": now.isoformat(),
            "training_window_start": int((now - timedelta(days=10)).timestamp()),
            "training_window_end": int((now - timedelta(hours=3)).timestamp()),
            "validation_window_start": None,
            "validation_window_end": None,
            "feature_names": ["feature_a"],
            "parameters": {},
            "thresholds": bundle.thresholds.as_dict(),
            "metrics": {},
            "artifact_path": str(artifact),
        }
    )
    models.promote(model_id)
    SampleRepository(database).insert(
        SampleRecord(
            address="Token222222222222222222222222222222222222",
            entry_time=int((now - timedelta(hours=2)).timestamp()),
            entry_price=1.0,
            liquidity=5_000.0,
            features={"feature_a": 1.0},
        )
    )
    result = PredictionService(database, settings).run_cycle(now=now)
    assert result.predictions_written == 3
    assert result.stale_signals == 3
    assert database.fetch_one("SELECT COUNT(*) AS n FROM positions")["n"] == 0


class _OneCycleMarket:
    def __init__(self, lines):
        self.lines = lines
        self.calls = 0

    async def klines(self, address, from_ts, to_ts):
        self.calls += 1
        return self.lines


def test_prediction_to_three_account_market_exit_e2e(tmp_path):
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
    model_id = "model-e2e"
    bundle = ModelBundle(
        model_id=model_id,
        algorithm="constant",
        estimator=ConstantEstimator(0.99),
        feature_names=("price", "feature_a"),
        thresholds=ThresholdSet(0.2, 0.4, 0.8),
        created_at=now,
        early_stage=True,
        training_start=now - timedelta(days=10),
        training_end=now - timedelta(minutes=1),
    )
    artifact = ModelRegistry(tmp_path / "models").save(bundle)
    models = ModelRepository(database)
    models.register(
        {
            "id": model_id,
            "version": model_id,
            "algorithm": "constant",
            "status": "candidate",
            "early_stage": True,
            "trained_at": now.isoformat(),
            "training_window_start": int((now - timedelta(days=10)).timestamp()),
            "training_window_end": int((now - timedelta(minutes=1)).timestamp()),
            "validation_window_start": None,
            "validation_window_end": None,
            "feature_names": ["price", "feature_a"],
            "parameters": {},
            "thresholds": bundle.thresholds.as_dict(),
            "metrics": {},
            "artifact_path": str(artifact),
        }
    )
    models.promote(model_id)
    entry = now - timedelta(seconds=10)
    SampleRepository(database).insert(
        SampleRecord(
            address="Asset333333333333333333333333333333333333",
            entry_time=int(entry.timestamp()),
            entry_price=1.0,
            liquidity=10_000.0,
            features={"feature_a": 1.0},
        )
    )

    scored = PredictionService(database, settings).run_cycle(now=now)
    assert scored.predictions_written == 3
    assert scored.paper_positions_opened == 3
    assert database.fetch_one(
        "SELECT COUNT(*) AS n FROM positions WHERE status='open'"
    )["n"] == 3

    trigger = int((entry + timedelta(minutes=1)).timestamp())
    market = _OneCycleMarket([Kline(trigger, 1.9, 0.95, 1.7)])
    monitored = __import__("asyncio").run(
        PaperPositionMonitor(database, settings).run_cycle(
            market, now_ts=int((entry + timedelta(minutes=2)).timestamp())
        )
    )
    assert monitored.market_requests == 1
    assert monitored.closed_positions == 3
    assert market.calls == 1
    rows = database.fetch_all(
        "SELECT account_kind,status,exit_reason,simulation_session_id FROM positions"
    )
    assert all(row["status"] == "closed" for row in rows)
    assert {row["exit_reason"] for row in rows} == {"take_profit_1_6x"}
    assert len({row["simulation_session_id"] for row in rows}) == 1
