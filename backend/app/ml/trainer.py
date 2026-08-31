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
    positive_raw_scores,
)
from .calibration import fit_sigmoid_calibrator
from .decision_policy import (
    AGE_POLICY_CANDIDATES,
    DEFAULT_AGE_POLICY_VERSION,
    DECISION_POLICY_VERSION,
)
from .sparse_budget import select_sparse_budget
from .splits import TemporalSplitConfig, TemporalSplitter
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
        "tabpfn",
        "flaml_automl",
    )
    # None means adaptive search over every feasible feature count. A tuple is
    # retained as an explicit test/operator override, not production policy.
    feature_subset_sizes: tuple[int, ...] | None = None
    min_features_to_select: int = 4
    max_relative_occam_score_drop: float = 0.08
    interaction_rank_estimators: int = 160
    interaction_rank_max_depth: int = 3
    # Route-validated execution PnL remains shadow-only and never changes the
    # precision/recall/payoff expected-return objective J.
    execution_min_observations: int = 100
    execution_full_observations: int = 500
    execution_max_weight: float = 0.0  # shadow-only; never changes J ranking
    top_k: int = 3


class ModelTrainer:
    """Chronological Top-K selection on one expected-return objective.

    Model fitting is ordinary equal-weight binary classification. Development OOS
    probabilities choose an operating point that maximizes J=(3TP-FP)/N+, which
    is exactly Recall*(4-1/Precision) under the current +3/-1 payoff. AP, temporal
    stability, decay, execution quality and family diversity are audit-only. The
    final recent holdout is certification only and never ranks or tunes models.
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

            subset_sizes = self._feature_subset_sizes(len(dataset.feature_names), spec)
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
                -float(item.development_metrics.recall if item.development_metrics else 0.0),
                -float(item.development_metrics.precision if item.development_metrics else 0.0),
                len(item.feature_names),
                item.complexity_rank,
                item.algorithm,
            ),
        )
        top = ranked[: self.config.top_k]
        now = datetime.now(timezone.utc)
        bundles: list[ModelBundle] = []
        evaluation_bundles: list[ModelBundle] = []

        for rank, selected in enumerate(top, start=1):
            features = selected.feature_names
            # The exact fitted instance evaluated on the frozen final holdout is
            # the only instance eligible for deployment. Never refit with final
            # labels after certification; doing so would invalidate the certificate.
            production_estimator = final_estimators[selected.algorithm]
            certified_fit_indices = plan.final_split.train_indices
            production_economics = dataset.economic_slice(certified_fit_indices)
            model_step = production_estimator.named_steps.get("model")
            runtime_metadata: dict[str, object] = {}
            if model_step is not None and hasattr(model_step, "actual_model_version_"):
                runtime_metadata = {
                    "tabpfn_model_version": getattr(model_step, "actual_model_version_", None),
                    "tabpfn_device": getattr(model_step, "device_", None),
                    "tabpfn_n_estimators": getattr(model_step, "n_estimators_", None),
                }
            if model_step is not None and hasattr(model_step, "flaml_best_estimator_"):
                runtime_metadata.update(
                    {
                        "flaml_best_estimator": getattr(model_step, "flaml_best_estimator_", None),
                        "flaml_best_config": getattr(model_step, "flaml_best_config_", None),
                        "flaml_time_budget_seconds": getattr(
                            model_step, "flaml_time_budget_seconds_", None
                        ),
                        "flaml_split_type": getattr(model_step, "flaml_split_type_", None),
                        "flaml_eval_method": getattr(model_step, "flaml_eval_method_", None),
                        "flaml_validation_fraction": getattr(
                            model_step, "flaml_validation_fraction_", None
                        ),
                        "flaml_estimator_list": list(
                            getattr(model_step, "flaml_estimator_list_", ())
                        ),
                    }
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
                training_end=dataset.timestamps.iloc[certified_fit_indices[-1]].to_pydatetime(),
                calibrator=selected.calibrator,
                sparse_budget=selected.sparse_budget,
                decision_policy_version=DECISION_POLICY_VERSION,
                age_policy_version=selected.age_policy_version,
            )
            bundles.append(
                ModelBundle(
                    estimator=production_estimator,
                    metrics={
                        "rank": rank,
                        "stage": plan.stage_label,
                        "development": asdict(selected.development_metrics),
                        "final_recent_window": asdict(selected.final_metrics),
                        "generalization": asdict(selected.generalization) if selected.generalization else {},
                        "economic_score": selected.economic_score,
                        "composite_score": selected.composite_score,
                        "score_standard_error": selected.score_standard_error,
                        "utility_eligible": production_economics.utility_eligible,
                        "utility_blockers": production_economics.blockers,
                        "model_runtime": runtime_metadata,
                        "calibration": selected.calibrator.provenance() if selected.calibrator else None,
                        "sparse_budget": selected.sparse_budget.provenance() if selected.sparse_budget else None,
                        "age_policy_version": selected.age_policy_version,
                        "age_policy_selection": dict(selected.age_policy_metrics),
                        "deployment_fit_scope": "final_train_only_certified_instance",
                        "decision_policy_version": DECISION_POLICY_VERSION,
                        "evaluation_only": False,
                    },
                    **common,
                )
            )
            evaluation_bundles.append(
                ModelBundle(
                    model_id=f"{model_id}-evaluation",
                    estimator=production_estimator,
                    metrics={
                        "rank": rank,
                        "stage": plan.stage_label,
                        "final_recent_window": asdict(selected.final_metrics),
                        "generalization": asdict(selected.generalization) if selected.generalization else {},
                        "composite_score": selected.composite_score,
                        "model_runtime": runtime_metadata,
                        "calibration": selected.calibrator.provenance() if selected.calibrator else None,
                        "sparse_budget": selected.sparse_budget.provenance() if selected.sparse_budget else None,
                        "age_policy_version": selected.age_policy_version,
                        "age_policy_selection": dict(selected.age_policy_metrics),
                        "deployment_fit_scope": "final_train_only_certified_instance",
                        "decision_policy_version": DECISION_POLICY_VERSION,
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
        warnings.append("Top-3 selection ranks only development-OOS expected-return score J")
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
        requested_features = tuple(dataset.feature_names)
        evaluation = self._evaluate_development_candidate(
            dataset, plan, spec, len(requested_features)
        )
        evaluation, estimator = self._attach_final_holdout(dataset, plan, spec, evaluation)
        features = evaluation.feature_names
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
                "age_policy_version": evaluation.age_policy_version,
                "age_policy_selection": dict(evaluation.age_policy_metrics),
                "deployment_fit_scope": "final_train_only_certified_instance",
                "evaluation_only": True,
            },
            calibrator=evaluation.calibrator,
            sparse_budget=evaluation.sparse_budget,
            decision_policy_version=DECISION_POLICY_VERSION,
            age_policy_version=evaluation.age_policy_version,
        )
        return bundle, plan, evaluation

    def _select_diverse_top_k(
        self,
        ranked: list[CandidateEvaluation],
        specs: dict[str, CandidateSpec],
    ) -> list[CandidateEvaluation]:
        """Compatibility helper: Phase19 never sacrifices J for family diversity."""
        return list(ranked[: self.config.top_k])

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
            if item.calibrator is None:
                raise ValueError(f"{item.algorithm} is missing development calibrator")
            raw_scores = positive_raw_scores(estimator, dataset.X.loc[rows, features])
            probs = item.calibrator.transform(raw_scores)
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
            "selection_policy": "pure_expected_return_top_k",
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

    def _feature_subset_sizes(
        self,
        total: int,
        spec: CandidateSpec | None = None,
    ) -> tuple[int, ...]:
        if total <= 0:
            return ()
        if self.config.feature_subset_sizes:
            sizes = {
                min(total, max(1, int(size)))
                for size in self.config.feature_subset_sizes
            }
            sizes.add(total)
            return tuple(sorted(sizes))
        if spec is not None and spec.feature_subset_sizes:
            minimum = min(total, max(1, int(self.config.min_features_to_select)))
            sizes = {
                min(total, max(minimum, int(size)))
                for size in spec.feature_subset_sizes
            }
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
        oos_scores: list[np.ndarray] = []
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
            oos_scores.append(
                positive_raw_scores(estimator, dataset.X.loc[fold.test_indices, fold_features])
            )

        positions = np.concatenate(oos_positions)
        scores = np.concatenate(oos_scores)
        order = np.argsort(positions, kind="stable")
        positions = positions[order]
        scores = scores[order]
        labels = dataset.y.iloc[positions].to_numpy(dtype=int)
        calibrator = fit_sigmoid_calibrator(
            scores,
            labels,
            source_indices=positions.tolist(),
            source_start=dataset.timestamps.iloc[positions[0]].isoformat(),
            source_end=dataset.timestamps.iloc[positions[-1]].isoformat(),
        )
        probabilities = calibrator.transform(scores)
        sparse_budget, development_metrics = select_sparse_budget(
            labels,
            probabilities,
            dataset.economic_slice(positions),
            min_trades=self.config.min_trades,
        )
        threshold = float(sparse_budget.policy_base_threshold)
        age_policy_version, age_policy_metrics = self._select_development_age_policy(
            dataset,
            positions,
            probabilities,
            threshold,
        )

        # Transform each chronological OOS fold with the calibrator learned only
        # from development OOS predictions. The final holdout is never an input
        # to either sigmoid fitting or operating-point tuning.
        fold_probabilities = [calibrator.transform(values) for values in oos_scores]
        fold_metrics: list[EvaluationMetrics] = []
        average_precisions: list[float] = []
        ap_skills: list[float] = []
        fold_composites: list[float] = []
        for fold_positions, fold_probs in zip(oos_positions, fold_probabilities, strict=True):
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
            fold_composites.append(fold_economic_score(metric))

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
        economic_score = float(fold_economic_score(development_metrics))
        execution_score, execution_observations, execution_selected, execution_net_pnl = (
            self._execution_score(dataset, positions, probabilities, threshold)
        )
        execution_weight = 0.0
        ranking_economic_score = float(economic_score)
        composite = float(ranking_economic_score)
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
        final_feature_order = self._rank_features(dataset, plan.final_split.train_indices)
        feature_names = tuple(final_feature_order[:feature_count])
        return CandidateEvaluation(
            algorithm=spec.name,
            complexity_rank=spec.complexity_rank,
            status="ok",
            threshold=threshold,
            feature_names=feature_names,
            development_metrics=development_metrics,
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
            calibrator=calibrator,
            sparse_budget=sparse_budget,
            age_policy_version=age_policy_version,
            age_policy_metrics=age_policy_metrics,
        )

    def _select_development_age_policy(
        self,
        dataset: PreparedDataset,
        positions: np.ndarray,
        probabilities: np.ndarray,
        base_threshold: float,
    ) -> tuple[str, dict[str, object]]:
        """Record the admission-only contract against development OOS predictions.

        Phase19 has no model-output age adjustment and no age-band abstain. All
        rows in the modeling dataset have already passed the Collector admission
        contract, so this audit reports the model's own frozen threshold rather
        than re-reading an optional age column and producing a false zero signal.
        """
        labels = dataset.y.iloc[positions].to_numpy(dtype=int)
        mask = np.asarray(probabilities, dtype=float) >= float(base_threshold)
        selected_count = int(mask.sum())
        true_positives = int(labels[mask].sum()) if selected_count else 0
        false_positives = selected_count - true_positives
        precision = true_positives / selected_count if selected_count else None
        evidence = {
            "selected_count": selected_count,
            "true_positives": true_positives,
            "false_positives": false_positives,
            "precision": precision,
            "profit_units": float(3 * true_positives - false_positives),
        }
        candidates: dict[str, dict[str, object]] = {
            policy_version: dict(evidence) for policy_version in AGE_POLICY_CANDIDATES
        }

        return DEFAULT_AGE_POLICY_VERSION, {
            "selection_source": "fixed_admission_contract",
            "selected": DEFAULT_AGE_POLICY_VERSION,
            "candidates": candidates,
        }

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
        if evaluation.calibrator is None:
            raise ValueError("development sigmoid calibrator is missing")
        raw_scores = positive_raw_scores(estimator, dataset.X.loc[final_test, features])
        probabilities = evaluation.calibrator.transform(raw_scores)
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
