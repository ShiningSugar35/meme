from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, Sequence

import httpx

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




class CoinbaseSolKlineProvider:
    """Unauthenticated, read-only SOL/USD fallback for fee accounting.

    Coinbase Exchange candle timestamps are bucket-open times. Returned Klines
    are timestamped at bucket close and only include fully closed one-minute
    candles so fee conversion never consumes a forming/future candle.
    """

    BASE_URL = "https://api.exchange.coinbase.com"

    def __init__(self, client: httpx.AsyncClient | None = None, *, timeout_seconds: float = 3.0) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=timeout_seconds,
            headers={"Accept": "application/json", "User-Agent": "meme-quant-sol-fee/1"},
        )
        self._owns_client = client is None

    @staticmethod
    def _iso(epoch: int) -> str:
        return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat().replace("+00:00", "Z")

    async def klines(self, address: str, from_ts: int, to_ts: int) -> Sequence[Kline]:
        if address != SOL_WRAPPED_MINT:
            return []
        response = await self._client.get(
            "/products/SOL-USD/candles",
            params={
                "granularity": "60",
                "start": self._iso(max(0, int(from_ts) - 60)),
                "end": self._iso(int(to_ts)),
            },
        )
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, list):
            return []
        result: list[Kline] = []
        for row in payload:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            try:
                opened_at = int(float(row[0]))
                low = float(row[1])
                high = float(row[2])
                open_price = float(row[3])
                close = float(row[4])
                volume = float(row[5])
            except (TypeError, ValueError, OverflowError):
                continue
            closed_at = opened_at + 60
            if closed_at > int(to_ts) or closed_at < int(from_ts):
                continue
            if close <= 0:
                continue
            result.append(
                Kline(
                    timestamp=closed_at,
                    high=high if high > 0 else None,
                    low=low if low > 0 else None,
                    close=close,
                    open=open_price if open_price > 0 else None,
                    volume=volume if volume >= 0 else None,
                )
            )
        return sorted(result, key=lambda item: item.timestamp)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


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
        source: str = "gmgn_1m_kline",
    ) -> SolUsdPrice | None:
        target = int(now_ts or datetime.now(timezone.utc).timestamp())
        klines = await provider.klines(
            SOL_WRAPPED_MINT,
            target - self.LOOKBACK_SECONDS,
            target,
        )
        self.ingest_klines(klines, source=source)
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
