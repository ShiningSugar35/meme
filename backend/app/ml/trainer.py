from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import math
import uuid

import numpy as np
from sklearn.feature_selection import mutual_info_classif
from sklearn.metrics import average_precision_score

from .economics import (
    economic_sample_weights,
    evaluate_probabilities,
    evaluate_rule_baseline,
    fold_economic_score,
)
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
    GeneralizationMetrics,
    ModelBundle,
    PreparedDataset,
    TrainingResult,
)


@dataclass(frozen=True)
class TrainerConfig:
    random_state: int = 42
    min_precision: float = 0.0  # retained for API compatibility; not a ranking gate
    min_trades: int = 5
    candidate_names: tuple[str, ...] = (
        "logistic_regression",
        "decision_tree",
        "hist_gradient_boosting",
        "gradient_boosting",
        "ada_boost",
        "extra_trees",
        "random_forest",
        "rbf_svm",
        "xgboost",
        "lightgbm",
        "catboost",
        "flaml_automl",
    )
    # None means adaptive search over every feasible feature count. A tuple is
    # retained as an explicit test/operator override, not production policy.
    feature_subset_sizes: tuple[int, ...] | None = None
    min_features_to_select: int = 4
    max_relative_occam_score_drop: float = 0.08
    max_diversity_score_drop: float = 0.08
    interaction_rank_estimators: int = 160
    interaction_rank_max_depth: int = 3
    economic_weight: float = 0.60
    generalization_weight: float = 0.40
    # E remains the current fixed-payoff proxy. E_exec is collected and reported
    # as a shadow metric only; it never changes the established E/G/S ranking.
    execution_min_observations: int = 100
    execution_full_observations: int = 500
    execution_max_weight: float = 0.0  # shadow-only; E_exec never changes current E/G/S ranking
    top_k: int = 3


