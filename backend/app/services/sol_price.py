from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, Sequence

from ..collector.constants import SOL_WRAPPED_MINT
from ..collector.models import Kline
from ..database import Database, utc_now_iso


class SolKlineProvider(Protocol):
    async def klines(self, address: str, from_ts: int, to_ts: int) -> Sequence[Kline]: ...


@dataclass(frozen=True, slots=True)
class SolUsdPrice:
    price_usd: float
    observed_at: int
    source: str


class SolUsdPriceService:
    """Freeze auditable SOL/USD observations for simulation fee accounting."""

    ASSET = "SOL"
    MAX_AGE_SECONDS = 180
    LOOKBACK_SECONDS = 5 * 60

    def __init__(self, database: Database) -> None:
        self.database = database

    def ingest_klines(self, klines: Sequence[Kline], *, source: str = "gmgn_1m_kline") -> int:
        rows = [
            (self.ASSET, int(line.timestamp), float(line.close), source, utc_now_iso())
            for line in klines
            if line.close is not None and float(line.close) > 0
        ]
        if not rows:
            return 0
        with self.database.transaction(immediate=True) as connection:
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO asset_usd_prices(asset,observed_at,price_usd,source,recorded_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(asset,observed_at) DO UPDATE SET
                        price_usd=excluded.price_usd,
                        source=excluded.source,
                        recorded_at=excluded.recorded_at
                    """,
                    row,
                )
        return len(rows)

    def price_at(
        self,
        occurred_at: datetime | int | float,
        *,
        max_age_seconds: int | None = None,
    ) -> SolUsdPrice | None:
        target = self._epoch(occurred_at)
        max_age = self.MAX_AGE_SECONDS if max_age_seconds is None else max(0, int(max_age_seconds))
        row = self.database.fetch_one(
            """
            SELECT price_usd,observed_at,source
            FROM asset_usd_prices
            WHERE asset=? AND observed_at<=?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (self.ASSET, target),
        )
        if not row:
            return None
        observed_at = int(row["observed_at"])
        if target - observed_at > max_age:
            return None
        price = float(row["price_usd"])
        if price <= 0:
            return None
        return SolUsdPrice(price_usd=price, observed_at=observed_at, source=str(row["source"]))

    async def refresh(
        self,
        provider: SolKlineProvider,
        *,
        now_ts: int | None = None,
    ) -> SolUsdPrice | None:
        target = int(now_ts or datetime.now(timezone.utc).timestamp())
        klines = await provider.klines(
            SOL_WRAPPED_MINT,
            target - self.LOOKBACK_SECONDS,
            target,
        )
        self.ingest_klines(klines)
        price = self.price_at(target)
        self.database.set_runtime_state(
            "sol_usd_price_status",
            {
                "state": "ready" if price else "stale",
                "price_usd": price.price_usd if price else None,
                "observed_at": price.observed_at if price else None,
                "source": price.source if price else None,
                "refreshed_at": utc_now_iso(),
            },
        )
        return price

    @staticmethod
    def _epoch(value: datetime | int | float) -> int:
        if isinstance(value, datetime):
            moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            return int(moment.timestamp())
        return int(value)
