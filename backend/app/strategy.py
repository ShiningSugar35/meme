from __future__ import annotations

from typing import Final

MODEL_STRATEGIES: Final[tuple[str, ...]] = ("model_1", "model_2", "model_3")
RULES_ONLY: Final[str] = "rules_only"
SIMULATION_STRATEGIES: Final[tuple[str, ...]] = (*MODEL_STRATEGIES, RULES_ONLY)

STRATEGY_LABELS: Final[dict[str, str]] = {
    "model_1": "模型 1",
    "model_2": "模型 2",
    "model_3": "模型 3",
    "rules_only": "不用模型",
}

ALGORITHM_DISPLAY_NAMES: Final[dict[str, str]] = {
    "logistic_regression": "Logistic Regression",
    "decision_tree": "Decision Tree",
    "hist_gradient_boosting": "Histogram Gradient Boosting",
    "gradient_boosting": "Gradient Boosting",
    "ada_boost": "AdaBoost",
    "extra_trees": "Extra Trees",
    "random_forest": "Random Forest",
    "rbf_svm": "RBF SVM",
    "xgboost": "XGBoost",
    "lightgbm": "LightGBM",
    "catboost": "CatBoost",
    "flaml_automl": "FLAML AutoML",
}


def algorithm_display_name(algorithm: str | None) -> str:
    key = str(algorithm or "Model")
    return ALGORITHM_DISPLAY_NAMES.get(key, key)


def validate_strategy(strategy_key: str) -> str:
    if strategy_key not in SIMULATION_STRATEGIES:
        raise ValueError(f"unknown simulation strategy: {strategy_key}")
    return strategy_key


def model_strategy(slot: int) -> str:
    if slot not in (1, 2, 3):
        raise ValueError("active model slot must be 1, 2 or 3")
    return f"model_{slot}"
