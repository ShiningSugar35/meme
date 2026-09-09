from __future__ import annotations

import csv
import io
import json
from typing import Any

from ..collector.constants import LabelPolicy
from ..database import Database
from ..ml.features import AGE_LOG1P_FEATURE, PRICE_LOG1P_FEATURE, materialize_entry_feature


class SampleExportService:
    """Export the complete mature/tagged sample registry as a training CSV."""

    BASE_COLUMNS = (
        "address",
        "name",
        "symbol",
        "type",
        "time",
        "launchpad",
        "price",
        "liquidity",
        "holder_count",
    )
    LABEL_COLUMNS = (
        "price_1h_max/price",
        "price_1h_min/price",
        "final_1h_close_ratio",
        "tag",
        "label_version",
        "label_source",
    )

    def __init__(self, database: Database) -> None:
        self.database = database

    def render_csv(self) -> tuple[str, int]:
        rows = self.database.fetch_all(
            """
            SELECT * FROM samples
            WHERE label_status='mature' AND tag IN (0,1)
              AND token_type IN ('new_creation','trending')
              AND label_version=?
            ORDER BY entry_time, id
            """,
            (LabelPolicy().label_version,),
        )
        feature_names: set[str] = set()
        decoded: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for row in rows:
            try:
                features = json.loads(row.get("features_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                features = {}
            if not isinstance(features, dict):
                features = {}
            canonical_features = {
                str(name): value for name, value in features.items() if str(name) != "age"
            }
            for name in (AGE_LOG1P_FEATURE, PRICE_LOG1P_FEATURE):
                value = materialize_entry_feature(
                    name,
                    features,
                    entry_price=row.get("entry_price"),
                )
                if value is not None:
                    canonical_features[name] = value
            feature_names.update(canonical_features)
            decoded.append((row, canonical_features))

        ordered_features = sorted(feature_names)
        fieldnames = [*self.BASE_COLUMNS, *ordered_features, *self.LABEL_COLUMNS]
        handle = io.StringIO(newline="")
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row, features in decoded:
            record: dict[str, Any] = {
                "address": row.get("address"),
                "name": row.get("name"),
                "symbol": row.get("symbol"),
                "type": row.get("token_type"),
                "time": row.get("entry_time"),
                "launchpad": row.get("launchpad"),
                "price": row.get("entry_price"),
                "liquidity": row.get("liquidity"),
                "holder_count": row.get("holder_count"),
                "price_1h_max/price": row.get("price_1h_max_ratio"),
                "price_1h_min/price": row.get("price_1h_min_ratio"),
                "final_1h_close_ratio": row.get("final_1h_close_ratio"),
                "tag": row.get("tag"),
                "label_version": row.get("label_version"),
                "label_source": row.get("label_source"),
            }
            record.update(features)
            writer.writerow(record)
        return handle.getvalue(), len(rows)
