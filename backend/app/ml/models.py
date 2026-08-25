from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, ClassifierMixin
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

from ..services.platform_configuration import read_provider_credentials


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    complexity_rank: int
    available: bool
    skip_reason: str | None
    builder: Callable[[], object] | None
    scale_numeric: bool = False
    family: str = "other"
    preprocess: bool = True
    feature_subset_sizes: tuple[int, ...] | None = None


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
            allow_writing_files=False,
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


class TabPFNProductionClassifier(ClassifierMixin, BaseEstimator):
    """Local TabPFN adapter safe for unattended scheduled training.

    V3 is preferred when its checkpoint is already cached or TABPFN_TOKEN is
    configured. Otherwise the adapter falls back to V2 instead of launching an
    interactive browser from a background worker. The actual checkpoint version
    and device are exposed on the fitted estimator for registry audit.
    """

    def __init__(
        self,
        *,
        random_state: int = 42,
        preferred_version: str = "v3",
        cpu_n_estimators: int = 1,
        gpu_n_estimators: int = 4,
    ) -> None:
        self.random_state = random_state
        self.preferred_version = preferred_version
        self.cpu_n_estimators = cpu_n_estimators
        self.gpu_n_estimators = gpu_n_estimators

    @staticmethod
    def _version_enum(value: str):
        from tabpfn.constants import ModelVersion

        normalized = str(value).strip().lower()
        mapping = {
            "v2": ModelVersion.V2,
            "v2.5": ModelVersion.V2_5,
            "v2_5": ModelVersion.V2_5,
            "v2.6": ModelVersion.V2_6,
            "v2_6": ModelVersion.V2_6,
            "v3": ModelVersion.V3,
        }
        if normalized not in mapping:
            raise ValueError(f"unsupported TabPFN model version: {value}")
        return mapping[normalized]

    def _can_use_preferred(self, version) -> bool:
        from tabpfn import TabPFNClassifier

        if str(version.value) == "v2":
            return True
        probe = TabPFNClassifier.create_default_for_version(
            version,
            device="cpu",
            n_estimators=1,
            show_progress_bar=False,
        )
        return Path(probe.model_path).exists() or bool(read_provider_credentials("tabpfn")) or bool(os.getenv("TABPFN_TOKEN"))

    def _build(self, version):
        import torch
        from tabpfn import TabPFNClassifier

        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cpu":
            os.environ.setdefault("TABPFN_ALLOW_CPU_LARGE_DATASET", "1")
        os.environ.setdefault("TABPFN_DISABLE_TELEMETRY", "1")
        os.environ.setdefault("TABPFN_NO_BROWSER", "1")
        n_estimators = (
            max(1, int(self.gpu_n_estimators))
            if device == "cuda"
            else max(1, int(self.cpu_n_estimators))
        )
        model = TabPFNClassifier.create_default_for_version(
            version,
            device=device,
            n_estimators=n_estimators,
            random_state=self.random_state,
            ignore_pretraining_limits=(device == "cpu"),
            show_progress_bar=False,
        )
        return model, device, n_estimators

    def fit(self, X, y, sample_weight=None):
        if sample_weight is not None:
            weights = np.asarray(sample_weight, dtype=float)
            if len(weights) != len(y):
                raise ValueError("TabPFN sample_weight length must match y")
            if len(weights) and not np.allclose(weights, weights[0]):
                raise ValueError(
                    "TabPFN adapter only supports the current equal-weight training policy"
                )

        preferred = self._version_enum(self.preferred_version)
        fallback = self._version_enum("v2")
        versions = [preferred]
        if fallback != preferred:
            versions.append(fallback)

        last_error: Exception | None = None
        for version in versions:
            if version == preferred and not self._can_use_preferred(version):
                continue
            try:
                model, device, n_estimators = self._build(version)
                model.fit(X, y)
            except Exception as exc:
                last_error = exc
                continue
            self.model_ = model
            self.classes_ = np.asarray(model.classes_)
            self.actual_model_version_ = str(version.value)
            self.device_ = device
            self.n_estimators_ = n_estimators
            self.model_path_ = str(model.model_path)
            return self

        if last_error is not None:
            raise RuntimeError(f"TabPFN initialization failed: {last_error}") from last_error
        raise RuntimeError(
            "TabPFN preferred checkpoint is unavailable and no fallback could be used"
        )

    def predict_proba(self, X):
        if not hasattr(self, "model_"):
            raise RuntimeError("TabPFNProductionClassifier is not fitted")
        return self.model_.predict_proba(X)


def _tabpfn_spec(random_state: int) -> CandidateSpec:
    if importlib.util.find_spec("tabpfn") is None:
        return _optional_missing("tabpfn", "tabpfn", 7, "foundation")

    from tabpfn import TabPFNClassifier
    from tabpfn.constants import ModelVersion

    v3_probe = TabPFNClassifier.create_default_for_version(
        ModelVersion.V3,
        device="cpu",
        n_estimators=1,
        show_progress_bar=False,
    )
    resolved_version = (
        "v3"
        if Path(v3_probe.model_path).exists() or bool(read_provider_credentials("tabpfn")) or bool(os.getenv("TABPFN_TOKEN"))
        else "v2"
    )
    return CandidateSpec(
        name="tabpfn",
        complexity_rank=7,
        available=True,
        skip_reason=None,
        builder=lambda: TabPFNProductionClassifier(
            random_state=random_state,
            preferred_version=resolved_version,
        ),
        family="foundation",
        preprocess=False,
        feature_subset_sizes=(4, 8, 16, 24),
    )


