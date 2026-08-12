from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

import numpy as np
import pandas as pd


UtilityUnit = Literal["usd", "legacy_proxy"]


@dataclass(frozen=True)
class EconomicSlice:
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
    execution_invested_usd: pd.Series
    execution_net_pnl_usd: pd.Series
    execution_observed: pd.Series
    feature_names: tuple[str, ...]
    dropped_columns: tuple[str, ...]
    source_rows: pd.Index

    def __len__(self) -> int:
        return len(self.X)

    def economic_slice(self, indices: np.ndarray | list[int]) -> EconomicSlice:
        positions = np.asarray(indices, dtype=int)
        tags = self.tags.iloc[positions].to_numpy(dtype=int)
        liquidity = self.liquidity_usd.iloc[positions].to_numpy(dtype=float)

        realized = np.full(len(positions), -0.10, dtype=float)
        realized[tags == 1] = 0.60

        blockers: list[str] = []
        real_liquidity = np.isfinite(liquidity) & (liquidity > 0)
        if not bool(real_liquidity.all()):
            blockers.append("raw entry-time liquidity is missing for one or more rows")

        utility_eligible = not blockers
        if utility_eligible:
            capital = np.minimum(0.01 * liquidity, 50.0)
            unit: UtilityUnit = "usd"
        else:
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
    positive_count: int
    profit_units: float
    fixed_profit_usd: float
    economic_capture: float
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
    """One decision threshold per model.

    The historical three-profile threshold design is intentionally gone. The
    name is retained only to keep old registry imports stable.
    """

    decision: float

    def as_dict(self) -> dict[str, float]:
        return {"decision": float(self.decision)}


@dataclass(frozen=True)
class GeneralizationMetrics:
    average_precision_mean: float
    average_precision_std: float
    average_precision_skill_mean: float
    stability_score: float
    decay_score: float
    score: float


@dataclass(frozen=True)
class CandidateEvaluation:
    algorithm: str
    complexity_rank: int
    status: Literal["ok", "skipped", "failed"]
    skip_reason: str | None = None
    threshold: float | None = None
    feature_names: tuple[str, ...] = ()
    development_metrics: EvaluationMetrics | None = None
    final_metrics: EvaluationMetrics | None = None
    fold_metrics: tuple[EvaluationMetrics, ...] = ()
    generalization: GeneralizationMetrics | None = None
    economic_score: float | None = None
    composite_score: float | None = None
    score_standard_error: float | None = None
    selection_score: float | None = None
    execution_score: float | None = None
    execution_observations: int = 0
    execution_selected: int = 0
    execution_net_pnl_usd: float | None = None
    execution_weight: float = 0.0
    ranking_economic_score: float | None = None


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

    @property
    def threshold(self) -> float:
        return float(self.thresholds.decision)

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
    bundles: tuple[ModelBundle, ...]
    evaluation_bundles: tuple[ModelBundle, ...]
    plan: TemporalPlan
    candidates: tuple[CandidateEvaluation, ...]
    top_algorithms: tuple[str, ...]
    rule_baseline: EvaluationMetrics
    warnings: tuple[str, ...] = ()

    @property
    def bundle(self) -> ModelBundle:
        return self.bundles[0]

    @property
    def evaluation_bundle(self) -> ModelBundle:
        return self.evaluation_bundles[0]

    @property
    def selected_algorithm(self) -> str:
        return self.top_algorithms[0]

    @property
    def final_metrics(self) -> EvaluationMetrics:
        final = self.candidates_by_algorithm[self.top_algorithms[0]].final_metrics
        assert final is not None
        return final

    @property
    def candidates_by_algorithm(self) -> dict[str, CandidateEvaluation]:
        return {item.algorithm: item for item in self.candidates if item.status == "ok"}


@dataclass(frozen=True)
class PromotionDecision:
    eligible: bool
    promote: bool
    candidate_metrics: EvaluationMetrics
    incumbent_metrics: EvaluationMetrics
    pnl_lift: float | None
    blockers: tuple[str, ...]
    comparison_rows: int
