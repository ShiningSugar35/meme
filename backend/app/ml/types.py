from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

import numpy as np
import pandas as pd


UtilityUnit = Literal["usd", "legacy_proxy"]


@dataclass(frozen=True)
class EconomicSlice:
    """Economic inputs for one evaluation slice.

    ``utility_eligible`` is deliberately strict.  It is true only when every
    row has the real entry-time liquidity and every tag=2 row has its actual
    two-hour close ratio.  Legacy CSV rows can still train a classifier, but
    their equal-weight proxy must never be presented as dollar PnL.
    """

    capital: np.ndarray
    realized_return: np.ndarray
    utility_eligible: bool
    unit: UtilityUnit
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedDataset:
    X: pd.DataFrame
    y: pd.Series
    tags: pd.Series
    timestamps: pd.Series
    liquidity_usd: pd.Series
    final_close_ratio: pd.Series
    return_is_estimated: pd.Series
    feature_names: tuple[str, ...]
    dropped_columns: tuple[str, ...]
    source_rows: pd.Index

    def __len__(self) -> int:
        return len(self.X)

    def economic_slice(self, indices: np.ndarray | list[int]) -> EconomicSlice:
        positions = np.asarray(indices, dtype=int)
        tags = self.tags.iloc[positions].to_numpy(dtype=int)
        close_ratio = self.final_close_ratio.iloc[positions].to_numpy(dtype=float)
        liquidity = self.liquidity_usd.iloc[positions].to_numpy(dtype=float)
        estimated = self.return_is_estimated.iloc[positions].to_numpy(dtype=bool)

        realized = np.full(len(positions), -0.10, dtype=float)
        realized[tags == 1] = 0.60
        tag2 = tags == 2
        realized[tag2] = close_ratio[tag2] - 1.0

        blockers: list[str] = []
        real_liquidity = np.isfinite(liquidity) & (liquidity > 0)
        if not bool(real_liquidity.all()):
            blockers.append("raw entry-time liquidity is missing for one or more rows")
        if bool(estimated.any()):
            blockers.append("actual two-hour close ratio is missing for one or more tag=2 rows")

        utility_eligible = not blockers
        if utility_eligible:
            capital = np.minimum(0.01 * liquidity, 50.0)
            unit: UtilityUnit = "usd"
        else:
            # Equal-weight proxy is useful for threshold/model diagnostics but
            # has no currency interpretation and cannot authorize promotion.
            capital = np.ones(len(positions), dtype=float)
            unit = "legacy_proxy"

        return EconomicSlice(
            capital=capital,
            realized_return=realized,
            utility_eligible=utility_eligible,
            unit=unit,
            blockers=tuple(blockers),
        )


@dataclass(frozen=True)
class TemporalFold:
    name: str
    train_indices: np.ndarray
    test_indices: np.ndarray


@dataclass(frozen=True)
class TemporalPlan:
    development_folds: tuple[TemporalFold, ...]
    final_split: TemporalFold
    refit_indices: np.ndarray
    active_indices: np.ndarray
    early_stage: bool
    data_start: datetime
    data_end: datetime
    gap_hours: float

    @property
    def stage_label(self) -> str:
        return "EARLY_STAGE_MODEL" if self.early_stage else "STANDARD_120D_MODEL"


@dataclass(frozen=True)
class EvaluationMetrics:
    threshold: float
    precision: float
    recall: float
    trade_count: int
    true_positives: int
    false_positives: int
    cumulative_pnl_usd: float | None
    proxy_pnl: float | None
    smooth_utility: float
    max_drawdown_usd: float | None
    max_drawdown_proxy: float | None
    selected_capital: float
    opportunity_capital: float
    sample_count: int
    utility_unit: UtilityUnit
    utility_eligible: bool
    blockers: tuple[str, ...] = ()

    @property
    def comparable_pnl(self) -> float:
        return (
            float(self.cumulative_pnl_usd)
            if self.cumulative_pnl_usd is not None
            else float(self.proxy_pnl or 0.0)
        )

    @property
    def comparable_drawdown(self) -> float:
        return (
            float(self.max_drawdown_usd)
            if self.max_drawdown_usd is not None
            else float(self.max_drawdown_proxy or 0.0)
        )


@dataclass(frozen=True)
class ThresholdSet:
    aggressive: float
    balanced: float
    conservative: float

    def for_profile(self, profile: str) -> float:
        if profile not in {"aggressive", "balanced", "conservative"}:
            raise ValueError(f"unknown threshold profile: {profile}")
        return float(getattr(self, profile))

    def as_dict(self) -> dict[str, float]:
        return {
            "aggressive": self.aggressive,
            "balanced": self.balanced,
            "conservative": self.conservative,
        }


@dataclass(frozen=True)
class CandidateEvaluation:
    algorithm: str
    complexity_rank: int
    status: Literal["ok", "skipped", "failed"]
    skip_reason: str | None = None
    thresholds: ThresholdSet | None = None
    development_metrics: EvaluationMetrics | None = None
    final_metrics: EvaluationMetrics | None = None
    fold_metrics: tuple[EvaluationMetrics, ...] = ()
    selection_score: float | None = None


@dataclass
class ModelBundle:
    model_id: str
    algorithm: str
    estimator: Any
    feature_names: tuple[str, ...]
    thresholds: ThresholdSet
    created_at: datetime
    early_stage: bool
    training_start: datetime
    training_end: datetime
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def predict_probabilities(self, frame: pd.DataFrame) -> np.ndarray:
        missing = [name for name in self.feature_names if name not in frame.columns]
        if missing:
            raise ValueError(f"missing model features: {missing}")
        probabilities = self.estimator.predict_proba(frame.loc[:, self.feature_names])
        classes = np.asarray(getattr(self.estimator, "classes_", [0, 1]))
        positive_columns = np.flatnonzero(classes == 1)
        if probabilities.ndim != 2 or len(positive_columns) != 1:
            raise ValueError("estimator did not return binary class probabilities")
        return np.asarray(probabilities[:, int(positive_columns[0])], dtype=float)


@dataclass(frozen=True)
class TrainingResult:
    bundle: ModelBundle
    evaluation_bundle: ModelBundle
    plan: TemporalPlan
    candidates: tuple[CandidateEvaluation, ...]
    selected_algorithm: str
    final_metrics: EvaluationMetrics
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PromotionDecision:
    eligible: bool
    promote: bool
    candidate_metrics: EvaluationMetrics
    incumbent_metrics: EvaluationMetrics
    pnl_lift: float | None
    blockers: tuple[str, ...]
    comparison_rows: int
