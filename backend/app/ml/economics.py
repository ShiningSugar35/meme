from __future__ import annotations

import numpy as np

from .types import EconomicSlice, EvaluationMetrics


def capital_from_liquidity(liquidity_usd: np.ndarray) -> np.ndarray:
    liquidity = np.asarray(liquidity_usd, dtype=float)
    if not np.isfinite(liquidity).all() or (liquidity <= 0).any():
        raise ValueError("real liquidity must be finite and positive")
    return np.minimum(0.01 * liquidity, 50.0)


def classification_sample_weights(economics: EconomicSlice) -> np.ndarray:
    """Equal classifier weights; economics are reserved for OOS selection.

    Profit magnitude must not redefine the class prior seen by the classifier.
    Otherwise a +60% positive and -10% negative make one positive observation
    behave like roughly six negatives, which destroys probability calibration
    and generalization. Trading economics remain fully active in threshold and
    model selection after the classifier has produced out-of-sample scores.
    """

    return np.ones(len(economics.capital), dtype=float)


def economic_sample_weights(economics: EconomicSlice) -> np.ndarray:
    """Backward-compatible alias for the classification fit weights."""

    return classification_sample_weights(economics)


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -50.0, 50.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def evaluate_probabilities(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    threshold: float,
    economics: EconomicSlice,
    *,
    temperature: float = 0.03,
) -> EvaluationMetrics:
    y = np.asarray(y_true, dtype=int)
    probs = np.asarray(probabilities, dtype=float)
    if len(y) != len(probs) or len(y) != len(economics.capital):
        raise ValueError("labels, probabilities, and economic inputs must have equal length")
    if len(y) == 0:
        raise ValueError("cannot evaluate an empty slice")
    if not np.isfinite(probs).all():
        raise ValueError("probabilities must be finite")
    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be between zero and one")

    selected = probs >= threshold
    true_positive = int(np.sum(selected & (y == 1)))
    false_positive = int(np.sum(selected & (y == 0)))
    trade_count = int(np.sum(selected))
    positive_count = int(np.sum(y == 1))
    precision = true_positive / trade_count if trade_count else 0.0
    recall = true_positive / positive_count if positive_count else 0.0

    per_row_pnl = np.where(
        selected,
        economics.capital * economics.realized_return,
        0.0,
    )
    cumulative = np.cumsum(per_row_pnl)
    peaks = np.maximum.accumulate(np.concatenate(([0.0], cumulative)))
    drawdowns = np.concatenate(([0.0], cumulative)) - peaks
    max_drawdown = float(abs(np.min(drawdowns)))
    discrete_pnl = float(np.sum(per_row_pnl))

    activation = _sigmoid((probs - threshold) / max(temperature, 1e-6))
    smooth_utility = float(
        np.sum(activation * economics.capital * economics.realized_return)
    )

    if economics.utility_eligible:
        pnl_usd: float | None = discrete_pnl
        proxy_pnl: float | None = None
        drawdown_usd: float | None = max_drawdown
        drawdown_proxy: float | None = None
    else:
        pnl_usd = None
        proxy_pnl = discrete_pnl
        drawdown_usd = None
        drawdown_proxy = max_drawdown

    return EvaluationMetrics(
        threshold=float(threshold),
        precision=float(precision),
        recall=float(recall),
        trade_count=trade_count,
        true_positives=true_positive,
        false_positives=false_positive,
        cumulative_pnl_usd=pnl_usd,
        proxy_pnl=proxy_pnl,
        smooth_utility=smooth_utility,
        max_drawdown_usd=drawdown_usd,
        max_drawdown_proxy=drawdown_proxy,
        selected_capital=float(np.sum(economics.capital[selected])),
        opportunity_capital=float(np.sum(economics.capital)),
        sample_count=len(y),
        utility_unit=economics.unit,
        utility_eligible=economics.utility_eligible,
        blockers=economics.blockers,
    )


def model_selection_score(
    metrics: EvaluationMetrics,
    worst_fold_pnl_rate: float,
    complexity_rank: int,
) -> float:
    """Unitless score dominated by cumulative chronological utility.

    The denominator is total opportunity capital, not selected-trade capital.
    Therefore this preserves the ranking of cumulative PnL on one shared
    window and does not reward a model merely for taking very few trades.
    It also permits a legacy-proxy development window and a later real-dollar
    window to contribute without adding unlike units.
    """

    scale = max(metrics.opportunity_capital, 1.0)
    pnl_rate = metrics.comparable_pnl / scale
    smooth_rate = metrics.smooth_utility / scale
    drawdown_rate = metrics.comparable_drawdown / scale
    activity = metrics.trade_count / max(metrics.sample_count, 1)
    complexity_cost = 0.001 * complexity_rank
    return float(
        0.55 * pnl_rate
        + 0.25 * smooth_rate
        + 0.15 * worst_fold_pnl_rate
        - 0.05 * drawdown_rate
        + 0.01 * activity
        - complexity_cost
    )
