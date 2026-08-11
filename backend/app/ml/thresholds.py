from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .economics import evaluate_probabilities
from .types import EconomicSlice, EvaluationMetrics, ThresholdSet


@dataclass(frozen=True)
class ThresholdSearchConfig:
    min_trades: int = 5
    min_trade_fraction: float = 0.01
    require_positive_expectancy: bool = True
    temperature: float = 0.03


@dataclass(frozen=True)
class ThresholdSearchResult:
    thresholds: ThresholdSet
    metrics: EvaluationMetrics


def _threshold_grid(probabilities: np.ndarray) -> np.ndarray:
    probs = np.asarray(probabilities, dtype=float)
    quantiles = np.quantile(probs, np.linspace(0.01, 0.99, 99))
    regular = np.linspace(0.01, 0.99, 99)
    candidates = np.unique(np.concatenate((quantiles, regular, [0.0, 1.0])))
    return np.clip(candidates, 0.0, 1.0)


def optimize_thresholds(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    economics: EconomicSlice,
    config: ThresholdSearchConfig | None = None,
) -> ThresholdSearchResult:
    """Pick one model threshold by fixed-payoff OOS profit.

    A +60% winner on a $50 notional contributes +6 payoff units and a -10%
    loser contributes -1 unit. The threshold is selected after model fitting,
    so the discontinuous p/r profit identity is never used as a training loss.
    """
    cfg = config or ThresholdSearchConfig()
    minimum_trades = max(
        cfg.min_trades,
        int(np.ceil(len(probabilities) * cfg.min_trade_fraction)),
    )
    evaluations = [
        evaluate_probabilities(
            y_true,
            probabilities,
            float(threshold),
            economics,
            temperature=cfg.temperature,
        )
        for threshold in _threshold_grid(probabilities)
    ]
    eligible = [metric for metric in evaluations if metric.trade_count >= minimum_trades]
    if cfg.require_positive_expectancy:
        positive = [metric for metric in eligible if metric.profit_units > 0]
        if positive:
            eligible = positive
    if not eligible:
        raise ValueError("no threshold satisfies the minimum trade count")

    selected = max(
        eligible,
        key=lambda item: (
            item.profit_units,
            item.economic_capture,
            item.recall,
            item.precision,
            item.trade_count,
            -item.threshold,
        ),
    )
    return ThresholdSearchResult(
        thresholds=ThresholdSet(decision=selected.threshold),
        metrics=selected,
    )
