from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .economics import evaluate_probabilities
from .types import EconomicSlice, EvaluationMetrics, ThresholdSet


@dataclass(frozen=True)
class ThresholdSearchConfig:
    min_precision: float = 0.20
    min_trades: int = 3
    min_trade_fraction: float = 0.01
    temperature: float = 0.03


@dataclass(frozen=True)
class ThresholdSearchResult:
    thresholds: ThresholdSet
    aggressive_metrics: EvaluationMetrics
    balanced_metrics: EvaluationMetrics
    conservative_metrics: EvaluationMetrics


def _threshold_grid(probabilities: np.ndarray) -> np.ndarray:
    probs = np.asarray(probabilities, dtype=float)
    quantiles = np.quantile(probs, np.linspace(0.01, 0.99, 99))
    regular = np.linspace(0.02, 0.98, 97)
    # Keep search O(grid * rows), not O(rows^2) on a large collection history.
    candidates = np.unique(np.concatenate((quantiles, regular, [0.0, 1.0])))
    return np.clip(candidates, 0.0, 1.0)


def optimize_thresholds(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    economics: EconomicSlice,
    config: ThresholdSearchConfig | None = None,
) -> ThresholdSearchResult:
    cfg = config or ThresholdSearchConfig()
    if not 0 <= cfg.min_precision <= 1:
        raise ValueError("min_precision must be between zero and one")
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
    eligible = [
        metric
        for metric in evaluations
        if metric.trade_count >= minimum_trades and metric.precision >= cfg.min_precision
    ]
    if not eligible:
        raise ValueError(
            "no threshold satisfies both the 20% precision gate and minimum trade count"
        )

    aggressive = max(
        eligible,
        key=lambda item: (
            item.recall,
            item.comparable_pnl,
            item.smooth_utility,
            -item.threshold,
        ),
    )
    balanced = max(
        eligible,
        key=lambda item: (
            0.65 * item.comparable_pnl
            + 0.35 * item.smooth_utility
            - 0.05 * item.comparable_drawdown,
            item.precision,
            item.trade_count,
            -item.threshold,
        ),
    )
    conservative = max(
        eligible,
        key=lambda item: (
            item.precision,
            item.comparable_pnl,
            -item.comparable_drawdown,
            item.trade_count,
            item.threshold,
        ),
    )
    thresholds = ThresholdSet(
        aggressive=aggressive.threshold,
        balanced=balanced.threshold,
        conservative=conservative.threshold,
    )
    return ThresholdSearchResult(
        thresholds=thresholds,
        aggressive_metrics=aggressive,
        balanced_metrics=balanced,
        conservative_metrics=conservative,
    )
