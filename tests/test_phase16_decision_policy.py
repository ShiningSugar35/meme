from __future__ import annotations

import numpy as np
import pytest

from backend.app.ml.calibration import fit_sigmoid_calibrator
from backend.app.ml.decision_policy import (
    age_adjusted_threshold,
    age_gate,
    clamp_adaptive_threshold,
)
from backend.app.ml.models import candidate_catalog
from backend.app.ml.sparse_budget import select_sparse_budget, wilson_lower_bound
from backend.app.ml.types import EconomicSlice


def test_sigmoid_calibration_is_monotonic_and_auditable() -> None:
    scores = np.linspace(-3.0, 3.0, 120)
    labels = (scores + np.sin(np.arange(120)) * 0.2 > 0.0).astype(int)
    indices = np.arange(200, 320)
    calibrator = fit_sigmoid_calibrator(
        scores,
        labels,
        source_indices=indices,
        source_start="2026-01-01T00:00:00+00:00",
        source_end="2026-01-02T00:00:00+00:00",
    )
    probabilities = calibrator.transform(scores)
    assert np.all(np.diff(probabilities) >= 0)
    assert calibrator.coefficient > 0
    assert calibrator.source_rows == 120
    assert len(calibrator.source_index_hash) == 64


def test_sigmoid_calibration_fails_closed_for_degenerate_labels() -> None:
    with pytest.raises(ValueError, match="both classes"):
        fit_sigmoid_calibrator(
            np.linspace(-1, 1, 40),
            np.zeros(40, dtype=int),
            source_indices=range(40),
        )


def test_full_operating_point_search_is_development_only_and_maximizes_expected_return() -> None:
    y = np.asarray([1, 1, 1, 0, 1, 0, 0, 0, 0, 0] * 20, dtype=int)
    probabilities = np.linspace(0.95, 0.05, len(y))
    economics = EconomicSlice(
        realized_return=np.where(y == 1, 0.80, -0.10).astype(float),
        capital=np.full(len(y), 50.0),
        utility_eligible=True,
        unit="usd",
    )
    selection, metrics = select_sparse_budget(y, probabilities, economics, min_trades=5)
    assert 0.0 < selection.budget_fraction <= 1.0
    assert selection.provenance()["selection_policy"] == "full_pr_expected_return_v1"
    assert selection.policy_base_threshold == pytest.approx(selection.budget_threshold)
    assert selection.sample_count == len(y)
    assert selection.oos_selected_count == metrics.trade_count
    assert selection.precision >= 0.25
    assert selection.profit_units > 0
    assert selection.wilson_lower_bound == pytest.approx(
        wilson_lower_bound(metrics.true_positives, metrics.trade_count)
    )


def test_full_operating_point_search_can_choose_more_than_old_fifteen_percent_budget() -> None:
    probabilities = np.linspace(1.0, 0.01, 100)
    y = np.zeros(100, dtype=int)
    # Ten early winners support a profitable operating point around the first
    # third; the remaining winners are deliberately late so trading everything
    # is negative. The old 15% cap could never reach this Recall.
    y[[0, 3, 6, 9, 12, 15, 18, 21, 24, 27]] = 1
    y[90:100] = 1
    economics = EconomicSlice(
        realized_return=np.where(y == 1, 0.80, -0.10).astype(float),
        capital=np.full(len(y), 50.0),
        utility_eligible=True,
        unit="usd",
    )

    selection, metrics = select_sparse_budget(y, probabilities, economics, min_trades=5)

    assert selection.budget_fraction > 0.15
    assert metrics.recall >= 0.50
    assert metrics.profit_units > 0


def test_sparse_budget_does_not_confuse_break_even_precision_with_probability_scale() -> None:
    probabilities = np.linspace(0.22, 0.05, 100)
    y = np.zeros(100, dtype=int)
    y[:5] = np.asarray([1, 1, 0, 0, 0], dtype=int)
    economics = EconomicSlice(
        realized_return=np.where(y == 1, 0.80, -0.10).astype(float),
        capital=np.full(len(y), 50.0),
        utility_eligible=True,
        unit="usd",
    )
    selection, metrics = select_sparse_budget(
        y,
        probabilities,
        economics,
        min_trades=5,
        fractions=(0.05,),
    )
    assert probabilities.max() < 0.25
    assert selection.policy_base_threshold < 0.25
    assert metrics.trade_count == 5
    assert metrics.precision == pytest.approx(0.40)
    assert metrics.profit_units > 0


def test_age_gate_is_admission_only_and_never_reweights_model_threshold() -> None:
    for age in (5, 20, 45, 60, 90, 119.9, 120, 299.9):
        decision = age_gate(age)
        assert decision.allowed
        assert decision.threshold_delta == pytest.approx(0.0)
        assert age_adjusted_threshold(0.18, decision) == pytest.approx(0.18)
    assert not age_gate(2).allowed
    assert not age_gate(300).allowed


def test_adaptive_expansive_cannot_undercut_relative_age_threshold() -> None:
    assert clamp_adaptive_threshold(0.18, 0.22, 0.20) == pytest.approx(0.22)
    assert clamp_adaptive_threshold(0.18, 0.18, 0.10) == pytest.approx(0.18)
    assert clamp_adaptive_threshold(0.18, 0.22, 0.50) == pytest.approx(0.50)


def test_rbf_svm_has_no_internal_probability_calibration() -> None:
    spec = {item.name: item for item in candidate_catalog()}["rbf_svm"]
    estimator = spec.builder()
    assert estimator.probability is False
