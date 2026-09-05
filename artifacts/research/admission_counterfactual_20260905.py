from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "meme_quant.db"
CURRENT_LABEL = "sl090_tp180_m90_binary_v5"
TASK_CUTOFF_ISO = "2026-09-05T13:59:44+00:00"
TASK_CUTOFF_EPOCH = int(datetime.fromisoformat(TASK_CUTOFF_ISO).timestamp())
ALLOWED_TYPES = {"new_creation", "near_completion"}
MARKETCAP_MIN = 5_000.0
ORIGINAL_LIQUIDITY_MIN = 4_800.0
NEW_LIQUIDITY_MIN = 5_000.0
ORIGINAL_AGE_MIN = 2.0
ORIGINAL_AGE_MAX = 300.0
NEW_AGE_MIN = 5.0
NEW_AGE_MAX = 240.0


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _recursive_find(value: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if candidate not in (None, ""):
                return candidate
        for nested in value.values():
            found = _recursive_find(nested, keys, depth + 1)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _recursive_find(nested, keys, depth + 1)
            if found not in (None, ""):
                return found
    return None


def _marketcap(features: dict[str, Any], raw: dict[str, Any]) -> float | None:
    direct = _finite(
        _recursive_find(
            raw,
            (
                "marketcap",
                "market_cap",
                "marketCap",
                "fdv",
                "fully_diluted_valuation",
                "usd_market_cap",
            ),
        )
    )
    if direct is not None and direct >= 0:
        return direct
    logged = _finite(features.get("ln(marketcap+1)"))
    if logged is not None:
        try:
            value = math.expm1(logged)
        except OverflowError:
            return None
        return value if math.isfinite(value) and value >= 0 else None
    ratio_log = _finite(features.get("ln(marketcap/liquidity)"))
    liquidity_log = _finite(features.get("ln(liquidity_usd)"))
    if ratio_log is not None and liquidity_log is not None:
        try:
            value = math.exp(ratio_log + liquidity_log)
        except OverflowError:
            return None
        return value if math.isfinite(value) and value >= 0 else None
    return None


def _wilson(successes: int, total: int, z: float = 1.959963984540054) -> list[float] | None:
    if total <= 0:
        return None
    p = successes / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    center = (p + z2 / (2.0 * total)) / denominator
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total) / denominator
    return [max(0.0, center - margin), min(1.0, center + margin)]


def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    mature = [
        item
        for item in items
        if item["label_status"] == "mature"
        and item["tag"] in (0, 1)
        and item["label_version"] == CURRENT_LABEL
    ]
    positives = sum(int(item["tag"] == 1) for item in mature)
    by_type: dict[str, dict[str, Any]] = {}
    for token_type in sorted(ALLOWED_TYPES):
        typed = [item for item in items if item["token_type"] == token_type]
        typed_mature = [
            item
            for item in typed
            if item["label_status"] == "mature"
            and item["tag"] in (0, 1)
            and item["label_version"] == CURRENT_LABEL
        ]
        typed_positives = sum(int(item["tag"] == 1) for item in typed_mature)
        by_type[token_type] = {
            "samples": len(typed),
            "mature_v5": len(typed_mature),
            "positives_v5": typed_positives,
            "positive_rate_v5": typed_positives / len(typed_mature) if typed_mature else None,
        }
    return {
        "samples": len(items),
        "mature_v5": len(mature),
        "positives_v5": positives,
        "positive_rate_v5": positives / len(mature) if mature else None,
        "positive_rate_wilson95_v5": _wilson(positives, len(mature)),
        "by_type": by_type,
    }


