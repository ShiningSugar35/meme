from __future__ import annotations

import csv
import math
from pathlib import Path

from backend.app.database import Database
from backend.app.services.csv_importer import CsvImporter


def test_csv_import_applies_current_top10_and_volume_per_swap_thresholds(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    path = tmp_path / "legacy.csv"
    fields = [
        "address",
        "type",
        "time",
        "age",
        "price",
        "price_2h_max/price",
        "price_2h_min/price",
        "top_10_holder_rate",
        "volume_1h/swaps_1h",
        "tag",
    ]
    base = {
        "type": "new_creation",
        "time": "1700000000",
        "age": str(math.log(10.0)),
        "price": "0.1",
        "price_2h_max/price": "1.30",
        "price_2h_min/price": "0.91",
        "tag": "0",
    }
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({**base, "address": "accepted", "top_10_holder_rate": "0.28", "volume_1h/swaps_1h": str(math.log(31.01))})
        writer.writerow({**base, "address": "top-too-high", "top_10_holder_rate": "0.280001", "volume_1h/swaps_1h": str(math.log(100.0))})
        writer.writerow({**base, "address": "vps-boundary", "top_10_holder_rate": "0.2", "volume_1h/swaps_1h": str(math.log(31.0))})

    summary = CsvImporter(database).import_file(path)

    assert summary.inserted_rows == 1
    rows = database.fetch_all("SELECT address FROM samples ORDER BY address")
    assert [row["address"] for row in rows] == ["accepted"]
