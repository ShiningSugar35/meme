from __future__ import annotations

import numpy as np

from .types import EconomicSlice, EvaluationMetrics

FIXED_TRADE_USD = 50.0
WIN_UNITS = 6.0
LOSS_UNITS = 1.0
UNIT_USD = 5.0


def capital_from_liquidity(liquidity_usd: np.ndarray) -> np.ndarray:
    liquidity = np.asarray(liquidity_usd, dtype=float)
    if not np.isfinite(liquidity).all() or (liquidity <= 0).any():
        raise ValueError("real liquidity must be finite and positive")
    return np.minimum(0.01 * liquidity, 50.0)


def classification_sample_weights(economics: EconomicSlice) -> np.ndarray:
    """Keep classification loss independent from trading payoff magnitude."""
    return np.ones(len(economics.capital), dtype=float)


def economic_sample_weights(economics: EconomicSlice) -> np.ndarray:
    return classification_sample_weights(economics)


def theoretical_profit_units(true_positives: int, false_positives: int) -> float:
    """Net payoff units under +60% / -10% and fixed $50 entries.

    One loss is one unit (-$5) and one win is six units (+$30).
    """
    return WIN_UNITS * int(true_positives) - LOSS_UNITS * int(false_positives)


def theoretical_profit_from_precision_recall(
    precision: float,
    recall: float,
    positive_count: int = 1,
) -> float:
    """Return normalized fixed-payoff profit units from p/r.

    TP = recall * N+, FP = TP/precision - TP, therefore
    U = N+ * recall * (7 - 1/precision). This is an evaluation identity,
    not a differentiable training objective.
    """
    if precision <= 0 or recall <= 0 or positive_count <= 0:
        return 0.0
    return float(positive_count) * float(recall) * (7.0 - 1.0 / float(precision))


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

    profit_units = theoretical_profit_units(true_positive, false_positive)
    fixed_profit_usd = profit_units * UNIT_USD
    oracle_units = WIN_UNITS * positive_count
    economic_capture = profit_units / oracle_units if oracle_units > 0 else 0.0

    per_row_pnl = np.where(selected, economics.capital * economics.realized_return, 0.0)
    cumulative = np.cumsum(per_row_pnl)
    peaks = np.maximum.accumulate(np.concatenate(([0.0], cumulative)))
    drawdowns = np.concatenate(([0.0], cumulative)) - peaks
    max_drawdown = float(abs(np.min(drawdowns)))
    discrete_pnl = float(np.sum(per_row_pnl))

    activation = _sigmoid((probs - threshold) / max(temperature, 1e-6))
    smooth_utility = float(np.sum(activation * economics.capital * economics.realized_return))

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
        positive_count=positive_count,
        profit_units=float(profit_units),
        fixed_profit_usd=float(fixed_profit_usd),
        economic_capture=float(economic_capture),
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


def evaluate_rule_baseline(y_true: np.ndarray, economics: EconomicSlice) -> EvaluationMetrics:
    """Evaluate the rule-only strategy that trades every admitted sample."""
    return evaluate_probabilities(
        np.asarray(y_true, dtype=int),
        np.ones(len(y_true), dtype=float),
        0.0,
        economics,
    )


def fold_economic_score(metrics: EvaluationMetrics) -> float:
    return float(np.clip(metrics.economic_capture, -1.0, 1.0))


def model_selection_score(
    metrics: EvaluationMetrics,
    worst_fold_pnl_rate: float = 0.0,
    complexity_rank: int = 0,
) -> float:
    """Compatibility helper; new ranking is implemented in ModelTrainer.

    Returns the fixed-payoff economic capture with a tiny complexity tie-break.
    """
    return float(fold_economic_score(metrics) - 0.001 * complexity_rank)