class ModelTrainer:
    """Chronological Top-K model selection with economic and decay-aware scoring.

    Model fitting is ordinary equal-weight binary classification. Trading payoff
    is applied only to out-of-sample predictions. Feature count is chosen inside
    the development folds by a one-standard-error Occam rule. The final recent
    holdout is certification only and never participates in ranking or tuning.
    """

    def __init__(
        self,
        config: TrainerConfig | None = None,
        split_config: TemporalSplitConfig | None = None,
    ) -> None:
        self.config = config or TrainerConfig()
        self.splitter = TemporalSplitter(split_config)
        self._feature_rank_cache: dict[bytes, list[str]] = {}

    def train(self, dataset: PreparedDataset) -> TrainingResult:
        self._feature_rank_cache.clear()
        plan = self.splitter.build(dataset)
        specs = {spec.name: spec for spec in candidate_catalog(self.config.random_state)}
        unknown = sorted(set(self.config.candidate_names) - set(specs))
        if unknown:
            raise ValueError(f"unknown model candidates: {unknown}")

        subset_sizes = self._feature_subset_sizes(len(dataset.feature_names))
        evaluations: list[CandidateEvaluation] = []
        final_estimators: dict[str, object] = {}
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

            subset_evaluations: list[CandidateEvaluation] = []
            for size in subset_sizes:
                try:
                    subset_evaluations.append(
                        self._evaluate_development_candidate(dataset, plan, spec, size)
                    )
                except Exception:
                    # A smaller subset may be degenerate while a larger one is valid.
                    continue
            if not subset_evaluations:
                evaluations.append(
                    CandidateEvaluation(
                        algorithm=name,
                        complexity_rank=spec.complexity_rank,
                        status="failed",
                        skip_reason="candidate failed on every chronological feature subset",
                    )
                )
                continue

            chosen = self._occam_feature_choice(subset_evaluations)
            selected_count = len(chosen.feature_names)
            final_feature_order = self._rank_features(dataset, plan.final_split.train_indices)
            chosen = replace(chosen, feature_names=tuple(final_feature_order[:selected_count]))
            try:
                chosen, estimator = self._attach_final_holdout(dataset, plan, spec, chosen)
            except Exception as exc:
                evaluations.append(
                    replace(
                        chosen,
                        status="failed",
                        skip_reason=f"final certification failed: {type(exc).__name__}: {exc}",
                    )
                )
                continue
            evaluations.append(chosen)
            final_estimators[name] = estimator
            successful_specs[name] = spec

        eligible = [
            item
            for item in evaluations
            if item.status == "ok"
            and item.composite_score is not None
            and item.threshold is not None
            and item.development_metrics is not None
            and item.final_metrics is not None
            and item.development_metrics.trade_count >= self.config.min_trades
        ]
        if len(eligible) < self.config.top_k:
            failures = "; ".join(
                f"{item.algorithm}: {item.skip_reason or 'not eligible'}" for item in evaluations
            )
            raise RuntimeError(
                f"only {len(eligible)} candidate models survived; need {self.config.top_k}; {failures}"
            )

        ranked = sorted(
            eligible,
            key=lambda item: (
                -float(item.composite_score),
                len(item.feature_names),
                item.complexity_rank,
                item.algorithm,
            ),
        )
        top = self._select_diverse_top_k(ranked, successful_specs)
        now = datetime.now(timezone.utc)
        bundles: list[ModelBundle] = []
        evaluation_bundles: list[ModelBundle] = []

        for rank, selected in enumerate(top, start=1):
            spec = successful_specs[selected.algorithm]
            features = selected.feature_names
            refit = build_pipeline(spec, dataset.X.loc[plan.refit_indices, features])
            refit_economics = dataset.economic_slice(plan.refit_indices)
            fit_pipeline(
                refit,
                dataset.X.loc[plan.refit_indices, features],
                dataset.y.iloc[plan.refit_indices],
                economic_sample_weights(refit_economics),
            )
            model_id = f"{now:%Y%m%dT%H%M%SZ}-{selected.algorithm}-{uuid.uuid4().hex[:8]}"
            thresholds = self._threshold_set(selected)
            common = dict(
                model_id=model_id,
                algorithm=selected.algorithm,
                feature_names=features,
                thresholds=thresholds,
                created_at=now,
                early_stage=plan.early_stage,
                training_start=dataset.timestamps.iloc[plan.active_indices[0]].to_pydatetime(),
                training_end=dataset.timestamps.iloc[plan.active_indices[-1]].to_pydatetime(),
            )
            bundles.append(
                ModelBundle(
                    estimator=refit,
                    metrics={
                        "rank": rank,
                        "stage": plan.stage_label,
                        "development": asdict(selected.development_metrics),
                        "final_recent_window": asdict(selected.final_metrics),
                        "generalization": asdict(selected.generalization) if selected.generalization else {},
                        "economic_score": selected.economic_score,
                        "composite_score": selected.composite_score,
                        "score_standard_error": selected.score_standard_error,
                        "utility_eligible": refit_economics.utility_eligible,
                        "utility_blockers": refit_economics.blockers,
                        "evaluation_only": False,
                    },
                    **common,
                )
            )
            evaluation_bundles.append(
                ModelBundle(
                    model_id=f"{model_id}-evaluation",
                    estimator=final_estimators[selected.algorithm],
                    metrics={
                        "rank": rank,
                        "stage": plan.stage_label,
                        "final_recent_window": asdict(selected.final_metrics),
                        "generalization": asdict(selected.generalization) if selected.generalization else {},
                        "composite_score": selected.composite_score,
                        "evaluation_only": True,
                        "trained_through": dataset.timestamps.iloc[
                            plan.final_split.train_indices[-1]
                        ].isoformat(),
                        "comparison_start": dataset.timestamps.iloc[
                            plan.final_split.test_indices[0]
                        ].isoformat(),
                    },
                    **{
                        **{key: value for key, value in common.items() if key not in {"model_id", "training_end"}},
                        "training_end": dataset.timestamps.iloc[
                            plan.final_split.train_indices[-1]
                        ].to_pydatetime(),
                    },
                )
            )

        final_rows = plan.final_split.test_indices
        diversity_metrics = self._audit_top_diversity(
            top,
            successful_specs,
            final_estimators,
            dataset,
            plan,
        )
        rule_baseline = evaluate_rule_baseline(
            dataset.y.iloc[final_rows].to_numpy(dtype=int),
            dataset.economic_slice(final_rows),
        )
        warnings: list[str] = []
        skipped = [item.algorithm for item in evaluations if item.status == "skipped"]
        if skipped:
            warnings.append(f"optional candidates skipped: {', '.join(skipped)}")
        if plan.early_stage:
            warnings.append("models are marked EARLY_STAGE_MODEL because history is under 120 days")
        warnings.append(
            "Top-3 selection prefers distinct model families inside the configured relative score budget"
        )
        warnings.append("final recent holdout is certification-only and is not used for Top-3 ranking")

        return TrainingResult(
            bundles=tuple(bundles),
            evaluation_bundles=tuple(evaluation_bundles),
            plan=plan,
            candidates=tuple(evaluations),
            top_algorithms=tuple(item.algorithm for item in top),
            rule_baseline=rule_baseline,
            diversity_metrics=diversity_metrics,
            warnings=tuple(warnings),
        )

    def rebuild_evaluation_bundle(
        self,
        dataset: PreparedDataset,
        algorithm: str,
    ) -> tuple[ModelBundle, object, CandidateEvaluation]:
        """Compatibility path for older promotion/audit callers."""
        plan = self.splitter.build(dataset)
        specs = {spec.name: spec for spec in candidate_catalog(self.config.random_state)}
        spec = specs.get(algorithm)
        if spec is None:
            raise ValueError(f"unknown model candidate: {algorithm}")
        if not spec.available:
            raise RuntimeError(spec.skip_reason or f"candidate {algorithm} is unavailable")
        features = tuple(dataset.feature_names)
        evaluation = self._evaluate_development_candidate(dataset, plan, spec, features)
        evaluation, estimator = self._attach_final_holdout(dataset, plan, spec, evaluation)
        now = datetime.now(timezone.utc)
        bundle = ModelBundle(
            model_id=f"rebuild-{algorithm}-{uuid.uuid4().hex[:8]}",
            algorithm=algorithm,
            estimator=estimator,
            feature_names=features,
            thresholds=self._threshold_set(evaluation),
            created_at=now,
            early_stage=plan.early_stage,
            training_start=dataset.timestamps.iloc[plan.active_indices[0]].to_pydatetime(),
            training_end=dataset.timestamps.iloc[plan.final_split.train_indices[-1]].to_pydatetime(),
            metrics={
                "stage": plan.stage_label,
                "final_recent_window": asdict(evaluation.final_metrics) if evaluation.final_metrics else {},
                "evaluation_only": True,
            },
        )
        return bundle, plan, evaluation

    def _select_diverse_top_k(
        self,
        ranked: list[CandidateEvaluation],
        specs: dict[str, CandidateSpec],
    ) -> list[CandidateEvaluation]:
        """Prefer distinct model families without sacrificing more than the score budget.

        The three deployed slots are independent strategies, not one averaged
        ensemble. Keeping three near-identical tree ensembles adds little model
        risk diversification, so within the configured near-best score band we
        prefer the strongest candidate from a family not yet represented. If the
        band cannot supply enough families, selection falls back to pure score.
        """

        if not ranked:
            return []
        best_score = float(ranked[0].composite_score or 0.0)
        relative_drop = float(np.clip(self.config.max_diversity_score_drop, 0.0, 1.0))
        score_floor = best_score - abs(best_score) * relative_drop
        remaining = list(ranked)
        selected: list[CandidateEvaluation] = []
        families: set[str] = set()

        while remaining and len(selected) < self.config.top_k:
            within_budget = [
                item
                for item in remaining
                if float(item.composite_score or -math.inf) >= score_floor
            ]
            candidate_pool = within_budget or remaining
            unseen_family = [
                item
                for item in candidate_pool
                if specs[item.algorithm].family not in families
            ]
            choice = (unseen_family or candidate_pool)[0]
            selected.append(choice)
            families.add(specs[choice.algorithm].family)
            remaining.remove(choice)

        return selected

    def _audit_top_diversity(
        self,
        top: list[CandidateEvaluation],
        specs: dict[str, CandidateSpec],
        final_estimators: dict[str, object],
        dataset: PreparedDataset,
        plan,
    ) -> dict[str, object]:
        """Measure Top-3 redundancy on the certification-only recent holdout."""

        rows = plan.final_split.test_indices
        probabilities: dict[str, np.ndarray] = {}
        selections: dict[str, np.ndarray] = {}
        families: dict[str, str] = {}
        for item in top:
            estimator = final_estimators[item.algorithm]
            features = item.feature_names
            probs = positive_probabilities(estimator, dataset.X.loc[rows, features])
            probabilities[item.algorithm] = probs
            selections[item.algorithm] = probs >= float(item.threshold)
            families[item.algorithm] = specs[item.algorithm].family

        pairs: list[dict[str, object]] = []
        for left_index, left in enumerate(top):
            for right in top[left_index + 1 :]:
                left_probs = probabilities[left.algorithm]
                right_probs = probabilities[right.algorithm]
                left_std = float(np.std(left_probs))
                right_std = float(np.std(right_probs))
                if left_std <= 1e-12 or right_std <= 1e-12:
                    pearson = 1.0 if np.allclose(left_probs, right_probs) else 0.0
                else:
                    pearson = float(np.corrcoef(left_probs, right_probs)[0, 1])
                left_selected = selections[left.algorithm]
                right_selected = selections[right.algorithm]
                agreement = float(np.mean(left_selected == right_selected))
                union = int(np.sum(left_selected | right_selected))
                jaccard = (
                    float(np.sum(left_selected & right_selected) / union)
                    if union
                    else 1.0
                )
                pairs.append(
                    {
                        "left": left.algorithm,
                        "right": right.algorithm,
                        "pearson_probability": pearson,
                        "decision_agreement": agreement,
                        "selected_jaccard": jaccard,
                    }
                )

        return {
            "selection_policy": "distinct_model_family_within_relative_score_budget",
            "max_relative_score_drop": float(self.config.max_diversity_score_drop),
            "holdout_role": "certification_only_not_used_for_selection",
            "families": families,
            "pairs": pairs,
            "max_abs_probability_correlation": max(
                (abs(float(item["pearson_probability"])) for item in pairs),
                default=0.0,
            ),
            "max_selected_jaccard": max(
                (float(item["selected_jaccard"]) for item in pairs),
                default=0.0,
            ),
        }

    def _rank_features(self, dataset: PreparedDataset, train_indices: np.ndarray) -> list[str]:
        """Rank features using train-only tree interaction contributions.

        The previous univariate mutual-information ranker could discard features
        whose signal appears mainly through interactions. A shallow XGBoost
        screening model sees the full candidate pool on the chronological train
        slice only; TreeSHAP interaction values then attribute both main and
        pairwise/low-order interaction contribution back to each base feature.
        Mutual information remains a deterministic fallback if screening fails.
        """

        cache_key = train_indices.tobytes()
        cached = self._feature_rank_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        frame = dataset.X.iloc[train_indices].copy()
        array = frame.to_numpy(dtype=float)
        medians = np.nanmedian(array, axis=0)
        medians = np.where(np.isfinite(medians), medians, 0.0)
        missing = ~np.isfinite(array)
        if missing.any():
            array[missing] = medians[np.where(missing)[1]]
        frame.loc[:, :] = array
        y = dataset.y.iloc[train_indices].to_numpy(dtype=int)
        original = {name: index for index, name in enumerate(dataset.feature_names)}

        try:
            from xgboost import DMatrix, XGBClassifier

            screen = XGBClassifier(
                n_estimators=max(40, int(self.config.interaction_rank_estimators)),
                max_depth=max(2, int(self.config.interaction_rank_max_depth)),
                learning_rate=0.04,
                min_child_weight=12,
                subsample=0.90,
                colsample_bytree=1.0,
                reg_lambda=6.0,
                reg_alpha=0.15,
                objective="binary:logistic",
                eval_metric="logloss",
                random_state=self.config.random_state,
                n_jobs=1,
            )
            screen.fit(
                frame,
                y,
                sample_weight=economic_sample_weights(dataset.economic_slice(train_indices)),
            )
            interactions = screen.get_booster().predict(
                DMatrix(frame),
                pred_interactions=True,
                strict_shape=True,
            )
            interactions = np.asarray(interactions, dtype=float)
            if interactions.ndim == 4:
                interactions = interactions[:, 0, :, :]
            feature_count = len(dataset.feature_names)
            interactions = interactions[:, :feature_count, :feature_count]
            strengths = np.mean(np.sum(np.abs(interactions), axis=2), axis=0)
            if len(strengths) != feature_count or not np.isfinite(strengths).any():
                raise ValueError("interaction ranker returned invalid strengths")
            order = sorted(
                dataset.feature_names,
                key=lambda name: (-float(strengths[original[name]]), original[name]),
            )
        except Exception:
            scores = mutual_info_classif(
                array,
                y,
                discrete_features=False,
                random_state=self.config.random_state,
            )
            order = sorted(
                dataset.feature_names,
                key=lambda name: (-float(scores[original[name]]), original[name]),
            )

        self._feature_rank_cache[cache_key] = list(order)
        return list(order)

    def _feature_subset_sizes(self, total: int) -> tuple[int, ...]:
        if total <= 0:
            return ()
        if self.config.feature_subset_sizes:
            sizes = {min(total, max(1, int(size))) for size in self.config.feature_subset_sizes}
            sizes.add(total)
            return tuple(sorted(sizes))
        minimum = min(total, max(1, int(self.config.min_features_to_select)))
        return tuple(range(minimum, total + 1))

    def _evaluate_development_candidate(
        self,
        dataset: PreparedDataset,
        plan,
        spec: CandidateSpec,
        feature_count: int,
    ) -> CandidateEvaluation:
        oos_positions: list[np.ndarray] = []
        oos_probabilities: list[np.ndarray] = []
        for fold in plan.development_folds:
            y_train = dataset.y.iloc[fold.train_indices]
            if y_train.nunique() < 2:
                raise ValueError(f"{fold.name} training slice has only one class")
            # Feature selection is part of model fitting, so rank features only
            # on this fold's chronological training slice. The OOS block never
            # participates in feature selection.
            fold_order = self._rank_features(dataset, fold.train_indices)
            fold_features = tuple(fold_order[:feature_count])
            estimator = build_pipeline(spec, dataset.X.loc[fold.train_indices, fold_features])
            fit_pipeline(
                estimator,
                dataset.X.loc[fold.train_indices, fold_features],
                y_train,
                economic_sample_weights(dataset.economic_slice(fold.train_indices)),
            )
            oos_positions.append(fold.test_indices)
            oos_probabilities.append(
                positive_probabilities(estimator, dataset.X.loc[fold.test_indices, fold_features])
            )

        positions = np.concatenate(oos_positions)
        probabilities = np.concatenate(oos_probabilities)
        order = np.argsort(positions, kind="stable")
        positions = positions[order]
        probabilities = probabilities[order]
        threshold_result = optimize_thresholds(
            dataset.y.iloc[positions].to_numpy(dtype=int),
            probabilities,
            dataset.economic_slice(positions),
            ThresholdSearchConfig(min_trades=self.config.min_trades),
        )
        threshold = float(threshold_result.thresholds.decision)

        fold_metrics: list[EvaluationMetrics] = []
        average_precisions: list[float] = []
        ap_skills: list[float] = []
        fold_composites: list[float] = []
        for fold_positions, fold_probs in zip(oos_positions, oos_probabilities, strict=True):
            y_fold = dataset.y.iloc[fold_positions].to_numpy(dtype=int)
            metric = evaluate_probabilities(
                y_fold,
                fold_probs,
                threshold,
                dataset.economic_slice(fold_positions),
            )
            fold_metrics.append(metric)
            prevalence = float(np.mean(y_fold))
            ap = float(average_precision_score(y_fold, fold_probs)) if y_fold.sum() else 0.0
            denominator = max(1.0 - prevalence, 1e-9)
            skill = float(np.clip((ap - prevalence) / denominator, 0.0, 1.0))
            average_precisions.append(ap)
            ap_skills.append(skill)
            fold_composites.append(
                self.config.economic_weight * fold_economic_score(metric)
                + self.config.generalization_weight * skill
            )

        skill_array = np.asarray(ap_skills, dtype=float)
        mean_skill = float(np.mean(skill_array))
        std_skill = float(np.std(skill_array, ddof=0))
        stability = float(np.clip(1.0 - std_skill / 0.25, 0.0, 1.0))
        if len(skill_array) >= 2:
            x = np.linspace(0.0, 1.0, len(skill_array))
            slope = float(np.polyfit(x, skill_array, 1)[0])
        else:
            slope = 0.0
        decay_score = float(np.clip(1.0 + min(0.0, slope), 0.0, 1.0))
        generalization_score = float(
            0.60 * mean_skill + 0.20 * stability + 0.20 * decay_score
        )
        economic_score = float(np.mean([fold_economic_score(metric) for metric in fold_metrics]))
        execution_score, execution_observations, execution_selected, execution_net_pnl = (
            self._execution_score(dataset, positions, probabilities, threshold)
        )
        execution_weight = 0.0  # E_exec is reported only; ranking remains the established E proxy.
        ranking_economic_score = float(
            (1.0 - execution_weight) * economic_score
            + execution_weight * (execution_score if execution_score is not None else economic_score)
        )
        composite = float(
            self.config.economic_weight * ranking_economic_score
            + self.config.generalization_weight * generalization_score
        )
        composites = np.asarray(fold_composites, dtype=float)
        standard_error = (
            float(np.std(composites, ddof=1) / math.sqrt(len(composites)))
            if len(composites) > 1
            else 0.0
        )
        generalization = GeneralizationMetrics(
            average_precision_mean=float(np.mean(average_precisions)),
            average_precision_std=float(np.std(average_precisions, ddof=0)),
            average_precision_skill_mean=mean_skill,
            stability_score=stability,
            decay_score=decay_score,
            score=generalization_score,
        )
        # Convert the selected feature-count hyperparameter into concrete names
        # using final-train data only. The recent holdout is untouched.
        final_feature_order = self._rank_features(dataset, plan.final_split.train_indices)
        feature_names = tuple(final_feature_order[:feature_count])
        return CandidateEvaluation(
            algorithm=spec.name,
            complexity_rank=spec.complexity_rank,
            status="ok",
            threshold=threshold,
            feature_names=feature_names,
            development_metrics=threshold_result.metrics,
            fold_metrics=tuple(fold_metrics),
            generalization=generalization,
            economic_score=economic_score,
            composite_score=composite,
            score_standard_error=standard_error,
            selection_score=composite,
            execution_score=execution_score,
            execution_observations=execution_observations,
            execution_selected=execution_selected,
            execution_net_pnl_usd=execution_net_pnl,
            execution_weight=execution_weight,
            ranking_economic_score=ranking_economic_score,
        )

    def _execution_score(
        self,
        dataset: PreparedDataset,
        positions: np.ndarray,
        probabilities: np.ndarray,
        threshold: float,
    ) -> tuple[float | None, int, int, float | None]:
        observed = dataset.execution_observed.iloc[positions].to_numpy(dtype=bool)
        if not observed.any():
            return None, 0, 0, None
        selected = probabilities >= threshold
        pnl = dataset.execution_net_pnl_usd.iloc[positions].to_numpy(dtype=float)
        observed_pnl = pnl[observed]
        denominator = float(np.sum(np.maximum(observed_pnl, 0.0)))
        selected_observed = observed & selected
        selected_pnl = float(np.sum(pnl[selected_observed])) if selected_observed.any() else 0.0
        score = (
            float(np.clip(selected_pnl / denominator, -1.0, 1.0))
            if denominator > 0
            else None
        )
        return score, int(np.sum(observed)), int(np.sum(selected_observed)), selected_pnl

    def _execution_weight(self, observations: int, score: float | None) -> float:
        if score is None or observations < self.config.execution_min_observations:
            return 0.0
        start = max(0, int(self.config.execution_min_observations))
        full = max(start + 1, int(self.config.execution_full_observations))
        progress = min(1.0, max(0.0, (observations - start) / (full - start)))
        return float(np.clip(progress * self.config.execution_max_weight, 0.0, 1.0))

    def _attach_final_holdout(
        self,
        dataset: PreparedDataset,
        plan,
        spec: CandidateSpec,
        evaluation: CandidateEvaluation,
    ) -> tuple[CandidateEvaluation, object]:
        features = evaluation.feature_names
        final_train = plan.final_split.train_indices
        final_test = plan.final_split.test_indices
        if dataset.y.iloc[final_train].nunique() < 2:
            raise ValueError("final pre-holdout training slice has only one class")
        estimator = build_pipeline(spec, dataset.X.loc[final_train, features])
        fit_pipeline(
            estimator,
            dataset.X.loc[final_train, features],
            dataset.y.iloc[final_train],
            economic_sample_weights(dataset.economic_slice(final_train)),
        )
        probabilities = positive_probabilities(estimator, dataset.X.loc[final_test, features])
        final_metrics = evaluate_probabilities(
            dataset.y.iloc[final_test].to_numpy(dtype=int),
            probabilities,
            float(evaluation.threshold),
            dataset.economic_slice(final_test),
        )
        return replace(evaluation, final_metrics=final_metrics), estimator

    def _occam_feature_choice(self, evaluations: list[CandidateEvaluation]) -> CandidateEvaluation:
        """Choose the smallest subset that is both statistically and practically near-best."""
        best = max(evaluations, key=lambda item: float(item.composite_score or -math.inf))
        best_score = float(best.composite_score or 0.0)
        one_se_cutoff = best_score - float(best.score_standard_error or 0.0)
        relative_drop = float(np.clip(self.config.max_relative_occam_score_drop, 0.0, 1.0))
        relative_cutoff = best_score - abs(best_score) * relative_drop
        cutoff = max(one_se_cutoff, relative_cutoff)
        near_best = [item for item in evaluations if float(item.composite_score or -math.inf) >= cutoff]
        return min(
            near_best,
            key=lambda item: (
                len(item.feature_names),
                -float(item.composite_score or -math.inf),
            ),
        )

    @staticmethod
    def _threshold_set(evaluation: CandidateEvaluation):
        from .types import ThresholdSet

        if evaluation.threshold is None:
            raise RuntimeError("model did not produce a decision threshold")
        return ThresholdSet(decision=float(evaluation.threshold))
