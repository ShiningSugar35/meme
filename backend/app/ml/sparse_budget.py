from __future__ import annotations

from dataclasses import asdict, dataclass
import math

import numpy as np

from .economics import evaluate_probabilities
from .types import EconomicSlice, EvaluationMetrics


SPARSE_BUDGET_FRACTIONS = (0.05, 0.075, 0.10, 0.15)
# Absolute calibrated-probability values are not an economic break-even proxy.
# Profitability is certified from chronological OOS precision / +3:-1 utility;
# the sparse-budget threshold therefore carries the model's calibrated score
# scale instead of being clipped to a hard-coded 0.25.
GLOBAL_PROBABILITY_FLOOR = 0.0


@dataclass(frozen=True, slots=True)
class SparseBudgetSelection:
    budget_fraction: float
    budget_threshold: float
    policy_base_threshold: float
    oos_selected_count: int
    precision: float
    wilson_lower_bound: float
    profit_units: float
    sample_count: int

    def provenance(self) -> dict[str, object]:
        return asdict(self)


def wilson_lower_bound(successes: int, total: int, *, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    denom = 1.0 + (z * z) / total
    centre = p + (z * z) / (2.0 * total)
    spread = z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * total)) / total)
    return float(max(0.0, (centre - spread) / denom))


def _budget_threshold(probabilities: np.ndarray, fraction: float, min_trades: int) -> float:
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
    fractions: tuple[float, ...] = SPARSE_BUDGET_FRACTIONS,
    global_probability_floor: float = GLOBAL_PROBABILITY_FLOOR,
) -> tuple[SparseBudgetSelection, EvaluationMetrics]:
    y = np.asarray(y_true, dtype=int)
    probabilities = np.asarray(calibrated_probabilities, dtype=float)
    if len(y) != len(probabilities):
        raise ValueError("labels and probabilities must align")
    candidates: list[tuple[SparseBudgetSelection, EvaluationMetrics]] = []
    for fraction in fractions:
        budget_threshold = _budget_threshold(probabilities, fraction, min_trades)
        policy_threshold = max(float(global_probability_floor), budget_threshold)
        metrics = evaluate_probabilities(y, probabilities, policy_threshold, economics)
        if metrics.trade_count < min_trades:
            continue
        selection = SparseBudgetSelection(
            budget_fraction=float(fraction),
            budget_threshold=float(budget_threshold),
            policy_base_threshold=float(policy_threshold),
            oos_selected_count=int(metrics.trade_count),
            precision=float(metrics.precision),
            wilson_lower_bound=wilson_lower_bound(metrics.true_positives, metrics.trade_count),
            profit_units=float(metrics.profit_units),
            sample_count=len(y),
        )
        candidates.append((selection, metrics))
    eligible = [item for item in candidates if item[1].precision >= 0.25 and item[1].profit_units > 0]
    if not eligible:
        raise ValueError("no development-only sparse budget clears the +3/-1 economic gate")
    return max(
        eligible,
        key=lambda item: (
            item[1].profit_units,
            item[0].wilson_lower_bound,
            item[1].precision,
            -item[0].budget_fraction,
        ),
    )
