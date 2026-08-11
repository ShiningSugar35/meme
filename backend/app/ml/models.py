from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from typing import Callable

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import (
    AdaBoostClassifier,
    ExtraTreesClassifier,
    GradientBoostingClassifier,
    HistGradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    complexity_rank: int
    available: bool
    skip_reason: str | None
    builder: Callable[[], object] | None
    scale_numeric: bool = False
    family: str = "other"


def _optional_missing(name: str, dependency: str, complexity_rank: int, family: str) -> CandidateSpec:
    return CandidateSpec(
        name=name,
        complexity_rank=complexity_rank,
        available=False,
        skip_reason=f"optional dependency '{dependency}' is not installed",
        builder=None,
        family=family,
    )


def _xgboost_spec(random_state: int) -> CandidateSpec:
    if importlib.util.find_spec("xgboost") is None:
        return _optional_missing("xgboost", "xgboost", 6, "boosting")

    def build() -> object:
        from xgboost import XGBClassifier

        return XGBClassifier(
            n_estimators=240,
            max_depth=2,
            learning_rate=0.03,
            min_child_weight=18,
            subsample=0.85,
            colsample_bytree=0.85,
            reg_lambda=6.0,
            reg_alpha=0.15,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=random_state,
            n_jobs=1,
        )

    return CandidateSpec(
        name="xgboost",
        complexity_rank=6,
        available=True,
        skip_reason=None,
        builder=build,
        family="boosting",
    )


def _lightgbm_spec(random_state: int) -> CandidateSpec:
    if importlib.util.find_spec("lightgbm") is None:
        return _optional_missing("lightgbm", "lightgbm", 6, "boosting")

    def build() -> object:
        from lightgbm import LGBMClassifier

        return LGBMClassifier(
            n_estimators=240,
            learning_rate=0.03,
            num_leaves=15,
            max_depth=5,
            min_child_samples=30,
            reg_lambda=6.0,
            reg_alpha=0.1,
            subsample=0.85,
            colsample_bytree=0.85,
            random_state=random_state,
            n_jobs=1,
            verbosity=-1,
        )

    return CandidateSpec(
        name="lightgbm",
        complexity_rank=6,
        available=True,
        skip_reason=None,
        builder=build,
        family="boosting",
    )


def _catboost_spec(random_state: int) -> CandidateSpec:
    if importlib.util.find_spec("catboost") is None:
        return _optional_missing("catboost", "catboost", 6, "boosting")

    def build() -> object:
        from catboost import CatBoostClassifier

        return CatBoostClassifier(
            iterations=240,
            depth=5,
            learning_rate=0.03,
            l2_leaf_reg=6.0,
            loss_function="Logloss",
            verbose=False,
            random_seed=random_state,
            thread_count=1,
        )

    return CandidateSpec(
        name="catboost",
        complexity_rank=6,
        available=True,
        skip_reason=None,
        builder=build,
        family="boosting",
    )


def _flaml_spec(random_state: int) -> CandidateSpec:
    """Expose FLAML in the candidate ledger without making it a hard dependency.

    The production environment currently keeps AutoML optional. When FLAML is
    installed, it should be integrated with an outer-fold-safe adapter rather
    than receiving the global final holdout. Until that adapter is enabled this
    candidate is deliberately skipped even if importable, avoiding accidental
    leakage through an internal random split.
    """
    if importlib.util.find_spec("flaml") is None:
        return _optional_missing("flaml_automl", "flaml", 8, "automl")
    return CandidateSpec(
        name="flaml_automl",
        complexity_rank=8,
        available=False,
        skip_reason=(
            "FLAML is installed but the nested time-split adapter is not enabled; "
            "candidate is skipped to protect the outer chronological holdout"
        ),
        builder=None,
        family="automl",
    )


def candidate_catalog(random_state: int = 42) -> tuple[CandidateSpec, ...]:
    """Diverse, auditable candidate pool for a small structured dataset."""
    return (
        CandidateSpec(
            name="logistic_regression",
            complexity_rank=1,
            available=True,
            skip_reason=None,
            builder=lambda: LogisticRegression(
                C=0.5,
                max_iter=1_500,
                solver="lbfgs",
                random_state=random_state,
            ),
            scale_numeric=True,
            family="linear",
        ),
        CandidateSpec(
            name="decision_tree",
            complexity_rank=2,
            available=True,
            skip_reason=None,
            builder=lambda: DecisionTreeClassifier(
                max_depth=5,
                min_samples_leaf=25,
                min_samples_split=50,
                ccp_alpha=0.001,
                random_state=random_state,
            ),
            family="tree",
        ),
        CandidateSpec(
            name="hist_gradient_boosting",
            complexity_rank=3,
            available=True,
            skip_reason=None,
            builder=lambda: HistGradientBoostingClassifier(
                max_depth=2,
                learning_rate=0.03,
                max_iter=180,
                min_samples_leaf=30,
                l2_regularization=5.0,
                early_stopping=False,
                random_state=random_state,
            ),
            family="boosting",
        ),
        CandidateSpec(
            name="gradient_boosting",
            complexity_rank=4,
            available=True,
            skip_reason=None,
            builder=lambda: GradientBoostingClassifier(
                n_estimators=180,
                learning_rate=0.03,
                max_depth=2,
                min_samples_leaf=25,
                subsample=0.85,
                random_state=random_state,
            ),
            family="boosting",
        ),
        CandidateSpec(
            name="ada_boost",
            complexity_rank=4,
            available=True,
            skip_reason=None,
            builder=lambda: AdaBoostClassifier(
                estimator=DecisionTreeClassifier(
                    max_depth=2,
                    min_samples_leaf=25,
                    random_state=random_state,
                ),
                n_estimators=160,
                learning_rate=0.03,
                random_state=random_state,
            ),
            family="boosting",
        ),
        CandidateSpec(
            name="extra_trees",
            complexity_rank=4,
            available=True,
            skip_reason=None,
            builder=lambda: ExtraTreesClassifier(
                n_estimators=320,
                max_depth=6,
                min_samples_leaf=15,
                max_features="sqrt",
                random_state=random_state,
                n_jobs=1,
            ),
            family="bagging",
        ),
        CandidateSpec(
            name="random_forest",
            complexity_rank=4,
            available=True,
            skip_reason=None,
            builder=lambda: RandomForestClassifier(
                n_estimators=320,
                max_depth=6,
                min_samples_leaf=15,
                max_features="sqrt",
                random_state=random_state,
                n_jobs=1,
            ),
            family="bagging",
        ),
        CandidateSpec(
            name="rbf_svm",
            complexity_rank=5,
            available=True,
            skip_reason=None,
            builder=lambda: SVC(
                C=1.0,
                gamma="scale",
                kernel="rbf",
                probability=True,
                random_state=random_state,
            ),
            scale_numeric=True,
            family="kernel",
        ),
        _xgboost_spec(random_state),
        _lightgbm_spec(random_state),
        _catboost_spec(random_state),
        _flaml_spec(random_state),
    )


def _preprocessor(frame: pd.DataFrame, *, scale_numeric: bool) -> ColumnTransformer:
    numeric = list(frame.select_dtypes(include=[np.number, "bool"]).columns)
    categorical = [column for column in frame.columns if column not in numeric]
    transformers: list[tuple[str, Pipeline, list[str]]] = []

    numeric_steps: list[tuple[str, object]] = [
        ("impute", SimpleImputer(strategy="median", keep_empty_features=True))
    ]
    if scale_numeric:
        numeric_steps.append(("scale", StandardScaler()))
    if numeric:
        transformers.append(("numeric", Pipeline(numeric_steps), numeric))
    if categorical:
        transformers.append(
            (
                "categorical",
                Pipeline(
                    [
                        (
                            "impute",
                            SimpleImputer(
                                strategy="constant",
                                fill_value="__missing__",
                                keep_empty_features=True,
                            ),
                        ),
                        (
                            "encode",
                            OneHotEncoder(
                                handle_unknown="ignore",
                                sparse_output=False,
                                min_frequency=2,
                            ),
                        ),
                    ]
                ),
                categorical,
            )
        )
    if not transformers:
        raise ValueError("candidate model has no usable feature columns")
    return ColumnTransformer(transformers=transformers, remainder="drop")


def build_pipeline(spec: CandidateSpec, frame: pd.DataFrame) -> Pipeline:
    if not spec.available or spec.builder is None:
        raise RuntimeError(spec.skip_reason or f"candidate {spec.name} is unavailable")
    return Pipeline(
        [
            ("preprocessor", _preprocessor(frame, scale_numeric=spec.scale_numeric)),
            ("model", spec.builder()),
        ]
    )


def fit_pipeline(
    pipeline: Pipeline,
    X: pd.DataFrame,
    y: pd.Series | np.ndarray,
    sample_weight: np.ndarray,
) -> Pipeline:
    pipeline.fit(X, y, model__sample_weight=sample_weight)
    return pipeline


def positive_probabilities(pipeline: Pipeline, X: pd.DataFrame) -> np.ndarray:
    probabilities = np.asarray(pipeline.predict_proba(X), dtype=float)
    classes = np.asarray(pipeline.classes_)
    positive_columns = np.flatnonzero(classes == 1)
    if probabilities.ndim != 2 or len(positive_columns) != 1:
        raise ValueError("candidate estimator must return binary probabilities")
    return probabilities[:, int(positive_columns[0])]