class FLAMLTimeSafeClassifier(ClassifierMixin, BaseEstimator):
    """FLAML adapter with a chronological inner holdout inside each outer train slice.

    The outer ModelTrainer owns the real chronological development folds and final
    holdout. FLAML only sees the already-isolated outer-training slice, keeps its
    row order, tunes on the latest ``validation_fraction`` of that slice via
    ``split_type='time'``, then retrains the selected recipe on the full outer
    training slice. This prevents AutoML from seeing an outer validation/final row.
    """

    def __init__(
        self,
        *,
        random_state: int = 42,
        time_budget_seconds: float = 5.0,
        validation_fraction: float = 0.20,
        estimator_list: tuple[str, ...] = (
            "lgbm",
            "rf",
            "extra_tree",
            "catboost",
        ),
    ) -> None:
        self.random_state = random_state
        self.time_budget_seconds = time_budget_seconds
        self.validation_fraction = validation_fraction
        self.estimator_list = estimator_list

    def fit(self, X, y, sample_weight=None):
        from flaml import AutoML

        labels = np.asarray(y)
        if labels.ndim != 1 or len(labels) != len(X):
            raise ValueError("FLAML labels must be a one-dimensional array matching X")
        if len(np.unique(labels)) != 2:
            raise ValueError("FLAML production adapter requires binary training labels")
        if sample_weight is not None:
            weights = np.asarray(sample_weight, dtype=float)
            if len(weights) != len(labels):
                raise ValueError("FLAML sample_weight length must match y")
            # The current model-training policy intentionally uses equal
            # classification weights. FLAML 2.6 + sklearn 1.9 has a time-holdout
            # length bug when an all-one sample_weight is passed, so validate the
            # invariant and omit the redundant vector rather than weakening the
            # chronological split.
            if len(weights) and not np.allclose(weights, weights[0]):
                raise ValueError(
                    "FLAML adapter only supports the current equal-weight training policy"
                )

        if not 0.05 <= float(self.validation_fraction) <= 0.40:
            raise ValueError("FLAML validation_fraction must be between 0.05 and 0.40")
        if float(self.time_budget_seconds) <= 0:
            raise ValueError("FLAML time_budget_seconds must be positive")

        automl = AutoML()
        automl.fit(
            X_train=X,
            y_train=labels,
            task="classification",
            metric="log_loss",
            time_budget=float(self.time_budget_seconds),
            estimator_list=list(self.estimator_list),
            eval_method="holdout",
            split_type="time",
            split_ratio=float(self.validation_fraction),
            retrain_full=True,
            auto_augment=False,
            allow_label_overlap=False,
            seed=int(self.random_state),
            n_jobs=1,
            verbose=0,
        )
        self.automl_ = automl
        self.classes_ = np.asarray(automl.classes_)
        self.flaml_best_estimator_ = str(automl.best_estimator)
        self.flaml_best_config_ = dict(automl.best_config or {})
        self.flaml_time_budget_seconds_ = float(self.time_budget_seconds)
        self.flaml_split_type_ = "time"
        self.flaml_eval_method_ = "holdout"
        self.flaml_validation_fraction_ = float(self.validation_fraction)
        self.flaml_estimator_list_ = tuple(self.estimator_list)
        return self

    def predict_proba(self, X):
        if not hasattr(self, "automl_"):
            raise RuntimeError("FLAMLTimeSafeClassifier is not fitted")
        return self.automl_.predict_proba(X)


def _flaml_spec(random_state: int) -> CandidateSpec:
    if importlib.util.find_spec("flaml") is None:
        return _optional_missing("flaml_automl", "flaml", 8, "automl")
    return CandidateSpec(
        name="flaml_automl",
        complexity_rank=8,
        available=True,
        skip_reason=None,
        builder=lambda: FLAMLTimeSafeClassifier(random_state=random_state),
        family="automl",
        # AutoML already searches model/hyperparameter space internally. Use a
        # sparse outer feature grid to keep daily scheduled training bounded.
        feature_subset_sizes=(4, 8, 16, 24),
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
                probability=False,
                random_state=random_state,
            ),
            scale_numeric=True,
            family="kernel",
        ),
        _xgboost_spec(random_state),
        _lightgbm_spec(random_state),
        _catboost_spec(random_state),
        _tabpfn_spec(random_state),
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
    if not spec.preprocess:
        return Pipeline([("model", spec.builder())])
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

def positive_raw_scores(pipeline: Pipeline, X: pd.DataFrame) -> np.ndarray:
    """Return an auditable one-dimensional score before project calibration."""
    if hasattr(pipeline, "decision_function"):
        scores = np.asarray(pipeline.decision_function(X), dtype=float)
        if scores.ndim == 2:
            classes = np.asarray(pipeline.classes_)
            positive_columns = np.flatnonzero(classes == 1)
            if len(positive_columns) != 1:
                raise ValueError("candidate estimator has no unique positive decision score")
            scores = scores[:, int(positive_columns[0])]
        return np.asarray(scores, dtype=float).reshape(-1)
    probabilities = np.clip(positive_probabilities(pipeline, X), 1e-8, 1.0 - 1e-8)
    return np.log(probabilities / (1.0 - probabilities))


def raw_probability_proxy(pipeline: Pipeline, X: pd.DataFrame) -> np.ndarray:
    """Return native probability when available, otherwise sigmoid(decision score)."""
    if hasattr(pipeline, "predict_proba"):
        return np.clip(positive_probabilities(pipeline, X), 0.0, 1.0)
    scores = np.clip(positive_raw_scores(pipeline, X), -50.0, 50.0)
    return 1.0 / (1.0 + np.exp(-scores))
