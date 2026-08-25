from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from ..collector.constants import FEATURE_SCHEMA_VERSION, LabelPolicy
from ..database import Database, utc_now_iso


@dataclass(slots=True)
class SampleRecord:
    address: str
    entry_time: int
    entry_price: float
    features: Mapping[str, Any]
    chain: str = "sol"
    name: str | None = None
    symbol: str | None = None
    token_type: str | None = None
    age_minutes: float | None = None
    launchpad: str | None = None
    liquidity: float | None = None
    liquidity_estimated: bool = False
    utility_eligible: bool = True
    holder_count: float | None = None
    price_2h_max_ratio: float | None = None
    price_2h_min_ratio: float | None = None
    price_1h_max_ratio: float | None = None
    price_1h_min_ratio: float | None = None
    final_1h_close_ratio: float | None = None
    label_max_price_ratio: float | None = None
    label_min_price_ratio: float | None = None
    label_final_close_ratio: float | None = None
    label_window_seconds: int | None = None
    final_close_ratio: float | None = None
    first_take_profit_at: int | None = None
    first_stop_loss_at: int | None = None
    exit_reason: str | None = None
    same_bar_conflict: bool = False
    gross_return_rate: float | None = None
    return_source: str | None = None
    tag: int | None = None
    label_status: str = "pending"
    label_version: str = LabelPolicy().label_version
    label_source: str = "collector"
    terminal_return_estimated: bool = False
    feature_schema_version: str = FEATURE_SCHEMA_VERSION
    feature_snapshot_at: int | None = None
    raw: Mapping[str, Any] | None = None

    @property
    def sample_key(self) -> str:
        material = f"{self.chain}|{self.address}|{self.entry_time}".encode()
        return hashlib.sha256(material).hexdigest()


class SampleRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def insert(self, record: SampleRecord) -> bool:
        now = utc_now_iso()
        payload = asdict(record)
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO samples(
                    sample_key, chain, address, name, symbol, token_type, entry_time, age_minutes,
                    launchpad, entry_price, liquidity, liquidity_estimated, utility_eligible, holder_count,
                    features_json, feature_schema_version, feature_snapshot_at,
                    price_2h_max_ratio, price_2h_min_ratio,
                    price_1h_max_ratio, price_1h_min_ratio, final_1h_close_ratio,
                    label_max_price_ratio, label_min_price_ratio, label_final_close_ratio, label_window_seconds,
                    final_close_ratio, first_take_profit_at, first_stop_loss_at, exit_reason, same_bar_conflict,
                    gross_return_rate, return_source,
                    tag, label_status, label_version, label_source, terminal_return_estimated,
                    raw_json, collected_at, updated_at
                ) VALUES(
                    :sample_key, :chain, :address, :name, :symbol, :token_type, :entry_time, :age_minutes,
                    :launchpad, :entry_price, :liquidity, :liquidity_estimated, :utility_eligible, :holder_count,
                    :features_json, :feature_schema_version, :feature_snapshot_at,
                    :price_2h_max_ratio, :price_2h_min_ratio,
                    :price_1h_max_ratio, :price_1h_min_ratio, :final_1h_close_ratio,
                    :label_max_price_ratio, :label_min_price_ratio, :label_final_close_ratio, :label_window_seconds,
                    :final_close_ratio, :first_take_profit_at, :first_stop_loss_at, :exit_reason, :same_bar_conflict,
                    :gross_return_rate, :return_source,
                    :tag, :label_status, :label_version, :label_source, :terminal_return_estimated,
                    :raw_json, :collected_at, :updated_at
                ) ON CONFLICT(sample_key) DO NOTHING
                """,
                {
                    **payload,
                    "sample_key": record.sample_key,
                    "liquidity_estimated": int(record.liquidity_estimated),
                    "utility_eligible": int(record.utility_eligible),
                    "terminal_return_estimated": int(record.terminal_return_estimated),
                    "features_json": json.dumps(record.features, ensure_ascii=False, separators=(",", ":")),
                    "raw_json": json.dumps(record.raw or {}, ensure_ascii=False, separators=(",", ":")),
                    "collected_at": now,
                    "updated_at": now,
                },
            )
            return cursor.rowcount == 1

    def insert_many(self, records: Iterable[SampleRecord]) -> tuple[int, int]:
        inserted = skipped = 0
        for record in records:
            if self.insert(record):
                inserted += 1
            else:
                skipped += 1
        return inserted, skipped

    def update_label(
        self,
        sample_id: int,
        *,
        tag: int,
        max_ratio: float,
        min_ratio: float,
        final_close_ratio: float,
        label_version: str | None = None,
        first_take_profit_at: int | None = None,
        first_stop_loss_at: int | None = None,
        exit_reason: str | None = None,
        same_bar_conflict: bool = False,
    ) -> None:
        if tag not in {0, 1}:
            raise ValueError("tag must be 0 or 1")
        policy = LabelPolicy()
        resolved_label_version = label_version or policy.label_version
        gross_return_rate = (
            policy.take_profit_ratio - 1.0
            if tag == 1
            else policy.stop_loss_ratio - 1.0
        )
        self.database.execute(
            """
            UPDATE samples
            SET tag=?, label_max_price_ratio=?, label_min_price_ratio=?, label_final_close_ratio=?,
                label_window_seconds=?, first_take_profit_at=?, first_stop_loss_at=?,
                exit_reason=?, same_bar_conflict=?, gross_return_rate=?,
                return_source='collector_kline', label_status='mature', label_version=?,
                label_source='collector', terminal_return_estimated=0, updated_at=?
            WHERE id=?
            """,
            (
                tag,
                max_ratio,
                min_ratio,
                final_close_ratio,
                policy.window_seconds,
                first_take_profit_at,
                first_stop_loss_at,
                exit_reason,
                int(same_bar_conflict),
                gross_return_rate,
                resolved_label_version,
                utc_now_iso(),
                sample_id,
            ),
        )

    def list_mature(self, *, since_epoch: int | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM samples WHERE label_status='mature' AND tag IN (0,1) "
            "AND token_type IN ('new_creation','near_completion') AND label_version=? "
            "AND feature_schema_version=?"
        )
        parameters: tuple[Any, ...] = (LabelPolicy().label_version, FEATURE_SCHEMA_VERSION)
        if since_epoch is not None:
            sql += " AND entry_time >= ?"
            parameters = (*parameters, since_epoch)
        sql += " ORDER BY entry_time, id"
        rows = self.database.fetch_all(sql, parameters)
        for row in rows:
            features = json.loads(row.pop("features_json") or "{}")
            row.pop("raw_json", None)
            row.update(features)
        return rows

    def list_pending_due(self, *, before_epoch: int, limit: int = 100) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT id, address, entry_time, entry_price
            FROM samples
            WHERE label_status='pending' AND entry_time <= ?
            ORDER BY entry_time
            LIMIT ?
            """,
            (before_epoch, limit),
        )

    def statistics(self) -> dict[str, Any]:
        totals = self.database.fetch_one(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN label_status='mature' THEN 1 ELSE 0 END) AS mature,
                   SUM(CASE WHEN label_status='pending' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN tag=1 THEN 1 ELSE 0 END) AS positives,
                   MIN(entry_time) AS min_entry_time,
                   MAX(entry_time) AS max_entry_time
            FROM samples
            WHERE feature_schema_version=?
            """,
            (FEATURE_SCHEMA_VERSION,),
        ) or {}
        platforms = self.database.fetch_all(
            "SELECT COALESCE(launchpad,'unknown') AS launchpad, COUNT(*) AS count FROM samples WHERE feature_schema_version=? GROUP BY launchpad ORDER BY count DESC",
            (FEATURE_SCHEMA_VERSION,),
        )
        return {**totals, "launchpads": platforms}
