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
PSI_CAUTION = 0.10
PSI_SEVERE = 0.25


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


def expm1_nonnegative(value: Any) -> float | None:
    number = finite(value)
    if number is None:
        return None
    try:
        result = math.expm1(number)
    except OverflowError:
        return None
    return result if math.isfinite(result) and result >= 0 else None


def holder_count(source: dict[str, Any]) -> float | None:
    age_log = finite(source.get("ln(age+1)"))
    growth = finite(source.get("holder_count/age"))
    if age_log is None or growth is None or growth < 0:
        return None
    age = math.expm1(age_log)
    count = growth * age
    return count if math.isfinite(count) and count > 0 else None


def count_share(source: dict[str, Any], count_key: str) -> float | None:
    count = expm1_nonnegative(source.get(count_key))
    holders = holder_count(source)
    if count is None or holders is None or holders <= 0:
        return None
    return count / holders


def trade_size_pressure(source: dict[str, Any]) -> float | None:
    short_log1p = finite(source.get("ln(volume_1m/swaps_1m+1)"))
    hour_log = finite(source.get("volume_1h/swaps_1h"))
    if short_log1p is None or hour_log is None:
        return None
    short_avg = math.expm1(short_log1p)
    hour_avg = math.exp(hour_log)
    if short_avg < 0 or hour_avg <= 0:
        return None
    return math.log1p(short_avg) - math.log1p(hour_avg)


def momentum_1m_vs_5m(source: dict[str, Any]) -> float | None:
    one = log1p_return(source.get("price_change_1m"))
    five = log1p_return(source.get("price_change_5m"))
    if one is None or five is None:
        return None
    return one - five / 5.0


def momentum_5m_vs_1h(source: dict[str, Any]) -> float | None:
    five = log1p_return(source.get("price_change_5m"))
    hour = log1p_return(source.get("price_change_1h"))
    if five is None or hour is None:
        return None
    return five / 5.0 - hour / 60.0


def log_holder_growth(source: dict[str, Any]) -> float | None:
    value = finite(source.get("holder_count/age"))
    return math.log1p(value) if value is not None and value >= 0 else None


DERIVED: dict[str, Callable[[dict[str, Any]], float | None]] = {
    "trade_size_pressure_1m_vs_1h": trade_size_pressure,
    "momentum_accel_1m_vs_5m": momentum_1m_vs_5m,
    "momentum_accel_5m_vs_1h": momentum_5m_vs_1h,
    "ln(holder_growth_per_min+1)": log_holder_growth,
    "smart_degen_share_of_holders": lambda s: count_share(s, "ln(smart_degen_count+1)"),
    "renowned_share_of_holders": lambda s: count_share(s, "ln(renowned_count+1)"),
    "visiting_share_of_holders": lambda s: count_share(s, "ln(visiting_count+1)"),
    "top_wallet_share_of_holders": lambda s: count_share(s, "ln(top_wallets+1)"),
}


