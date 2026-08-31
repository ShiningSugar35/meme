import numpy as np
import pytest

from backend.app.ml.decision_policy import (
    DEPLOYMENT_CERTIFICATION_VERSION,
    deployment_certification_is_current,
    deployment_model_is_qualified,
)
from backend.app.services.training import (
    FINAL_CERTIFICATION_MIN_NEGATIVES,
    FINAL_CERTIFICATION_MIN_POSITIVES,
    FINAL_CERTIFICATION_MIN_ROWS,
    evaluate_final_deployment_evidence,
)


def _good_ranked_slice() -> tuple[np.ndarray, np.ndarray]:
    labels = np.asarray([1] * 20 + [0] * 100, dtype=int)
    probabilities = np.concatenate(
        [np.linspace(0.95, 0.70, 20), np.linspace(0.60, 0.01, 100)]
    )
    return labels, probabilities


def test_current_per_model_certificate_is_explicit_and_fail_closed() -> None:
    qualified = {
        "version": DEPLOYMENT_CERTIFICATION_VERSION,
        "deployment_fit_scope": "final_train_only_certified_instance",
        "qualified_deployment_evidence": True,
    }
    unqualified = {
        "version": DEPLOYMENT_CERTIFICATION_VERSION,
        "deployment_fit_scope": "final_train_only_certified_instance",
        "qualified_deployment_evidence": False,
    }
    stale = {
        "version": "phase16_final_recent_certification_v4",
        "deployment_fit_scope": "final_train_only_certified_instance",
        "qualified_deployment_evidence": True,
    }

    assert deployment_certification_is_current(qualified)
    assert deployment_model_is_qualified(qualified)
    assert deployment_certification_is_current(unqualified)
    assert not deployment_model_is_qualified(unqualified)
    assert not deployment_certification_is_current(stale)
    assert not deployment_model_is_qualified(stale)
    assert not deployment_certification_is_current(None)


def test_same_model_positive_ranking_and_model_threshold_evidence_can_qualify() -> None:
    labels, probabilities = _good_ranked_slice()
    selected = np.zeros_like(labels, dtype=bool)
    selected[:4] = True

    result = evaluate_final_deployment_evidence(labels, probabilities, selected)

    assert result["support_ok"]
    assert result["ranking_evidence"]
    assert result["positive_model_threshold_evidence"]
    assert result["qualified"]
    assert result["average_precision"] > result["prevalence"]
    assert result["roc_auc"] >= 0.50
    assert result["profit_units"] > 0


def test_a_single_losing_model_threshold_observation_never_qualifies() -> None:
    labels, probabilities = _good_ranked_slice()
    selected = np.zeros_like(labels, dtype=bool)
    selected[-1] = True

    result = evaluate_final_deployment_evidence(labels, probabilities, selected)

    assert result["ranking_evidence"]
    assert not result["positive_model_threshold_evidence"]
    assert not result["qualified"]
    assert "final_model_threshold_profit_not_positive" in result["blockers"]


def test_reversed_ranking_is_blocked_even_when_manual_policy_mask_wins() -> None:
    labels, good_probabilities = _good_ranked_slice()
    selected = np.zeros_like(labels, dtype=bool)
    selected[:4] = True

    result = evaluate_final_deployment_evidence(
        labels,
        1.0 - good_probabilities,
        selected,
    )

    assert result["positive_model_threshold_evidence"]
    assert not result["ranking_evidence"]
    assert not result["qualified"]
    assert "final_roc_auc_below_random" in result["blockers"]


def test_small_or_class_sparse_final_window_fails_closed() -> None:
    labels = np.asarray([1] * 5 + [0] * 45, dtype=int)
    probabilities = np.linspace(0.95, 0.01, len(labels))
    selected = np.zeros_like(labels, dtype=bool)
    selected[:2] = True

    result = evaluate_final_deployment_evidence(labels, probabilities, selected)

    assert FINAL_CERTIFICATION_MIN_ROWS == 100
    assert FINAL_CERTIFICATION_MIN_POSITIVES == 10
    assert FINAL_CERTIFICATION_MIN_NEGATIVES == 30
    assert not result["support_ok"]
    assert not result["qualified"]
    assert "final_rows_below_minimum" in result["blockers"]
    assert "final_positives_below_minimum" in result["blockers"]


def test_certification_inputs_must_be_aligned_binary_and_finite() -> None:
    with pytest.raises(ValueError, match="must align"):
        evaluate_final_deployment_evidence(
            np.asarray([0, 1]),
            np.asarray([0.1]),
            np.asarray([False, True]),
        )
    with pytest.raises(ValueError, match="binary"):
        evaluate_final_deployment_evidence(
            np.asarray([0, 2]),
            np.asarray([0.1, 0.9]),
            np.asarray([False, True]),
        )
    with pytest.raises(ValueError, match="finite"):
        evaluate_final_deployment_evidence(
            np.asarray([0, 1]),
            np.asarray([0.1, np.nan]),
            np.asarray([False, True]),
        )
