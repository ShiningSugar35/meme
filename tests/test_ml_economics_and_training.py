from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from backend.app.ml import (
    CandidateEvaluation,
    FeatureBuilder,
    FeaturePolicy,
    ModelBundle,
    ModelTrainer,
    PromotionEvaluator,
    TemporalSplitConfig,
    ThresholdSet,
    TrainerConfig,
)
from backend.app.ml.economics import (
    evaluate_probabilities,
    theoretical_profit_from_precision_recall,
    theoretical_profit_units,
)
from backend.app.ml.models import candidate_catalog


def _learnable_frame(rows: int = 360, *, include_liquidity: bool = True) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    start = pd.Timestamp("2026-01-01", tz="UTC")
    timestamps = start + pd.to_timedelta(np.arange(rows) * 4, unit="h")
    momentum = rng.normal(size=rows)
    quality = rng.normal(size=rows)
    latent = 1.6 * momentum + 0.5 * quality + rng.normal(scale=0.45, size=rows)
    tags = np.where(latent > 0.65, 1, 0)
    frame = pd.DataFrame(
        {
            "address": [f"token-{i}" for i in range(rows)],
            "name": "meme",
            "symbol": "MEME",
            "type": "new_creation",
            "time": [int(value.timestamp()) for value in timestamps],
            "price": 1.0,
            "price_2h_max/price": np.where(tags == 1, 1.7, 1.1),
            "price_2h_min/price": np.where(tags == 0, 0.85, 0.98),
            "final_2h_close_ratio": np.nan,
            "price_change_1h": momentum,
            "fresh_wallet_rate": 1 / (1 + np.exp(-quality)),
            "launchpad": np.where(np.arange(rows) % 3, "Pump.fun", "letsbonk"),
            "tag": tags,
        }
    )
    if include_liquidity:
        frame["liquidity"] = rng.uniform(4_800, 20_000, rows)
    return frame


def test_realized_return_and_fixed_payoff_formula_are_exact() -> None:
    frame = pd.DataFrame(
        {
            "time": [1_800_000_000, 1_800_000_100, 1_800_000_200],
            "feature": [0.0, 1.0, 2.0],
            "liquidity": [1_000.0, 4_000.0, 10_000.0],
            "tag": [0, 1, 0],
        }
    )
    prepared = FeatureBuilder().prepare(frame)
    economics = prepared.economic_slice(np.arange(3))
    metrics = evaluate_probabilities(
        prepared.y.to_numpy(), np.array([0.9, 0.9, 0.9]), 0.5, economics
    )
    assert economics.capital.tolist() == [10.0, 40.0, 50.0]
    assert economics.realized_return.tolist() == pytest.approx([-0.10, 0.60, -0.10])
    assert metrics.cumulative_pnl_usd == pytest.approx(-1 + 24 - 5)
    assert metrics.profit_units == pytest.approx(4.0)  # 6*TP - FP = 6 - 2
    assert metrics.fixed_profit_usd == pytest.approx(20.0)


def test_precision_recall_identity_matches_tp_fp_payoff() -> None:
    tp, fp, positives = 18, 12, 60
    precision = tp / (tp + fp)
    recall = tp / positives
    from_counts = theoretical_profit_units(tp, fp)
    from_pr = theoretical_profit_from_precision_recall(precision, recall, positives)
    assert from_counts == pytest.approx(from_pr)
    assert from_pr == pytest.approx(positives * recall * (7 - 1 / precision))


def test_candidate_catalog_is_expanded_and_optional_automl_is_explicit() -> None:
    catalog = {spec.name: spec for spec in candidate_catalog()}
    required = {
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
    }
    assert set(catalog) == required
    assert catalog["decision_tree"].available
    assert catalog["tabpfn"].family == "foundation"
    assert catalog["tabpfn"].feature_subset_sizes == (4, 8, 16, 24)
    assert catalog["flaml_automl"].skip_reason is not None




def test_default_feature_count_search_is_adaptive_and_explicit_override_is_preserved() -> None:
    adaptive = ModelTrainer(TrainerConfig())
    assert adaptive._feature_subset_sizes(8) == (4, 5, 6, 7, 8)

    overridden = ModelTrainer(TrainerConfig(feature_subset_sizes=(2, 5)))
    assert overridden._feature_subset_sizes(8) == (2, 5, 8)

    tabpfn = {spec.name: spec for spec in candidate_catalog()}["tabpfn"]
    assert adaptive._feature_subset_sizes(31, tabpfn) == (4, 8, 16, 24, 31)


