from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.app.ml import FeatureBuilder, TemporalSplitConfig, TemporalSplitter
from backend.app.ml.features import (
    AGE_LOG1P_FEATURE,
    PRICE_LOG1P_FEATURE,
    FeaturePolicy,
    MODEL_TRAINING_FEATURES,
    materialize_entry_feature,
)


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
        AGE_LOG1P_FEATURE: np.log1p(30.0),
        PRICE_LOG1P_FEATURE: np.log1p(1.0),
        "price_1h_max/price": np.where(tag == 1, 1.6, 1.1),
        "price_1h_min/price": np.where(tag == 0, 0.9, 1.0),
        "final_1h_close_ratio": np.nan,
        "label_max_price_ratio": np.where(tag == 1, 1.8, 1.2),
        "label_min_price_ratio": np.where(tag == 0, 0.8, 0.95),
        "label_final_close_ratio": np.nan,
        "label_window_seconds": 5400,
        "first_take_profit_at": np.where(tag == 1, 1_700_000_000, np.nan),
        "first_stop_loss_at": np.where(tag == 0, 1_700_000_001, np.nan),
        "price_2h_max/price": np.where(tag == 1, 1.7, 1.2),
        "price_2h_min/price": np.where(tag == 0, 0.8, 0.95),
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
        "launchpad",
        "time",
        "price",
        "price_1h_max/price",
        "price_1h_min/price",
        "final_1h_close_ratio",
        "label_max_price_ratio",
        "label_min_price_ratio",
        "label_final_close_ratio",
        "label_window_seconds",
        "first_take_profit_at",
        "first_stop_loss_at",
        "price_2h_max/price",
        "price_2h_min/price",
        "final_2h_close_ratio",
        "tag",
    }
    assert forbidden.isdisjoint(prepared.feature_names)
    assert {"feature_score", "price_change_1h", "liquidity"}.issubset(prepared.feature_names)
    assert prepared.y.equals(prepared.tags.eq(1).astype(int))
    assert prepared.timestamps.is_monotonic_increasing
    assert prepared.economic_slice(np.arange(len(prepared))).utility_eligible


def test_production_feature_allowlist_includes_entry_price_and_excludes_launchpad() -> None:
    frame = _frame()
    prepared = FeatureBuilder(
        FeaturePolicy(feature_allowlist=MODEL_TRAINING_FEATURES)
    ).prepare(frame)

    assert prepared.feature_names == MODEL_TRAINING_FEATURES
    assert len(prepared.feature_names) == len(MODEL_TRAINING_FEATURES)
    assert "price_change_1m" not in prepared.feature_names
    assert "volume_acceleration_2m" not in prepared.feature_names
    assert len(prepared.feature_names) == 31
    assert AGE_LOG1P_FEATURE in prepared.feature_names
    assert PRICE_LOG1P_FEATURE in prepared.feature_names
    assert "price" not in prepared.feature_names
    assert "launchpad" not in prepared.feature_names
    assert not any(name.startswith("launchpad::") for name in prepared.feature_names)
    assert "liquidity" not in prepared.feature_names
    assert "liquidity_usd" not in prepared.feature_names
    assert "tag" not in prepared.feature_names
    assert prepared.X[PRICE_LOG1P_FEATURE].eq(np.log1p(1.0)).all()


def test_log1p_entry_feature_materialization_keeps_legacy_models_compatible() -> None:
    legacy_age = float(np.log(9.0))
    current_age = float(np.log1p(9.0))
    entry_price = 0.001

    assert materialize_entry_feature(
        AGE_LOG1P_FEATURE, {"age": legacy_age}, entry_price=entry_price
    ) == pytest.approx(current_age)
    assert materialize_entry_feature(
        PRICE_LOG1P_FEATURE, {"age": legacy_age}, entry_price=entry_price
    ) == pytest.approx(np.log1p(entry_price))
    assert materialize_entry_feature(
        "age", {AGE_LOG1P_FEATURE: current_age}, entry_price=entry_price
    ) == pytest.approx(legacy_age)
    assert materialize_entry_feature(
        "price", {AGE_LOG1P_FEATURE: current_age}, entry_price=entry_price
    ) == pytest.approx(entry_price)


def test_legacy_missing_liquidity_is_proxy_not_dollar_pnl() -> None:
    prepared = FeatureBuilder().prepare(_frame(liquidity=False))
    economics = prepared.economic_slice(np.arange(len(prepared)))

    assert not economics.utility_eligible
    assert economics.unit == "legacy_proxy"
    assert np.all(economics.capital == 1.0)
    assert any("liquidity" in blocker for blocker in economics.blockers)


def test_early_stage_split_is_expanding_and_has_one_hour_gap() -> None:
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
        assert train_end + pd.Timedelta(hours=1) <= test_start
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
    assert train_end + pd.Timedelta(hours=1) <= holdout_start
