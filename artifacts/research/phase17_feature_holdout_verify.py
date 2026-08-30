from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Callable

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "meme_quant.db"
FEATURE_SCHEMA_VERSION = "event1m_regime_v3"
LABEL_VERSION = "sl090_tp180_m90_binary_v5"


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def log1p_return(value: Any) -> float | None:
    number = finite(value)
    if number is None or number <= -1.0:
        return None
    return math.log1p(number)


def momentum_accel_1m_vs_5m(source: dict[str, Any]) -> float | None:
    one = log1p_return(source.get("price_change_1m"))
    five = log1p_return(source.get("price_change_5m"))
    if one is None or five is None:
        return None
    return one - five / 5.0


def psi_reference(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    values = values[np.isfinite(values)]
    edges = np.unique(np.quantile(values, [0.2, 0.4, 0.6, 0.8]))
    bins = np.searchsorted(edges, values, side="right")
    counts = np.bincount(bins, minlength=len(edges) + 1).astype(float)
    return edges, counts / counts.sum()


def psi(values: np.ndarray, reference: tuple[np.ndarray, np.ndarray]) -> float:
    values = values[np.isfinite(values)]
    edges, expected = reference
    bins = np.searchsorted(edges, values, side="right")
    counts = np.bincount(bins, minlength=len(edges) + 1).astype(float)
    actual = counts / counts.sum()
    eps = 1e-6
    expected = np.clip(expected, eps, None)
    actual = np.clip(actual, eps, None)
    return float(np.sum((actual - expected) * np.log(actual / expected)))


def rank_direction(values: np.ndarray, labels: np.ndarray) -> float:
    mask = np.isfinite(values)
    x = values[mask]
    y = labels[mask]
    rx = np.argsort(np.argsort(x))
    ry = np.argsort(np.argsort(y))
    corr = np.corrcoef(rx, ry)[0, 1]
    return 1.0 if not np.isfinite(corr) or corr >= 0 else -1.0


def evaluate(dev: np.ndarray, dev_y: np.ndarray, holdout: np.ndarray, holdout_y: np.ndarray) -> dict[str, float | int | None]:
    dev_mask = np.isfinite(dev)
    test_mask = np.isfinite(holdout)
    if dev_mask.sum() < 100 or test_mask.sum() < 50:
        return {"coverage": float(test_mask.mean()), "psi": None, "auc": None, "ap_lift": None}
    direction = rank_direction(dev, dev_y)
    scores = direction * holdout[test_mask]
    labels = holdout_y[test_mask]
    ap = float(average_precision_score(labels, scores))
    return {
        "coverage": float(test_mask.mean()),
        "psi": psi(holdout, psi_reference(dev)),
        "auc": float(roc_auc_score(labels, scores)),
        "ap_lift": ap - float(np.mean(labels)),
        "direction": direction,
        "rows": int(test_mask.sum()),
    }


def main() -> None:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT entry_time,features_json,tag
            FROM samples
            WHERE feature_schema_version=? AND label_version=?
              AND label_status='mature' AND tag IN (0,1)
              AND token_type IN ('new_creation','near_completion')
            ORDER BY entry_time,id
            """,
            (FEATURE_SCHEMA_VERSION, LABEL_VERSION),
        )
    ]
    sources: list[dict[str, Any]] = []
    for row in rows:
        try:
            sources.append(json.loads(row.get("features_json") or "{}"))
        except json.JSONDecodeError:
            sources.append({})
    labels = np.asarray([int(row["tag"]) for row in rows], dtype=int)
    split = int(math.floor(len(rows) * 0.80))

    frozen: dict[str, Callable[[dict[str, Any]], float | None]] = {
        "price_change_1h": lambda s: finite(s.get("price_change_1h")),
        "momentum_accel_1m_vs_5m": momentum_accel_1m_vs_5m,
        "ln(volume_1m+1)": lambda s: finite(s.get("ln(volume_1m+1)")),
        "volume_1h/swaps_1h": lambda s: finite(s.get("volume_1h/swaps_1h")),
        "liquidity/holder_count": lambda s: finite(s.get("liquidity/holder_count")),
    }
    output: dict[str, Any] = {
        "rows": len(rows),
        "development_rows": split,
        "untouched_holdout_rows": len(rows) - split,
        "note": "Candidate replacement was frozen from development audit before this script inspected the reserved latest 20%.",
        "features": {},
    }
    for name, fn in frozen.items():
        values = np.asarray([
            np.nan if (value := fn(source)) is None else float(value)
            for source in sources
        ], dtype=float)
        output["features"][name] = evaluate(
            values[:split], labels[:split], values[split:], labels[split:]
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
