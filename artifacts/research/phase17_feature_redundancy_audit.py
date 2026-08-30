from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "meme_quant.db"
FEATURE_SCHEMA_VERSION = "event1m_regime_v3"
LABEL_VERSION = "sl090_tp180_m90_binary_v5"


def finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def main() -> None:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT entry_price,features_json
            FROM samples
            WHERE feature_schema_version=? AND label_version=?
              AND label_status='mature' AND tag IN (0,1)
              AND token_type IN ('new_creation','near_completion')
            ORDER BY entry_time,id
            """,
            (FEATURE_SCHEMA_VERSION, LABEL_VERSION),
        )
    ]
    split = int(math.floor(len(rows) * 0.80))
    records: list[dict[str, float]] = []
    for row in rows[:split]:
        try:
            source = json.loads(row.get("features_json") or "{}")
        except json.JSONDecodeError:
            source = {}
        price = finite(row.get("entry_price"))
        source["ln(price+1)"] = math.log1p(price) if price is not None and price >= 0 else None
        records.append({
            key: value
            for key, raw in source.items()
            if key != "ln(liquidity_usd)" and (value := finite(raw)) is not None
        })
    frame = pd.DataFrame.from_records(records)
    numeric = frame.apply(pd.to_numeric, errors="coerce")
    correlation = numeric.corr(method="spearman")
    pairs: list[dict[str, float | str]] = []
    columns = list(correlation.columns)
    for i, left in enumerate(columns):
        for right in columns[i + 1:]:
            value = correlation.loc[left, right]
            if pd.notna(value) and abs(float(value)) >= 0.95:
                overlap = int(numeric[[left, right]].dropna().shape[0])
                pairs.append({"left": left, "right": right, "spearman": float(value), "overlap": overlap})
    pairs.sort(key=lambda item: -abs(float(item["spearman"])))
    constants = []
    for column in numeric.columns:
        values = numeric[column].dropna()
        unique = int(values.nunique())
        if unique <= 3:
            constants.append({
                "feature": column,
                "unique_values": unique,
                "coverage": float(values.shape[0] / max(len(numeric), 1)),
                "values": sorted(float(value) for value in values.unique())[:10],
            })
    print(json.dumps({
        "development_rows": split,
        "reserved_latest_rows": len(rows) - split,
        "high_spearman_pairs_abs_ge_0_95": pairs,
        "near_constant_features_unique_le_3": constants,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
