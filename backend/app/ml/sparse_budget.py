from __future__ import annotations

from dataclasses import asdict, dataclass
import math

import numpy as np

from .economics import expected_return_score_from_counts, evaluate_probabilities
from .types import EconomicSlice, EvaluationMetrics


# Historical Phase16/17 replay constants only. Phase19 production threshold
# selection searches the complete development-OOS operating-point set.
SPARSE_BUDGET_FRACTIONS = (0.05, 0.075, 0.10, 0.15)
GLOBAL_PROBABILITY_FLOOR = 0.0


@dataclass(frozen=True, slots=True)
class SparseBudgetSelection:
    # Field names are retained for artifact/API compatibility. budget_fraction is
    # now the selected share of development OOS rows, not a preset budget.
    budget_fraction: float
    budget_threshold: float
    policy_base_threshold: float
    oos_selected_count: int
    precision: float
    wilson_lower_bound: float
    profit_units: float
    sample_count: int

    def provenance(self) -> dict[str, object]:
        payload = asdict(self)
        payload["selection_policy"] = "full_pr_expected_return_v1"
        return payload


def wilson_lower_bound(successes: int, total: int, *, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    denom = 1.0 + (z * z) / total
    centre = p + (z * z) / (2.0 * total)
    spread = z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * total)) / total)
    return float(max(0.0, (centre - spread) / denom))


def _budget_threshold(probabilities: np.ndarray, fraction: float, min_trades: int) -> float:
    """Historical/operator override for explicit fraction replay only."""
    n = len(probabilities)
    if n == 0:
        raise ValueError("cannot choose a budget on an empty slice")
    k = min(n, max(int(min_trades), int(math.ceil(n * float(fraction)))))
    ordered = np.sort(np.asarray(probabilities, dtype=float))[::-1]
    return float(ordered[k - 1])


def select_sparse_budget(
    y_true: np.ndarray,
    calibrated_probabilities: np.ndarray,
    economics: EconomicSlice,
    *,
    min_trades: int = 5,
    fractions: tuple[float, ...] | None = None,
    global_probability_floor: float = GLOBAL_PROBABILITY_FLOOR,
) -> tuple[SparseBudgetSelection, EvaluationMetrics]:
    """Select the development-OOS operating point with maximum expected return.

    Production (`fractions=None`) evaluates every distinct calibrated probability
    operating point in O(N log N) using one descending sort and cumulative TP/FP
    counts. Explicit fractions remain available only for historical replay/tests.
    """
    y = np.asarray(y_true, dtype=int)
    probabilities = np.asarray(calibrated_probabilities, dtype=float)
    if len(y) != len(probabilities):
        raise ValueError("labels and probabilities must align")
    if len(y) == 0 or not np.isfinite(probabilities).all():
        raise ValueError("development probabilities must be non-empty and finite")
    if not np.isin(y, (0, 1)).all():
        raise ValueError("development labels must be binary")

    positive_count = int(np.sum(y == 1))
    if positive_count <= 0:
        raise ValueError("development threshold search requires positive labels")

    candidates: list[tuple[float, int, int, int, float, float, float]] = []
    # tuple: threshold, selected, TP, FP, J, recall, precision
    if fractions is not None:
        for fraction in fractions:
            threshold = max(
                float(global_probability_floor),
                _budget_threshold(probabilities, fraction, min_trades),
            )
            selected = probabilities >= threshold
            count = int(selected.sum())
            if count < min_trades:
                continue
            tp = int(np.sum(selected & (y == 1)))
            fp = count - tp
            recall = tp / positive_count
            precision = tp / count
            score = expected_return_score_from_counts(tp, fp, positive_count)
            candidates.append((threshold, count, tp, fp, score, recall, precision))
    else:
        order = np.argsort(-probabilities, kind="stable")
        sorted_probs = probabilities[order]
        sorted_y = y[order]
        cumulative_tp = np.cumsum(sorted_y == 1)
        cumulative_fp = np.cumsum(sorted_y == 0)
        for index, threshold in enumerate(sorted_probs):
            # threshold >= value selects the entire tied group, so only evaluate
            # the last row of each group.
            if index + 1 < len(sorted_probs) and sorted_probs[index + 1] == threshold:
                continue
            if float(threshold) < float(global_probability_floor):
                break
            count = index + 1
            if count < min_trades:
                continue
            tp = int(cumulative_tp[index])
            fp = int(cumulative_fp[index])
            recall = tp / positive_count
            precision = tp / count
            score = expected_return_score_from_counts(tp, fp, positive_count)
            candidates.append((float(threshold), count, tp, fp, score, recall, precision))

    eligible = [item for item in candidates if item[4] > 0.0]
    if not eligible:
        raise ValueError("no development-only operating point has positive expected return")

    threshold, count, tp, fp, _, _, _ = max(
        eligible,
        key=lambda item: (
            item[4],  # J first
            item[5],  # then Recall
            item[6],  # then Precision
            wilson_lower_bound(item[2], item[1]),
        ),
    )
    metrics = evaluate_probabilities(y, probabilities, threshold, economics)
    selection = SparseBudgetSelection(
        budget_fraction=float(count / len(y)),
        budget_threshold=float(threshold),
        policy_base_threshold=float(threshold),
        oos_selected_count=int(count),
        precision=float(metrics.precision),
        wilson_lower_bound=wilson_lower_bound(tp, count),
        profit_units=float(metrics.profit_units),
        sample_count=len(y),
    )
    return selection, metrics
