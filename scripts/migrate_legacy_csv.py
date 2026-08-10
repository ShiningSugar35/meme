from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import Database
from backend.app.services.csv_importer import CsvImporter


def main() -> int:
    csv_path = PROJECT_ROOT / "meme数据.csv"
    database = Database(PROJECT_ROOT / "data" / "meme_quant.db")
    database.initialize()
    if not csv_path.exists():
        print("legacy CSV not found; schema migration completed")
        return 0
    summary = CsvImporter(database).import_file(csv_path)
    counts = database.fetch_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN label_status='mature' THEN 1 ELSE 0 END) AS mature,
               SUM(CASE WHEN label_status='pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN tag=0 THEN 1 ELSE 0 END) AS tag0,
               SUM(CASE WHEN tag=1 THEN 1 ELSE 0 END) AS tag1,
               SUM(CASE WHEN tag=2 THEN 1 ELSE 0 END) AS tag2
        FROM samples
        """
    ) or {}
    schema = database.fetch_one(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ) or {}
    print(f"schema_version={schema.get('value')}")
    print(
        "legacy_import "
        f"total={summary.total_rows} inserted={summary.inserted_rows} "
        f"skipped={summary.skipped_rows} legacy_tag2={summary.legacy_terminal_rows}"
    )
    print(
        "database "
        f"total={counts.get('total', 0)} mature={counts.get('mature', 0)} "
        f"pending={counts.get('pending', 0)} tag0={counts.get('tag0', 0)} "
        f"tag1={counts.get('tag1', 0)} tag2={counts.get('tag2', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