def main() -> int:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT id, sample_key, address, token_type, entry_time, age_minutes,
               liquidity, features_json, raw_json, label_status, tag,
               label_version, feature_schema_version
        FROM samples
        WHERE token_type IN ('new_creation','near_completion')
          AND entry_time <= ?
        ORDER BY entry_time, id
        """,
        (TASK_CUTOFF_EPOCH,),
    ).fetchall()

    normalized: list[dict[str, Any]] = []
    schema_counts: Counter[str] = Counter()
    missing = Counter()
    for row in rows:
        features = json.loads(row["features_json"] or "{}")
        raw = json.loads(row["raw_json"] or "{}")
        marketcap = _marketcap(features, raw)
        liquidity = _finite(row["liquidity"])
        age = _finite(row["age_minutes"])
        if marketcap is None:
            missing["marketcap"] += 1
        if liquidity is None:
            missing["liquidity"] += 1
        if age is None:
            missing["age"] += 1
        schema_counts[str(row["feature_schema_version"] or "unknown")] += 1
        normalized.append(
            {
                "id": int(row["id"]),
                "sample_key": str(row["sample_key"]),
                "address": str(row["address"]),
                "token_type": str(row["token_type"]),
                "entry_time": int(row["entry_time"]),
                "age_minutes": age,
                "marketcap": marketcap,
                "liquidity": liquidity,
                "label_status": str(row["label_status"]),
                "tag": int(row["tag"]) if row["tag"] in (0, 1) else None,
                "label_version": str(row["label_version"] or ""),
                "feature_schema_version": str(row["feature_schema_version"] or ""),
                "raw_fact_presence": {
                    name: _recursive_find(raw, aliases) not in (None, "")
                    for name, aliases in {
                        "creator": ("creator_address", "creator", "owner"),
                        "pool": (
                            "biggest_pool_address",
                            "pool_address",
                            "pair_address",
                            "amm_address",
                            "pool_id",
                        ),
                        "buys_1h": ("buys_1h", "buy_1h", "buy_count_1h"),
                        "sells_1h": ("sells_1h", "sell_1h", "sell_count_1h"),
                        "swaps_1h": ("swaps_1h", "swaps1h", "trade_1h", "trades_1h"),
                    }.items()
                },
            }
        )

    comparable = [
        item
        for item in normalized
        if item["marketcap"] is not None
        and item["liquidity"] is not None
        and item["age_minutes"] is not None
    ]
    baseline = [
        item
        for item in comparable
        if float(item["marketcap"]) > MARKETCAP_MIN
        and float(item["liquidity"]) > ORIGINAL_LIQUIDITY_MIN
        and ORIGINAL_AGE_MIN < float(item["age_minutes"]) < ORIGINAL_AGE_MAX
    ]
    age_gt5_only = [
        item
        for item in baseline
        if NEW_AGE_MIN < float(item["age_minutes"]) < ORIGINAL_AGE_MAX
    ]
    age_lt240_only = [
        item
        for item in baseline
        if ORIGINAL_AGE_MIN < float(item["age_minutes"]) < NEW_AGE_MAX
    ]
    age_gt5_lt240_only = [
        item
        for item in baseline
        if NEW_AGE_MIN < float(item["age_minutes"]) < NEW_AGE_MAX
    ]
    liquidity_gt5000_only = [
        item for item in baseline if float(item["liquidity"]) > NEW_LIQUIDITY_MIN
    ]
    deployed = [
        item
        for item in baseline
        if float(item["liquidity"]) > NEW_LIQUIDITY_MIN
        and NEW_AGE_MIN < float(item["age_minutes"]) < NEW_AGE_MAX
    ]

    report = {
        "database": str(DB_PATH.relative_to(PROJECT_ROOT)),
        "policy": {
            "task_cutoff_iso_utc": TASK_CUTOFF_ISO,
            "task_cutoff_entry_time": TASK_CUTOFF_EPOCH,
            "baseline": {
                "marketcap_gt": MARKETCAP_MIN,
                "liquidity_gt": ORIGINAL_LIQUIDITY_MIN,
                "age_minutes_gt": ORIGINAL_AGE_MIN,
                "age_minutes_lt": ORIGINAL_AGE_MAX,
            },
            "deployed": {
                "marketcap_gt": MARKETCAP_MIN,
                "liquidity_gt": NEW_LIQUIDITY_MIN,
                "age_minutes_gt": NEW_AGE_MIN,
                "age_minutes_lt": NEW_AGE_MAX,
            },
            "positive_rate_label_version": CURRENT_LABEL,
        },
        "rows_total": len(rows),
        "rows_comparable": len(comparable),
        "missing_facts": dict(missing),
        "feature_schema_counts": dict(schema_counts),
        "baseline": _summary(baseline),
        "age_gt5_only": _summary(age_gt5_only),
        "age_lt240_only": _summary(age_lt240_only),
        "age_gt5_lt240_only": _summary(age_gt5_lt240_only),
        "liquidity_gt5000_only": _summary(liquidity_gt5000_only),
        "deployed_liquidity_gt5000_age_gt5_lt240": _summary(deployed),
        "deployed_fraction_of_baseline": len(deployed) / len(baseline) if baseline else None,
        "deployed_raw_fact_presence": {
            name: sum(int(item["raw_fact_presence"].get(name, False)) for item in deployed)
            for name in ("creator", "pool", "buys_1h", "sells_1h", "swaps_1h")
        },
        "deployed_sample_ids": [item["id"] for item in deployed],
    }
    target = PROJECT_ROOT / "artifacts" / "research" / "admission_counterfactual_20260905.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(
        json.dumps(
            {
                "baseline": report["baseline"],
                "age_gt5_only": report["age_gt5_only"],
                "age_lt240_only": report["age_lt240_only"],
                "age_gt5_lt240_only": report["age_gt5_lt240_only"],
                "liquidity_gt5000_only": report["liquidity_gt5000_only"],
                "deployed": report["deployed_liquidity_gt5000_age_gt5_lt240"],
                "deployed_fraction_of_baseline": report["deployed_fraction_of_baseline"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
