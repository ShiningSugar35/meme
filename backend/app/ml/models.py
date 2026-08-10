from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from typing import Callable

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import (
    ExtraTreesClassifier,
    HistGradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    complexity_rank: int
    available: bool
    skip_reason: str | None
    builder: Callable[[], object] | None
    scale_numeric: bool = False


def _xgboost_spec(random_state: int) -> CandidateSpec:
    if importlib.util.find_spec("xgboost") is None:
        return CandidateSpec(
            name="xgboost",
            complexity_rank=5,
            available=False,
            skip_reason=(
                "optional dependency 'xgboost' is not installed; candidate was "
                "skipped without affecting the other four models"
            ),
            builder=None,
        )

    def build() -> object:
        from xgboost import XGBClassifier

        return XGBClassifier(
            n_estimators=200,
            max_depth=2,
            learning_rate=0.03,
            min_child_weight=20,
            subsample=0.85,
            colsample_bytree=0.85,
            reg_lambda=5.0,
            reg_alpha=0.1,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=random_state,
            n_jobs=1,
        )

    return CandidateSpec(
        name="xgboost",
        complexity_rank=5,
        available=True,
        skip_reason=None,
        builder=build,
    )


def candidate_catalog(random_state: int = 42) -> tuple[CandidateSpec, ...]:
    return (
        CandidateSpec(
            name="logistic_regression",
            complexity_rank=1,
            available=True,
            skip_reason=None,
            builder=lambda: LogisticRegression(
                C=0.5,
                max_iter=1_000,
                solver="lbfgs",
                random_state=random_state,
            ),
            scale_numeric=True,
        ),
        CandidateSpec(
            name="hist_gradient_boosting",
            complexity_rank=2,
            available=True,
            skip_reason=None,
            builder=lambda: HistGradientBoostingClassifier(
                max_depth=2,
                learning_rate=0.03,
                max_iter=150,
                min_samples_leaf=30,
                l2_regularization=5.0,
                early_stopping=False,
                random_state=random_state,
            ),
        ),
        _xgboost_spec(random_state),
        CandidateSpec(
            name="extra_trees",
            complexity_rank=4,
            available=True,
            skip_reason=None,
            builder=lambda: ExtraTreesClassifier(
                n_estimators=240,
                max_depth=6,
                min_samples_leaf=15,
                max_features="sqrt",
                random_state=random_state,
                n_jobs=1,
            ),
        ),
        CandidateSpec(
            name="random_forest",
            complexity_rank=4,
            available=True,
            skip_reason=None,
            builder=lambda: RandomForestClassifier(
                n_estimators=240,
                max_depth=6,
                min_samples_leaf=15,
                max_features="sqrt",
                random_state=random_state,
                n_jobs=1,
            ),
        ),
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
