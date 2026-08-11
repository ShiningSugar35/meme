from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from backend.app.ml import (
    FeatureBuilder,
    ModelBundle,
    ModelTrainer,
    PromotionEvaluator,
    TemporalSplitConfig,
    ThresholdSet,
    TrainerConfig,
)
from backend.app.ml.economics import evaluate_probabilities
from backend.app.ml.models import candidate_catalog


def _learnable_frame(rows: int = 360, *, include_liquidity: bool = True) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    start = pd.Timestamp("2026-01-01", tz="UTC")
    timestamps = start + pd.to_timedelta(np.arange(rows) * 4, unit="h")
    momentum = rng.normal(size=rows)
    quality = rng.normal(size=rows)
    latent = 1.6 * momentum + 0.5 * quality + rng.normal(scale=0.45, size=rows)
    tags = np.where(latent > 0.65, 1, 0)
    frame = pd.DataFrame(
        {
            "address": [f"token-{i}" for i in range(rows)],
            "name": "meme",
            "symbol": "MEME",
            "type": "new_creation",
            "time": [int(value.timestamp()) for value in timestamps],
            "price": 1.0,
            "price_2h_max/price": np.where(tags == 1, 1.7, 1.1),
            "price_2h_min/price": np.where(tags == 0, 0.85, 0.98),
            "final_2h_close_ratio": np.nan,
            "price_change_1h": momentum,
            "fresh_wallet_rate": 1 / (1 + np.exp(-quality)),
            "launchpad": np.where(np.arange(rows) % 3, "Pump.fun", "letsbonk"),
            "tag": tags,
        }
    )
    if include_liquidity:
        frame["liquidity"] = rng.uniform(4_800, 20_000, rows)
    return frame


def test_realized_return_and_capital_formula_are_exact() -> None:
    frame = pd.DataFrame(
        {
            "time": [1_800_000_000, 1_800_000_100, 1_800_000_200],
            "feature": [0.0, 1.0, 2.0],
            "liquidity": [1_000.0, 4_000.0, 10_000.0],
            "final_2h_close_ratio": [np.nan, np.nan, 1.27],
            "tag": [0, 1, 0],
        }
    )
    prepared = FeatureBuilder().prepare(frame)
    economics = prepared.economic_slice(np.arange(3))

    assert economics.utility_eligible
    assert economics.capital.tolist() == [10.0, 40.0, 50.0]
    assert economics.realized_return.tolist() == pytest.approx([-0.10, 0.60, -0.10])
    metrics = evaluate_probabilities(
        prepared.y.to_numpy(), np.array([0.9, 0.9, 0.9]), 0.5, economics
    )
    assert metrics.cumulative_pnl_usd == pytest.approx(-1 + 24 - 5)
    assert metrics.proxy_pnl is None


def test_candidate_catalog_contains_all_five_and_xgboost_skip_is_explicit() -> None:
    catalog = {spec.name: spec for spec in candidate_catalog()}
    assert set(catalog) == {
        "logistic_regression",
        "hist_gradient_boosting",
        "xgboost",
        "extra_trees",
        "random_forest",
    }
    if not catalog["xgboost"].available:
        assert "not installed" in (catalog["xgboost"].skip_reason or "")


def test_trainer_uses_one_champion_three_thresholds_and_oos_evaluation_bundle() -> None:
    dataset = FeatureBuilder().prepare(_learnable_frame())
    trainer = ModelTrainer(
        TrainerConfig(
            candidate_names=("logistic_regression", "hist_gradient_boosting"),
            min_trades=2,
        ),
        TemporalSplitConfig(
            min_train_rows=50,
            min_test_rows=12,
            development_folds=3,
        ),
    )
    result = trainer.train(dataset)

    assert result.selected_algorithm in {
        "logistic_regression",
        "hist_gradient_boosting",
    }
    assert set(result.bundle.thresholds.as_dict()) == {
        "aggressive",
        "balanced",
        "conservative",
    }
    assert result.final_metrics.precision >= 0.20
    assert result.plan.early_stage
    assert result.bundle.early_stage
    assert result.evaluation_bundle.metrics["evaluation_only"] is True
    assert result.bundle.metrics["evaluation_only"] is False
    train_end = dataset.timestamps.iloc[result.plan.final_split.train_indices].max()
    test_start = dataset.timestamps.iloc[result.plan.final_split.test_indices].min()
    assert train_end + pd.Timedelta(hours=2) <= test_start


class _ScoreEstimator:
    def __init__(self, multiplier: float = 1.0) -> None:
        self.multiplier = multiplier

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        p = np.clip(frame["score"].to_numpy(dtype=float) * self.multiplier, 0, 1)
        return np.column_stack([1 - p, p])


def _bundle(name: str, multiplier: float, threshold: float) -> ModelBundle:
    now = datetime.now(timezone.utc)
    return ModelBundle(
        model_id=name,
        algorithm=name,
        estimator=_ScoreEstimator(multiplier),
        feature_names=("score",),
        thresholds=ThresholdSet(threshold, threshold, threshold),
        created_at=now,
        early_stage=True,
        training_start=now,
        training_end=now,
        metrics={"evaluation_only": True},
    )


def _promotion_frame(include_liquidity: bool) -> pd.DataFrame:
    score = np.array([0.95, 0.9, 0.85, 0.8, 0.2, 0.1, 0.05, 0.01])
    frame = pd.DataFrame(
        {
            "time": 1_800_000_000 + np.arange(len(score)) * 10_000,
            "score": score,
            "tag": [1, 1, 1, 1, 0, 0, 0, 0],
        }
    )
    if include_liquidity:
        frame["liquidity"] = 10_000.0
    return frame


def test_promotion_requires_same_window_real_economics_and_five_percent_lift() -> None:
    prepared = FeatureBuilder().prepare(_promotion_frame(include_liquidity=True))
    rows = np.arange(len(prepared))
    candidate = _bundle("candidate", 1.0, 0.5)
    incumbent = _bundle("incumbent", 0.45, 0.5)

    decision = PromotionEvaluator().compare(candidate, incumbent, prepared, rows)

    assert decision.eligible
    assert decision.promote
    assert decision.comparison_rows == len(rows)
    assert decision.candidate_metrics.cumulative_pnl_usd is not None
    assert decision.pnl_lift is not None and decision.pnl_lift >= 0.05


def test_legacy_proxy_can_score_but_cannot_auto_promote() -> None:
    prepared = FeatureBuilder().prepare(_promotion_frame(include_liquidity=False))
    rows = np.arange(len(prepared))
    decision = PromotionEvaluator().compare(
        _bundle("candidate", 1.0, 0.5),
        _bundle("incumbent", 0.45, 0.5),
        prepared,
        rows,
    )

    assert not decision.eligible
    assert not decision.promote
    assert decision.candidate_metrics.cumulative_pnl_usd is None
    assert decision.candidate_metrics.proxy_pnl is not None
    assert any("liquidity" in blocker for blocker in decision.blockers)