def psi_reference(values: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    values = values[np.isfinite(values)]
    if len(values) < 40:
        return None
    edges = np.unique(np.quantile(values, [0.2, 0.4, 0.6, 0.8]))
    bins = np.searchsorted(edges, values, side="right")
    counts = np.bincount(bins, minlength=len(edges) + 1).astype(float)
    proportions = counts / counts.sum()
    return edges, proportions


def psi(values: np.ndarray, reference: tuple[np.ndarray, np.ndarray] | None) -> float | None:
    values = values[np.isfinite(values)]
    if reference is None or len(values) < 20:
        return None
    edges, expected = reference
    bins = np.searchsorted(edges, values, side="right")
    counts = np.bincount(bins, minlength=len(edges) + 1).astype(float)
    actual = counts / max(counts.sum(), 1.0)
    eps = 1e-6
    expected = np.clip(expected, eps, None)
    actual = np.clip(actual, eps, None)
    return float(np.sum((actual - expected) * np.log(actual / expected)))


def feature_score(reference_x: np.ndarray, reference_y: np.ndarray, test_x: np.ndarray, test_y: np.ndarray) -> dict[str, float | None]:
    ref_mask = np.isfinite(reference_x)
    test_mask = np.isfinite(test_x)
    if ref_mask.sum() < 30 or test_mask.sum() < 20 or len(np.unique(reference_y[ref_mask])) < 2 or len(np.unique(test_y[test_mask])) < 2:
        return {"auc": None, "ap": None, "ap_lift": None, "direction": None}
    ref_x = reference_x[ref_mask]
    ref_y = reference_y[ref_mask]
    corr = np.corrcoef(np.argsort(np.argsort(ref_x)), np.argsort(np.argsort(ref_y)))[0, 1]
    direction = 1.0 if not np.isfinite(corr) or corr >= 0 else -1.0
    scores = direction * test_x[test_mask]
    labels = test_y[test_mask]
    auc = float(roc_auc_score(labels, scores))
    ap = float(average_precision_score(labels, scores))
    prevalence = float(np.mean(labels))
    return {"auc": auc, "ap": ap, "ap_lift": ap - prevalence, "direction": direction}


def main() -> None:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT id,entry_time,entry_price,age_minutes,features_json,tag
            FROM samples
            WHERE feature_schema_version=? AND label_version=?
              AND label_status='mature' AND tag IN (0,1)
              AND token_type IN ('new_creation','near_completion')
            ORDER BY entry_time,id
            """,
            (FEATURE_SCHEMA_VERSION, LABEL_VERSION),
        )
    ]
    if len(rows) < 500:
        raise SystemExit("not enough mature rows")

    sources: list[dict[str, Any]] = []
    for row in rows:
        try:
            source = json.loads(row.get("features_json") or "{}")
        except json.JSONDecodeError:
            source = {}
        price = finite(row.get("entry_price"))
        source["ln(price+1)"] = math.log1p(price) if price is not None and price >= 0 else None
        sources.append(source)

    feature_names = sorted({key for source in sources for key in source if key != "ln(liquidity_usd)"})
    feature_names += list(DERIVED)
    y = np.asarray([int(row["tag"]) for row in rows], dtype=int)
    n = len(rows)
    development_end = int(math.floor(n * 0.80))
    development_end = max(400, min(development_end, n - 100))
    dev_sources = sources[:development_end]
    dev_y = y[:development_end]

    # Four chronological development blocks; each transition uses only prior rows
    # as reference and the immediately following block as unseen evaluation.
    boundaries = np.linspace(0, development_end, 5, dtype=int)
    transitions = [(0, boundaries[i], boundaries[i], boundaries[i + 1]) for i in range(1, 4)]

    matrix: dict[str, np.ndarray] = {}
    for name in feature_names:
        values: list[float] = []
        for source in dev_sources:
            if name in DERIVED:
                value = DERIVED[name](source)
            else:
                value = finite(source.get(name))
            values.append(np.nan if value is None else float(value))
        matrix[name] = np.asarray(values, dtype=float)

    results: list[dict[str, Any]] = []
    for name in feature_names:
        x = matrix[name]
        coverage = float(np.isfinite(x).mean())
        psis: list[float] = []
        aucs: list[float] = []
        lifts: list[float] = []
        for ref_start, ref_end, test_start, test_end in transitions:
            ref_x = x[ref_start:ref_end]
            test_x = x[test_start:test_end]
            value = psi(test_x, psi_reference(ref_x))
            if value is not None:
                psis.append(value)
            score = feature_score(ref_x, dev_y[ref_start:ref_end], test_x, dev_y[test_start:test_end])
            if score["auc"] is not None:
                aucs.append(float(score["auc"]))
            if score["ap_lift"] is not None:
                lifts.append(float(score["ap_lift"]))
        results.append(
            {
                "feature": name,
                "derived": name in DERIVED,
                "coverage": coverage,
                "psi_mean": float(np.mean(psis)) if psis else None,
                "psi_max": float(np.max(psis)) if psis else None,
                "psi_caution_frequency": float(np.mean(np.asarray(psis) >= PSI_CAUTION)) if psis else None,
                "psi_severe_frequency": float(np.mean(np.asarray(psis) >= PSI_SEVERE)) if psis else None,
                "future_auc_mean": float(np.mean(aucs)) if aucs else None,
                "future_ap_lift_mean": float(np.mean(lifts)) if lifts else None,
                "future_ap_lift_min": float(np.min(lifts)) if lifts else None,
            }
        )

    results.sort(key=lambda item: (-(item["psi_mean"] or -1), item["feature"]))
    output = {
        "rows_total": n,
        "development_rows_used_for_feature_governance": development_end,
        "reserved_latest_rows_not_used_for_feature_decisions": n - development_end,
        "development_start": rows[0]["entry_time"],
        "development_end": rows[development_end - 1]["entry_time"],
        "method": "expanding-reference chronological PSI + direction-frozen one-feature future AUC/AP; latest 20% untouched",
        "features": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
