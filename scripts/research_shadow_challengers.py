from __future__ import annotations

import gzip
import json
import math
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.app.config import PROJECT_ROOT
from backend.app.ml.features import (
    AVAILABLE_MODEL_FEATURES,
    DEFAULT_MODEL_TRAINING_FEATURES,
    FeatureBuilder,
    FeaturePolicy,
    materialize_entry_feature,
)
from backend.app.ml.trainer import ModelTrainer, TrainerConfig
from scripts.research_label_policy_grid import replay

CACHE = PROJECT_ROOT / "artifacts/research/kline_cache_v3_2h.json.gz"
OUT = PROJECT_ROOT / "artifacts/research/shadow_challenger_training_1183.json"
STOP_STRESS_RETURN = -0.21925559655074778
TP_STRESS_HAIRCUT = 0.05
BUDGETS = (0.05, 0.075, 0.10, 0.15, 0.20)


def load_snapshot() -> tuple[dict[str, Any], list[dict[str, Any]], tuple[str, ...]]:
    with gzip.open(CACHE, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    items = payload["items"]
    ids = {int(key) for key in items}
    connection = sqlite3.connect(f"file:{(PROJECT_ROOT / 'data/meme_quant.db').as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT * FROM samples
            WHERE label_status='mature' AND tag IN (0,1)
              AND token_type IN ('new_creation','near_completion')
              AND feature_schema_version='event1m_regime_v3'
            ORDER BY entry_time,id
            """
        )
        if int(row["id"]) in ids
    ]
    # Match SampleRepository.list_mature(): production training materializes the
    # canonical features_json payload onto each sample row before feature building.
    for row in rows:
        try:
            features = json.loads(row.pop("features_json", None) or "{}")
        except (TypeError, json.JSONDecodeError):
            features = {}
        row.pop("raw_json", None)
        row.update(features)
    feature_key = "model_training_feature_pool:event1m_regime_v3"
    state = connection.execute("SELECT value_json FROM runtime_state WHERE key=?", (feature_key,)).fetchone()
    connection.close()
    if {int(row["id"]) for row in rows} != ids:
        raise RuntimeError(f"snapshot mismatch: db={len(rows)} cache={len(ids)}")
    if state:
        try:
            selected = tuple(json.loads(state[0]))
        except Exception:
            selected = DEFAULT_MODEL_TRAINING_FEATURES
    else:
        selected = DEFAULT_MODEL_TRAINING_FEATURES
    selected = tuple(name for name in AVAILABLE_MODEL_FEATURES if name in set(selected))
    return items, rows, selected


def final_close_ratio(item: dict[str, Any], entry_time: int, entry_price: float, window: int) -> float | None:
    end_ts = entry_time + window * 60
    closes = [
        (int(bar[0]), float(bar[4]))
        for bar in item["bars"]
        if entry_time <= int(bar[0]) <= end_ts and bar[4] not in (None, 0)
    ]
    if not closes or entry_price <= 0:
        return None
    return max(closes, key=lambda pair: pair[0])[1] / entry_price


def make_records(
    items: dict[str, Any],
    rows: list[dict[str, Any]],
    feature_names: tuple[str, ...],
    *,
    tp: float,
    sl: float,
    window: int,
    subset: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for row in rows:
        age = float(row.get("age_minutes") or 0.0)
        if subset == "age_lt_60" and not age < 60.0:
            continue
        if subset == "exclude_60_120" and 60.0 <= age < 120.0:
            continue
        if subset == "only_60_120" and not (60.0 <= age < 120.0):
            continue
        item = items[str(row["id"])]
        outcome = replay(item, tp, sl, window, None, None)
        if outcome is None:
            continue
        entry_price = float(row["entry_price"])
        features = {
            key: materialize_entry_feature(key, row, entry_price=entry_price)
            for key in AVAILABLE_MODEL_FEATURES
        }
        features.update(
            {
                "time": row["entry_time"],
                "tag": int(outcome["tag"]),
                "launchpad": row.get("launchpad"),
                "liquidity_usd": row.get("liquidity"),
                "final_close_ratio": final_close_ratio(
                    item, int(row["entry_time"]), entry_price, window
                ),
                "execution_invested_usd": None,
                "execution_net_pnl_usd": None,
                "execution_observed": False,
            }
        )
        records.append(features)
        stress_return = float(outcome.get("proxy_ret") or 0.0)
        if outcome["reason"] == "hard_sl":
            stress_return = STOP_STRESS_RETURN
        elif outcome["reason"] == "tp":
            stress_return = max(0.0, tp - 1.0 - TP_STRESS_HAIRCUT)
        outcomes.append(
            {
                "sample_id": int(row["id"]),
                "age_minutes": age,
                "tag": int(outcome["tag"]),
                "reason": str(outcome["reason"]),
                "stress_return": stress_return,
            }
        )
    return pd.DataFrame.from_records(records), outcomes


def prior_adjust(probabilities: np.ndarray, source_prior: float, target_prior: float) -> np.ndarray:
    eps = 1e-6
    p = np.clip(np.asarray(probabilities, dtype=float), eps, 1.0 - eps)
    sp = float(np.clip(source_prior, eps, 1.0 - eps))
    tp = float(np.clip(target_prior, eps, 1.0 - eps))
    odds = p / (1.0 - p)
    multiplier = (tp / (1.0 - tp)) / (sp / (1.0 - sp))
    adjusted_odds = odds * multiplier
    return adjusted_odds / (1.0 + adjusted_odds)


def selected_metrics(
    probabilities: np.ndarray,
    labels: np.ndarray,
    stress_returns: np.ndarray,
    selected: np.ndarray,
) -> dict[str, Any]:
    count = int(selected.sum())
    if count == 0:
        return {"trades": 0, "precision": 0.0, "stress_mean_return": None}
    return {
        "trades": count,
        "precision": float(labels[selected].mean()),
        "wins": int(labels[selected].sum()),
        "stress_mean_return": float(stress_returns[selected].mean()),
        "mean_probability": float(probabilities[selected].mean()),
    }


def summarize_result(
    name: str,
    policy: dict[str, Any],
    subset: str,
    dataset: Any,
    outcomes: list[dict[str, Any]],
    result: Any,
    elapsed: float,
) -> dict[str, Any]:
    candidates = []
    for candidate in result.candidates:
        candidates.append(
            {
                "algorithm": candidate.algorithm,
                "status": candidate.status,
                "skip_reason": candidate.skip_reason,
                "threshold": candidate.threshold,
                "feature_names": list(candidate.feature_names),
                "development_metrics": asdict(candidate.development_metrics)
                if candidate.development_metrics
                else None,
                "final_metrics": asdict(candidate.final_metrics) if candidate.final_metrics else None,
                "generalization": asdict(candidate.generalization) if candidate.generalization else None,
                "economic_score": candidate.economic_score,
                "ranking_economic_score": candidate.ranking_economic_score,
                "composite_score": candidate.composite_score,
                "execution_score": candidate.execution_score,
                "execution_observations": candidate.execution_observations,
            }
        )

    test_indices = np.asarray(result.plan.final_split.test_indices, dtype=int)
    source_rows = dataset.source_rows.to_numpy(dtype=int)
    aligned = [outcomes[int(source_rows[index])] for index in test_indices]
    labels = np.asarray([item["tag"] for item in aligned], dtype=int)
    stress_returns = np.asarray([item["stress_return"] for item in aligned], dtype=float)
    train_indices = np.asarray(result.plan.final_split.train_indices, dtype=int)
    source_prior = float(dataset.y.iloc[train_indices].mean())
    recent_train = train_indices[-min(100, len(train_indices)) :]
    target_prior = float(dataset.y.iloc[recent_train].mean())

    top_models: list[dict[str, Any]] = []
    for bundle in result.evaluation_bundles:
        probabilities = bundle.predict_probabilities(dataset.X.iloc[test_indices])
        threshold = float(bundle.threshold)
        raw = selected_metrics(
            probabilities,
            labels,
            stress_returns,
            probabilities >= threshold,
        )
        adjusted_probabilities = prior_adjust(probabilities, source_prior, target_prior)
        adjusted = selected_metrics(
            adjusted_probabilities,
            labels,
            stress_returns,
            adjusted_probabilities >= threshold,
        )
        budgets: list[dict[str, Any]] = []
        order = np.argsort(-probabilities)
        for fraction in BUDGETS:
            n = max(1, int(round(len(labels) * fraction)))
            mask = np.zeros(len(labels), dtype=bool)
            mask[order[:n]] = True
            budgets.append(
                {
                    "fraction": fraction,
                    **selected_metrics(probabilities, labels, stress_returns, mask),
                }
            )
        top_models.append(
            {
                "algorithm": bundle.algorithm,
                "threshold": threshold,
                "feature_names": list(bundle.feature_names),
                "raw_threshold": raw,
                "prior_adjusted_same_threshold": adjusted,
                "trade_budgets": budgets,
            }
        )

    return {
        "name": name,
        "policy": policy,
        "subset": subset,
        "rows": len(dataset),
        "positive": int(dataset.y.sum()),
        "positive_rate": float(dataset.y.mean()),
        "final_holdout_rows": len(test_indices),
        "final_holdout_positive": int(labels.sum()),
        "final_holdout_rate": float(labels.mean()),
        "source_prior": source_prior,
        "prior_estimate_last_100_train": target_prior,
        "elapsed_seconds": elapsed,
        "top_algorithms": list(result.top_algorithms),
        "rule_baseline_final": asdict(result.rule_baseline),
        "diversity": dict(result.diversity_metrics),
        "warnings": list(result.warnings),
        "candidates": candidates,
        "top_model_shadow_execution": top_models,
    }


def run_training(
    items: dict[str, Any],
    rows: list[dict[str, Any]],
    features: tuple[str, ...],
    *,
    name: str,
    tp: float,
    sl: float,
    window: int,
    subset: str,
    candidate_names: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    print(json.dumps({"stage": "start", "name": name, "subset": subset}), flush=True)
    frame, outcomes = make_records(
        items,
        rows,
        features,
        tp=tp,
        sl=sl,
        window=window,
        subset=subset,
    )
    dataset = FeatureBuilder(FeaturePolicy(feature_allowlist=features)).prepare(frame)
    config = TrainerConfig() if candidate_names is None else TrainerConfig(candidate_names=candidate_names)
    started = time.time()
    result = ModelTrainer(config).train(dataset)
    elapsed = time.time() - started
    summary = summarize_result(
        name,
        {"tp": tp, "sl": sl, "window_minutes": window},
        subset,
        dataset,
        outcomes,
        result,
        elapsed,
    )
    print(
        json.dumps(
            {
                "stage": "complete",
                "name": name,
                "rows": summary["rows"],
                "positive_rate": summary["positive_rate"],
                "top_algorithms": summary["top_algorithms"],
                "elapsed_seconds": round(elapsed, 1),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return summary


def main() -> None:
    items, rows, features = load_snapshot()
    payload: dict[str, Any] = {
        "snapshot_rows": len(rows),
        "snapshot_max_sample_id": max(int(row["id"]) for row in rows),
        "feature_pool": list(features),
        "feature_count": len(features),
        "objective": "fixed_3_to_1_v3",
        "execution_shadow_disabled": True,
        "stop_stress_return": STOP_STRESS_RETURN,
        "tp_stress_haircut": TP_STRESS_HAIRCUT,
        "runs": [],
    }

    p18 = run_training(
        items, rows, features, name="p18_90_full", tp=1.8, sl=0.9, window=90, subset="all"
    )
    payload["runs"].append(p18)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    p20 = run_training(
        items, rows, features, name="p20_90_full", tp=2.0, sl=0.9, window=90, subset="all"
    )
    payload["runs"].append(p20)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    reduced = tuple(dict.fromkeys(("logistic_regression", *p18["top_algorithms"], *p20["top_algorithms"])))
    payload["ablation_candidate_pool"] = list(reduced)
    for name, subset in (
        ("p18_90_age_lt60", "age_lt_60"),
        ("p18_90_exclude_60_120", "exclude_60_120"),
    ):
        run = run_training(
            items,
            rows,
            features,
            name=name,
            tp=1.8,
            sl=0.9,
            window=90,
            subset=subset,
            candidate_names=reduced,
        )
        payload["runs"].append(run)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"saved": str(OUT), "runs": len(payload["runs"])}), flush=True)


if __name__ == "__main__":
    main()
