from __future__ import annotations

import csv
import io
import json
from typing import Any

from ..database import Database


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
        "price_2h_max/price",
        "price_2h_min/price",
        "final_close_ratio",
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
            WHERE label_status='mature' AND tag IS NOT NULL
            ORDER BY entry_time, id
            """
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
            feature_names.update(str(name) for name in features)
            decoded.append((row, features))

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
                "price_2h_max/price": row.get("price_2h_max_ratio"),
                "price_2h_min/price": row.get("price_2h_min_ratio"),
                "final_close_ratio": row.get("final_close_ratio"),
                "tag": row.get("tag"),
                "label_version": row.get("label_version"),
                "label_source": row.get("label_source"),
            }
            record.update(features)
            writer.writerow(record)
        return handle.getvalue(), len(rows)
