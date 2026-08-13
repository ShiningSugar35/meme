from __future__ import annotations

import csv
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..collector.constants import FilterThresholds, LabelPolicy
from ..database import Database
from ..repositories.samples import SampleRecord, SampleRepository


IDENTITY_COLUMNS = {"address", "name", "symbol", "type", "time", "price"}
FUTURE_COLUMNS = {"price_2h_max/price", "price_2h_min/price", "tag"}
EXCLUDED_MODEL_COLUMNS = IDENTITY_COLUMNS | FUTURE_COLUMNS
CURRENT_LABEL_VERSION = LabelPolicy().label_version
FILTER_THRESHOLDS = FilterThresholds()


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

                token_type = str(row.get("type") or "").strip()
                if token_type == "completed":
                    continue

                age_feature = _float(row.get("age"))
                min_log_age = math.log(FILTER_THRESHOLDS.min_age_minutes)
                max_log_age = math.log(FILTER_THRESHOLDS.max_age_minutes_exclusive)
                if age_feature is None or not min_log_age < age_feature < max_log_age:
                    try:
                        age_minutes = math.exp(age_feature) if age_feature is not None else None
                    except OverflowError:
                        age_minutes = None
                    self.database.audit(
                        category="data_import",
                        action="age_out_of_range_csv_row",
                        severity="info",
                        details={"row_number": row_number, "age_minutes": age_minutes},
                    )
                    continue

                raw_tag = _int(row.get("tag"))
                max_ratio = _float(row.get("price_2h_max/price"))
                min_ratio = _float(row.get("price_2h_min/price"))
                tag = raw_tag if raw_tag in {0, 1, 2} else None
                terminal_estimated = False
                final_close_ratio = None
                if tag == 1 and (max_ratio is None or max_ratio < 1.6):
                    # The old script also called a >1.25x two-hour close positive.
                    # Under the binary v3 policy, every no-TP timeout is negative.
                    tag = 0
                    final_close_ratio = 1.25
                    terminal_estimated = True
                    legacy_terminal += 1
                elif tag == 2:
                    # Existing intermediate migrations used tag=2 for timeout-only
                    # positives. Binary v3 folds that class into the negative class.
                    tag = 0
                    final_close_ratio = 1.20
                    terminal_estimated = True

                # Shrinking H2 -> H1 is monotonic for negatives: an H2 negative
                # cannot become an H1 positive. H2 positives, however, may have
                # first touched 1.6x only in the second hour, so they must be
                # re-fetched before becoming a mature H1 label.
                status = "mature" if tag == 0 and max_ratio is not None and min_ratio is not None else "pending"
                if status == "pending":
                    tag = None
                mature += int(status == "mature")
                pending += int(status == "pending")
                gross_return_rate = -0.10 if status == "mature" else None
                return_source = "legacy_h2_monotonic_negative" if status == "mature" else None
                exit_reason = "h1_negative_inferred_from_h2_negative" if status == "mature" else None
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
                        token_type=token_type or None,
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
                        label_version=CURRENT_LABEL_VERSION,
                        label_source=(
                            "legacy_h2_monotonic_negative"
                            if status == "mature"
                            else "legacy_h2_requires_h1_refetch"
                        ),
                        terminal_return_estimated=terminal_estimated,
                        raw=row,
                    )
                )

        inserted, skipped = self.samples.insert_many(records)
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
