from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import math
import uuid

import numpy as np

from .economics import economic_sample_weights, evaluate_probabilities, model_selection_score
from .models import (
    CandidateSpec,
    build_pipeline,
    candidate_catalog,
    fit_pipeline,
    positive_probabilities,
)
from .splits import TemporalSplitConfig, TemporalSplitter
from .thresholds import ThresholdSearchConfig, optimize_thresholds
from .types import (
    CandidateEvaluation,
    EvaluationMetrics,
    ModelBundle,
    PreparedDataset,
    TrainingResult,
)


@dataclass(frozen=True)
class TrainerConfig:
    random_state: int = 42
    min_precision: float = 0.35
    min_trades: int = 3
    candidate_names: tuple[str, ...] = (
        "logistic_regression",
        "hist_gradient_boosting",
        "xgboost",
        "extra_trees",
        "random_forest",
    )
    occam_equivalence_fraction: float = 0.02


class ModelTrainer:
    """Chronological candidate comparison and one-Champion refit."""

    def __init__(
        self,
        config: TrainerConfig | None = None,
        split_config: TemporalSplitConfig | None = None,
    ) -> None:
        self.config = config or TrainerConfig()
        self.splitter = TemporalSplitter(split_config)

    def train(self, dataset: PreparedDataset) -> TrainingResult:
        plan = self.splitter.build(dataset)
        specs = {
            spec.name: spec for spec in candidate_catalog(self.config.random_state)
        }
        unknown = sorted(set(self.config.candidate_names) - set(specs))
        if unknown:
            raise ValueError(f"unknown model candidates: {unknown}")

        evaluations: list[CandidateEvaluation] = []
        fitted_for_final: dict[str, object] = {}
        successful_specs: dict[str, CandidateSpec] = {}

        for name in self.config.candidate_names:
            spec = specs[name]
            if not spec.available:
                evaluations.append(
                    CandidateEvaluation(
                        algorithm=name,
                        complexity_rank=spec.complexity_rank,
                        status="skipped",
                        skip_reason=spec.skip_reason,
                    )
                )
                continue
            try:
                evaluation, final_estimator = self._evaluate_candidate(dataset, plan, spec)
            except Exception as exc:  # one optional/complex candidate must not abort all
                evaluations.append(
                    CandidateEvaluation(
                        algorithm=name,
                        complexity_rank=spec.complexity_rank,
                        status="failed",
                        skip_reason=f"candidate evaluation failed: {type(exc).__name__}: {exc}",
                    )
                )
                continue
            evaluations.append(evaluation)
            fitted_for_final[name] = final_estimator
            successful_specs[name] = spec

        eligible = [
            item
            for item in evaluations
            if item.status == "ok"
            and item.development_metrics is not None
            and item.final_metrics is not None
            and item.thresholds is not None
            and item.selection_score is not None
            and item.development_metrics.precision >= self.config.min_precision
            and item.final_metrics.precision >= self.config.min_precision
            and item.final_metrics.trade_count >= self.config.min_trades
        ]
        if not eligible:
            failures = "; ".join(
                f"{item.algorithm}: {item.skip_reason or 'precision gate failed'}"
                for item in evaluations
            )
            raise RuntimeError(
                "no candidate passed chronological validation and the 20% precision gate; "
                + failures
            )

        selected = self._occam_select(eligible)
        selected_spec = successful_specs[selected.algorithm]
        evaluation_estimator = fitted_for_final[selected.algorithm]

        refit = build_pipeline(selected_spec, dataset.X.iloc[plan.refit_indices])
        refit_economics = dataset.economic_slice(plan.refit_indices)
        fit_pipeline(
            refit,
            dataset.X.iloc[plan.refit_indices],
            dataset.y.iloc[plan.refit_indices],
            economic_sample_weights(refit_economics),
        )

        now = datetime.now(timezone.utc)
        model_id = f"{now:%Y%m%dT%H%M%SZ}-{selected.algorithm}-{uuid.uuid4().hex[:8]}"
        common = dict(
            model_id=model_id,
            algorithm=selected.algorithm,
            feature_names=dataset.feature_names,
            thresholds=selected.thresholds,
            created_at=now,
            early_stage=plan.early_stage,
            training_start=dataset.timestamps.iloc[plan.active_indices[0]].to_pydatetime(),
            training_end=dataset.timestamps.iloc[plan.active_indices[-1]].to_pydatetime(),
        )
        production_bundle = ModelBundle(
            estimator=refit,
            metrics={
                "stage": plan.stage_label,
                "development": asdict(selected.development_metrics),
                "final_recent_window": asdict(selected.final_metrics),
                "utility_eligible": refit_economics.utility_eligible,
                "utility_blockers": refit_economics.blockers,
                "evaluation_only": False,
            },
            **common,
        )
        # This estimator was fitted strictly before the final recent window.
        # Promotion must use it, never the all-data refit above.
        evaluation_bundle = ModelBundle(
            model_id=f"{model_id}-evaluation",
            estimator=evaluation_estimator,
            metrics={
                "stage": plan.stage_label,
                "final_recent_window": asdict(selected.final_metrics),
                "evaluation_only": True,
                "trained_through": dataset.timestamps.iloc[
                    plan.final_split.train_indices[-1]
                ].isoformat(),
                "comparison_start": dataset.timestamps.iloc[
                    plan.final_split.test_indices[0]
                ].isoformat(),
            },
            **{
                **{
                    key: value
                    for key, value in common.items()
                    if key not in {"model_id", "training_end"}
                },
                "training_end": dataset.timestamps.iloc[
                    plan.final_split.train_indices[-1]
                ].to_pydatetime(),
            },
        )

        warnings: list[str] = []
        final_economics = dataset.economic_slice(plan.final_split.test_indices)
        if not final_economics.utility_eligible:
            warnings.append(
                "recent comparison window lacks complete real economic inputs; "
                "reported utility is a legacy proxy and automatic promotion is blocked"
            )
        if plan.early_stage:
            warnings.append("model is marked EARLY_STAGE_MODEL because history is under 120 days")

        return TrainingResult(
            bundle=production_bundle,
            evaluation_bundle=evaluation_bundle,
            plan=plan,
            candidates=tuple(evaluations),
            selected_algorithm=selected.algorithm,
            final_metrics=selected.final_metrics,
            warnings=tuple(warnings),
        )

    def rebuild_evaluation_bundle(
        self,
        dataset: PreparedDataset,
        algorithm: str,
    ) -> tuple[ModelBundle, object, CandidateEvaluation]:
        """Rebuild one fixed recipe before the final holdout without selection gates.

        This is used for incumbent-vs-challenger comparisons. An incumbent may
        have degraded on the newest holdout and therefore must not be discarded
        merely because it no longer passes the candidate promotion gate.
        """
        plan = self.splitter.build(dataset)
        specs = {spec.name: spec for spec in candidate_catalog(self.config.random_state)}
        spec = specs.get(algorithm)
        if spec is None:
            raise ValueError(f"unknown model candidate: {algorithm}")
        if not spec.available:
            raise RuntimeError(spec.skip_reason or f"candidate {algorithm} is unavailable")
        evaluation, estimator = self._evaluate_candidate(dataset, plan, spec)
        if evaluation.thresholds is None:
            raise RuntimeError("incumbent recipe did not produce valid development thresholds")
        now = datetime.now(timezone.utc)
        bundle = ModelBundle(
            model_id=f"rebuild-{algorithm}-{uuid.uuid4().hex[:8]}",
            algorithm=algorithm,
            estimator=estimator,
            feature_names=dataset.feature_names,
            thresholds=evaluation.thresholds,
            created_at=now,
            early_stage=plan.early_stage,
            training_start=dataset.timestamps.iloc[plan.active_indices[0]].to_pydatetime(),
            training_end=dataset.timestamps.iloc[
                plan.final_split.train_indices[-1]
            ].to_pydatetime(),
            metrics={
                "stage": plan.stage_label,
                "final_recent_window": (
                    asdict(evaluation.final_metrics)
                    if evaluation.final_metrics is not None
                    else {}
                ),
                "evaluation_only": True,
                "trained_through": dataset.timestamps.iloc[
                    plan.final_split.train_indices[-1]
                ].isoformat(),
                "comparison_start": dataset.timestamps.iloc[
                    plan.final_split.test_indices[0]
                ].isoformat(),
            },
        )
        return bundle, plan, evaluation

    def _evaluate_candidate(self, dataset, plan, spec):
        oos_positions: list[np.ndarray] = []
        oos_probabilities: list[np.ndarray] = []

        for fold in plan.development_folds:
            y_train = dataset.y.iloc[fold.train_indices]
            if y_train.nunique() < 2:
                raise ValueError(f"{fold.name} training slice has only one class")
            estimator = build_pipeline(spec, dataset.X.iloc[fold.train_indices])
            train_economics = dataset.economic_slice(fold.train_indices)
            fit_pipeline(
                estimator,
                dataset.X.iloc[fold.train_indices],
                y_train,
                economic_sample_weights(train_economics),
            )
            oos_positions.append(fold.test_indices)
            oos_probabilities.append(
                positive_probabilities(estimator, dataset.X.iloc[fold.test_indices])
            )

        positions = np.concatenate(oos_positions)
        probabilities = np.concatenate(oos_probabilities)
        order = np.argsort(positions, kind="stable")
        positions = positions[order]
        probabilities = probabilities[order]
        development_economics = dataset.economic_slice(positions)
        threshold_result = optimize_thresholds(
            dataset.y.iloc[positions].to_numpy(dtype=int),
            probabilities,
            development_economics,
            ThresholdSearchConfig(
                min_precision=self.config.min_precision,
                min_trades=self.config.min_trades,
            ),
        )

        fold_metrics: list[EvaluationMetrics] = []
        # The concatenation follows chronological non-overlapping fold blocks.
        for fold_positions, fold_probs in zip(oos_positions, oos_probabilities, strict=True):
            metric = evaluate_probabilities(
                dataset.y.iloc[fold_positions].to_numpy(dtype=int),
                fold_probs,
                threshold_result.thresholds.balanced,
                dataset.economic_slice(fold_positions),
            )
            fold_metrics.append(metric)
        worst_fold_rate = min(
            metric.comparable_pnl / max(metric.opportunity_capital, 1.0)
            for metric in fold_metrics
        )

        final_train = plan.final_split.train_indices
        final_test = plan.final_split.test_indices
        if dataset.y.iloc[final_train].nunique() < 2:
            raise ValueError("final pre-holdout training slice has only one class")
        final_estimator = build_pipeline(spec, dataset.X.iloc[final_train])
        fit_pipeline(
            final_estimator,
            dataset.X.iloc[final_train],
            dataset.y.iloc[final_train],
            economic_sample_weights(dataset.economic_slice(final_train)),
        )
        final_probabilities = positive_probabilities(
            final_estimator, dataset.X.iloc[final_test]
        )
        final_metrics = evaluate_probabilities(
            dataset.y.iloc[final_test].to_numpy(dtype=int),
            final_probabilities,
            threshold_result.thresholds.balanced,
            dataset.economic_slice(final_test),
        )

        development_score = model_selection_score(
            threshold_result.balanced_metrics,
            worst_fold_rate,
            spec.complexity_rank,
        )
        final_score = model_selection_score(
            final_metrics,
            final_metrics.comparable_pnl
            / max(final_metrics.opportunity_capital, 1.0),
            spec.complexity_rank,
        )
        selection_score = 0.55 * development_score + 0.45 * final_score
        return (
            CandidateEvaluation(
                algorithm=spec.name,
                complexity_rank=spec.complexity_rank,
                status="ok",
                thresholds=threshold_result.thresholds,
                development_metrics=threshold_result.balanced_metrics,
                final_metrics=final_metrics,
                fold_metrics=tuple(fold_metrics),
                selection_score=float(selection_score),
            ),
            final_estimator,
        )

    def _occam_select(
        self, evaluations: list[CandidateEvaluation]
    ) -> CandidateEvaluation:
        best_score = max(float(item.selection_score) for item in evaluations)
        tolerance = max(abs(best_score) * self.config.occam_equivalence_fraction, 0.01)
        near_best = [
            item
            for item in evaluations
            if float(item.selection_score) >= best_score - tolerance
        ]
        return min(
            near_best,
            key=lambda item: (
                item.complexity_rank,
                -float(item.selection_score),
                item.algorithm,
            ),
        )
