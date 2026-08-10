from __future__ import annotations

import csv
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..database import Database
from ..repositories.samples import SampleRecord, SampleRepository


IDENTITY_COLUMNS = {"address", "name", "symbol", "type", "time", "price"}
FUTURE_COLUMNS = {"price_2h_max/price", "price_2h_min/price", "tag"}
EXCLUDED_MODEL_COLUMNS = IDENTITY_COLUMNS | FUTURE_COLUMNS
LEGACY_LABEL_VERSION = "sl090_tp160_h2_close125_legacy"


@dataclass(slots=True)
class ImportSummary:
    source: str
    source_sha256: str
    total_rows: int
    inserted_rows: int
    skipped_rows: int
    mature_rows: int
    pending_rows: int
    legacy_terminal_rows: int


def _float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _typed(value: Any) -> Any:
    text = str(value or "").strip()
    if text == "":
        return None
    number = _float(text)
    return number if number is not None else text


class CsvImporter:
    """Idempotently imports the legacy 40-column collector CSV into SQLite."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.samples = SampleRepository(database)

    def import_file(self, path: Path, *, force: bool = False) -> ImportSummary:
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        state = self.database.get_runtime_state("initial_csv_import", {})
        if not force and state.get("sha256") == source_hash:
            self._backfill_legacy_label_facts()
            return ImportSummary(str(path), source_hash, state.get("total_rows", 0), 0, state.get("total_rows", 0), state.get("mature_rows", 0), state.get("pending_rows", 0), state.get("legacy_terminal_rows", 0))

        records: list[SampleRecord] = []
        mature = pending = legacy_terminal = 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"address", "time", "price", "tag"}
            missing = required.difference(reader.fieldnames or [])
            if missing:
                raise ValueError(f"CSV missing required columns: {sorted(missing)}")
            for row_number, row in enumerate(reader, start=2):
                entry_time = _int(row.get("time"))
                entry_price = _float(row.get("price"))
                address = str(row.get("address") or "").strip()
                if not address or entry_time is None or entry_price is None or entry_price <= 0:
                    self.database.audit(
                        category="data_import",
                        action="invalid_csv_row",
                        severity="warning",
                        details={"row_number": row_number, "reason": "missing identity/time/price"},
                    )
                    continue

                raw_tag = _int(row.get("tag"))
                max_ratio = _float(row.get("price_2h_max/price"))
                min_ratio = _float(row.get("price_2h_min/price"))
                tag = raw_tag if raw_tag in {0, 1, 2} else None
                terminal_estimated = False
                final_close_ratio = None
                if tag == 1 and (max_ratio is None or max_ratio < 1.6):
                    tag = 2
                    # Legacy rows were only tagged positive when their final close exceeded 1.25x.
                    # The exact close is unavailable, so 1.25x is a conservative known floor.
                    final_close_ratio = 1.25
                    terminal_estimated = True
                    legacy_terminal += 1
                elif tag == 2:
                    final_close_ratio = 1.20
                    terminal_estimated = True

                status = "mature" if tag is not None and max_ratio is not None and min_ratio is not None else "pending"
                mature += int(status == "mature")
                pending += int(status == "pending")
                gross_return_rate = None
                return_source = None
                exit_reason = None
                if status == "mature":
                    if tag == 1:
                        gross_return_rate = 0.60
                        return_source = "legacy_label_rule"
                        exit_reason = "legacy_take_profit"
                    elif tag == 2:
                        gross_return_rate = 0.25
                        return_source = "legacy_floor"
                        exit_reason = "legacy_timeout_positive"
                    else:
                        gross_return_rate = -0.10
                        return_source = "legacy_label_rule"
                        exit_reason = "legacy_negative"
                features = {
                    key: _typed(value)
                    for key, value in row.items()
                    if key not in EXCLUDED_MODEL_COLUMNS
                }
                records.append(
                    SampleRecord(
                        address=address,
                        name=str(row.get("name") or "").strip() or None,
                        symbol=str(row.get("symbol") or "").strip() or None,
                        token_type=str(row.get("type") or "").strip() or None,
                        entry_time=entry_time,
                        age_minutes=_float(row.get("age")),
                        launchpad=str(row.get("launchpad") or "").strip() or None,
                        entry_price=entry_price,
                        liquidity=None,
                        liquidity_estimated=True,
                        utility_eligible=False,
                        features=features,
                        price_2h_max_ratio=max_ratio,
                        price_2h_min_ratio=min_ratio,
                        final_close_ratio=final_close_ratio,
                        exit_reason=exit_reason,
                        gross_return_rate=gross_return_rate,
                        return_source=return_source,
                        tag=tag,
                        label_status=status,
                        label_version=LEGACY_LABEL_VERSION,
                        label_source="legacy_csv_migration",
                        terminal_return_estimated=terminal_estimated,
                        raw=row,
                    )
                )

        inserted, skipped = self.samples.insert_many(records)
        self._backfill_legacy_label_facts()
        summary = ImportSummary(
            source=str(path),
            source_sha256=source_hash,
            total_rows=len(records),
            inserted_rows=inserted,
            skipped_rows=skipped,
            mature_rows=mature,
            pending_rows=pending,
            legacy_terminal_rows=legacy_terminal,
        )
        self.database.set_runtime_state(
            "initial_csv_import",
            {
                "sha256": source_hash,
                "total_rows": len(records),
                "mature_rows": mature,
                "pending_rows": pending,
                "legacy_terminal_rows": legacy_terminal,
            },
        )
        self.database.audit(
            category="data_import",
            action="csv_import_completed",
            details={
                "sha256": source_hash,
                "total": len(records),
                "inserted": inserted,
                "skipped": skipped,
                "legacy_terminal_rows": legacy_terminal,
            },
        )
        return summary

    def _backfill_legacy_label_facts(self) -> None:
        """Fill v2 label-fact columns for already imported legacy rows."""
        self.database.execute(
            """
            UPDATE samples
            SET gross_return_rate = CASE
                    WHEN tag=1 THEN 0.60
                    WHEN tag=2 THEN 0.25
                    WHEN tag=0 THEN -0.10
                    ELSE gross_return_rate
                END,
                return_source = CASE
                    WHEN tag=2 THEN 'legacy_floor'
                    WHEN tag IN (0,1) THEN 'legacy_label_rule'
                    ELSE return_source
                END,
                exit_reason = CASE
                    WHEN tag=1 THEN 'legacy_take_profit'
                    WHEN tag=2 THEN 'legacy_timeout_positive'
                    WHEN tag=0 THEN 'legacy_negative'
                    ELSE exit_reason
                END
            WHERE label_source='legacy_csv_migration'
              AND label_status='mature'
              AND (gross_return_rate IS NULL OR return_source IS NULL OR exit_reason IS NULL)
            """
        )