def test_occam_rule_never_accepts_more_than_eight_percent_score_drop() -> None:
    trainer = ModelTrainer(TrainerConfig(max_relative_occam_score_drop=0.08))
    evaluations = [
        CandidateEvaluation(
            algorithm="random_forest",
            complexity_rank=4,
            status="ok",
            feature_names=("a", "b", "c", "d"),
            composite_score=0.350,
            score_standard_error=0.100,
        ),
        CandidateEvaluation(
            algorithm="random_forest",
            complexity_rank=4,
            status="ok",
            feature_names=("a", "b", "c", "d", "e", "f"),
            composite_score=0.372,
            score_standard_error=0.050,
        ),
        CandidateEvaluation(
            algorithm="random_forest",
            complexity_rank=4,
            status="ok",
            feature_names=tuple("abcdefghij"),
            composite_score=0.400,
            score_standard_error=0.100,
        ),
    ]

    chosen = trainer._occam_feature_choice(evaluations)

    # One-SE alone would accept 0.350, but the 8% floor is 0.368.
    assert chosen.composite_score == pytest.approx(0.372)
    assert len(chosen.feature_names) == 6


def test_top3_prefers_distinct_model_families_inside_eight_percent_budget() -> None:
    trainer = ModelTrainer(TrainerConfig(top_k=3, max_diversity_score_drop=0.08))
    specs = {spec.name: spec for spec in candidate_catalog(42)}
    ranked = [
        CandidateEvaluation(
            algorithm="ada_boost",
            complexity_rank=4,
            status="ok",
            feature_names=("a", "b", "c", "d"),
            composite_score=1.00,
        ),
        CandidateEvaluation(
            algorithm="hist_gradient_boosting",
            complexity_rank=3,
            status="ok",
            feature_names=("a", "b", "c", "d"),
            composite_score=0.99,
        ),
        CandidateEvaluation(
            algorithm="random_forest",
            complexity_rank=4,
            status="ok",
            feature_names=("a", "b", "c", "d"),
            composite_score=0.97,
        ),
        CandidateEvaluation(
            algorithm="logistic_regression",
            complexity_rank=1,
            status="ok",
            feature_names=("a", "b", "c", "d"),
            composite_score=0.94,
        ),
    ]

    selected = trainer._select_diverse_top_k(ranked, specs)

    assert [item.algorithm for item in selected] == [
        "ada_boost",
        "random_forest",
        "logistic_regression",
    ]


def test_interaction_aware_ranker_keeps_xor_features_ahead_of_noise() -> None:
    rng = np.random.default_rng(123)
    rows = 800
    x1 = rng.integers(0, 2, size=rows).astype(float)
    x2 = rng.integers(0, 2, size=rows).astype(float)
    tag = np.logical_xor(x1.astype(bool), x2.astype(bool)).astype(int)
    frame = pd.DataFrame(
        {
            "time": np.arange(1_800_000_000, 1_800_000_000 + rows),
            "x1": x1,
            "x2": x2,
            "noise": rng.normal(size=rows),
            "tag": tag,
        }
    )
    dataset = FeatureBuilder(
        FeaturePolicy(feature_allowlist=("x1", "x2", "noise"))
    ).prepare(frame)
    trainer = ModelTrainer(
        TrainerConfig(interaction_rank_estimators=100, interaction_rank_max_depth=3)
    )

    ranked = trainer._rank_features(dataset, np.arange(600))

    assert set(ranked[:2]) == {"x1", "x2"}
    assert ranked[-1] == "noise"


def test_execution_score_is_shadow_only_and_never_changes_ranking_weight() -> None:
    trainer = ModelTrainer(
        TrainerConfig(
            execution_min_observations=100,
            execution_full_observations=500,
            execution_max_weight=0.0,
        )
    )
    assert trainer._execution_weight(99, 0.8) == pytest.approx(0.0)
    assert trainer._execution_weight(100, 0.8) == pytest.approx(0.0)
    assert trainer._execution_weight(300, 0.8) == pytest.approx(0.0)
    assert trainer._execution_weight(500, 0.8) == pytest.approx(0.0)
    assert trainer._execution_weight(1000, 0.8) == pytest.approx(0.0)
    assert trainer._execution_weight(500, None) == pytest.approx(0.0)


