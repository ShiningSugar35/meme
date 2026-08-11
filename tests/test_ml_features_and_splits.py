from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.app.ml import FeatureBuilder, TemporalSplitConfig, TemporalSplitter
from backend.app.ml.features import FeaturePolicy, LAUNCHPAD_MODEL_FEATURES, MODEL_TRAINING_FEATURES


def _frame(days: int = 60, rows_per_day: int = 4, *, liquidity: bool = True):
    rows = days * rows_per_day
    start = pd.Timestamp("2026-01-01", tz="UTC")
    time = start + pd.to_timedelta(np.arange(rows) * (24 / rows_per_day), unit="h")
    score = np.linspace(-2.0, 2.0, rows)
    tag = np.where(score > 0.6, 1, 0)
    data = {
        "address": [f"token-{i}" for i in range(rows)],
        "name": [f"Token {i}" for i in range(rows)],
        "symbol": [f"T{i}" for i in range(rows)],
        "type": "new_creation",
        "time": [int(value.timestamp()) for value in time],
        "price": 1.0,
        "price_2h_max/price": np.where(tag == 1, 1.6, 1.1),
        "price_2h_min/price": np.where(tag == 0, 0.9, 1.0),
        "final_2h_close_ratio": np.nan,
        "feature_score": score,
        "launchpad": np.where(np.arange(rows) % 2, "Pump.fun", "letsbonk"),
        "price_change_1h": score * 10,
        "tag": tag,
    }
    if liquidity:
        data["liquidity"] = np.linspace(4_800, 12_000, rows)
    return pd.DataFrame(data)


def test_feature_builder_excludes_ids_time_and_future_outcomes() -> None:
    prepared = FeatureBuilder().prepare(_frame())

    forbidden = {
        "address",
        "name",
        "symbol",
        "type",
        "time",
        "price",
        "price_2h_max/price",
        "price_2h_min/price",
        "final_2h_close_ratio",
        "tag",
    }
    assert forbidden.isdisjoint(prepared.feature_names)
    assert {"feature_score", "price_change_1h", "launchpad", "liquidity"}.issubset(
        prepared.feature_names
    )
    assert prepared.y.equals(prepared.tags.eq(1).astype(int))
    assert prepared.timestamps.is_monotonic_increasing
    assert prepared.economic_slice(np.arange(len(prepared))).utility_eligible


def test_production_feature_allowlist_includes_entry_price_and_launchpad_one_hot() -> None:
    frame = _frame()
    prepared = FeatureBuilder(
        FeaturePolicy(feature_allowlist=MODEL_TRAINING_FEATURES)
    ).prepare(frame)

    assert prepared.feature_names == MODEL_TRAINING_FEATURES
    assert "price" in prepared.feature_names
    assert "launchpad" not in prepared.feature_names
    assert set(LAUNCHPAD_MODEL_FEATURES).issubset(prepared.feature_names)
    assert "liquidity" not in prepared.feature_names
    assert "liquidity_usd" not in prepared.feature_names
    assert "tag" not in prepared.feature_names
    assert prepared.X["price"].eq(1.0).all()
    assert prepared.X["launchpad::Pump.fun"].sum() > 0
    assert prepared.X["launchpad::letsbonk"].sum() > 0


def test_legacy_missing_liquidity_is_proxy_not_dollar_pnl() -> None:
    prepared = FeatureBuilder().prepare(_frame(liquidity=False))
    economics = prepared.economic_slice(np.arange(len(prepared)))

    assert not economics.utility_eligible
    assert economics.unit == "legacy_proxy"
    assert np.all(economics.capital == 1.0)
    assert any("liquidity" in blocker for blocker in economics.blockers)


def test_early_stage_split_is_expanding_and_has_two_hour_gap() -> None:
    prepared = FeatureBuilder().prepare(_frame(days=60))
    plan = TemporalSplitter(
        TemporalSplitConfig(
            min_train_rows=30,
            min_test_rows=8,
            development_folds=3,
        )
    ).build(prepared)

    assert plan.early_stage
    assert plan.stage_label == "EARLY_STAGE_MODEL"
    assert len(plan.final_split.test_indices) == pytest.approx(
        len(prepared) * 0.20, abs=1
    )
    for fold in (*plan.development_folds, plan.final_split):
        train_end = prepared.timestamps.iloc[fold.train_indices].max()
        test_start = prepared.timestamps.iloc[fold.test_indices].min()
        assert train_end + pd.Timedelta(hours=2) <= test_start
        assert fold.train_indices.max() < fold.test_indices.min()


def test_120_day_plan_uses_only_last_120_days_and_recent_30_day_holdout() -> None:
    prepared = FeatureBuilder().prepare(_frame(days=150, rows_per_day=2))
    plan = TemporalSplitter(
        TemporalSplitConfig(min_train_rows=30, min_test_rows=8)
    ).build(prepared)

    assert not plan.early_stage
    active_start = prepared.timestamps.iloc[plan.active_indices[0]]
    end = prepared.timestamps.iloc[plan.active_indices[-1]]
    assert active_start >= end - pd.Timedelta(days=120)
    holdout_start = prepared.timestamps.iloc[plan.final_split.test_indices[0]]
    assert holdout_start >= end - pd.Timedelta(days=30)
    train_end = prepared.timestamps.iloc[plan.final_split.train_indices].max()
    assert train_end + pd.Timedelta(hours=2) <= holdout_start