def test_execution_score_uses_route_validated_net_pnl_but_remains_shadow_metric() -> None:
    frame = _learnable_frame(rows=8)
    frame["execution_observed"] = False
    frame["execution_invested_usd"] = np.nan
    frame["execution_net_pnl_usd"] = np.nan
    frame.loc[:2, "execution_observed"] = True
    frame.loc[:2, "execution_invested_usd"] = 50.0
    frame.loc[:2, "execution_net_pnl_usd"] = [10.0, -5.0, 20.0]
    dataset = FeatureBuilder().prepare(frame)

    trainer = ModelTrainer(TrainerConfig())
    score, observations, selected, pnl = trainer._execution_score(
        dataset,
        np.array([0, 1, 2]),
        np.array([0.9, 0.8, 0.1]),
        0.5,
    )

    # Selected actual PnL is +10-5=+5; the observed profitable-opportunity pool
    # is +10+20=+30. E_exec is 1/6 but has zero influence on E/G/S ranking.
    assert score == pytest.approx(1 / 6)
    assert observations == 3
    assert selected == 2
    assert pnl == pytest.approx(5.0)
    assert trainer.config.execution_max_weight == pytest.approx(0.0)


def test_trainer_returns_top3_one_threshold_each_and_keeps_final_holdout_separate() -> None:
    dataset = FeatureBuilder().prepare(_learnable_frame())
    trainer = ModelTrainer(
        TrainerConfig(
            candidate_names=(
                "decision_tree",
                "hist_gradient_boosting",
                "extra_trees",
                "random_forest",
            ),
            min_trades=2,
            top_k=3,
            feature_subset_sizes=(2, 3),
        ),
        TemporalSplitConfig(min_train_rows=50, min_test_rows=12, development_folds=3),
    )
    result = trainer.train(dataset)
    assert len(result.bundles) == 3
    assert len(result.top_algorithms) == 3
    assert all(set(bundle.thresholds.as_dict()) == {"decision"} for bundle in result.bundles)
    assert all(candidate.composite_score is not None for candidate in result.candidates if candidate.status == "ok")
    assert result.rule_baseline.recall == pytest.approx(1.0)
    assert result.diversity_metrics["holdout_role"] == "certification_only_not_used_for_selection"
    assert len(result.diversity_metrics["pairs"]) == 3
    assert 0.0 <= result.diversity_metrics["max_selected_jaccard"] <= 1.0
    assert "certification-only" in " ".join(result.warnings)
    train_end = dataset.timestamps.iloc[result.plan.final_split.train_indices].max()
    test_start = dataset.timestamps.iloc[result.plan.final_split.test_indices].min()
    assert train_end + pd.Timedelta(hours=1) <= test_start


class _ScoreEstimator:
    def __init__(self, multiplier: float = 1.0) -> None:
        self.multiplier = multiplier

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        p = np.clip(frame["score"].to_numpy(dtype=float) * self.multiplier, 0, 1)
        return np.column_stack([1 - p, p])


def _bundle(name: str, multiplier: float, threshold: float) -> ModelBundle:
    now = datetime.now(timezone.utc)
    return ModelBundle(
        model_id=name,
        algorithm=name,
        estimator=_ScoreEstimator(multiplier),
        feature_names=("score",),
        thresholds=ThresholdSet(decision=threshold),
        created_at=now,
        early_stage=True,
        training_start=now,
        training_end=now,
        metrics={"evaluation_only": True},
    )


def _promotion_frame(include_liquidity: bool) -> pd.DataFrame:
    score = np.array([0.95, 0.9, 0.85, 0.8, 0.2, 0.1, 0.05, 0.01])
    frame = pd.DataFrame(
        {"time": 1_800_000_000 + np.arange(len(score)) * 10_000, "score": score, "tag": [1, 1, 1, 1, 0, 0, 0, 0]}
    )
    if include_liquidity:
        frame["liquidity"] = 10_000.0
    return frame


def test_compatibility_promotion_uses_one_threshold_and_real_economics() -> None:
    prepared = FeatureBuilder().prepare(_promotion_frame(include_liquidity=True))
    rows = np.arange(len(prepared))
    decision = PromotionEvaluator().compare(
        _bundle("candidate", 1.0, 0.5),
        _bundle("incumbent", 0.45, 0.5),
        prepared,
        rows,
    )
    assert decision.eligible
    assert decision.promote
    assert decision.pnl_lift is not None and decision.pnl_lift >= 0.05


def test_legacy_proxy_can_score_but_cannot_auto_promote() -> None:
    prepared = FeatureBuilder().prepare(_promotion_frame(include_liquidity=False))
    rows = np.arange(len(prepared))
    decision = PromotionEvaluator().compare(
        _bundle("candidate", 1.0, 0.5),
        _bundle("incumbent", 0.45, 0.5),
        prepared,
        rows,
    )
    assert not decision.eligible
    assert decision.candidate_metrics.cumulative_pnl_usd is None
    assert decision.candidate_metrics.proxy_pnl is not None
    assert any("liquidity" in blocker for blocker in decision.blockers)
