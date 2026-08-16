from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class PublicMarketSnapshot:
    observed_at: int
    features: Mapping[str, float | None]
    btc_available: bool
    sol_available: bool
    errors: tuple[str, ...] = ()
    source: str = "coinbase_exchange_public"

    @property
    def available(self) -> bool:
        return self.btc_available or self.sol_available


@dataclass(frozen=True, slots=True)
class _Candle:
    opened_at: int
    close: float

    @property
    def closed_at(self) -> int:
        return self.opened_at + 60


class CoinbasePublicMarketProvider:
    """Low-frequency public crypto-market context from Coinbase Exchange.

    This is deliberately *not* a trading/account client.  The provider only calls
    the unauthenticated Exchange product-candles endpoint and caches the result so
    the one-minute Regime worker does not poll historical candles every minute.
    """

    BASE_URL = "https://api.exchange.coinbase.com"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        timeout_seconds: float = 5.0,
        min_refresh_seconds: int = 300,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=timeout_seconds,
            headers={"Accept": "application/json", "User-Agent": "meme-quant-regime/1"},
        )
        self._owns_client = client is None
        self.min_refresh_seconds = max(60, int(min_refresh_seconds))
        self._cached: PublicMarketSnapshot | None = None
        self._last_refresh_monotonic = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def _iso(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_candles(payload: Any, observed_at: int) -> list[_Candle]:
        if not isinstance(payload, list):
            return []
        result: list[_Candle] = []
        for row in payload:
            if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 5:
                continue
            try:
                opened_at = int(float(row[0]))
                close = float(row[4])
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(close) or close <= 0:
                continue
            # Coinbase timestamps are bucket-open times.  Never consume the
            # currently forming minute or any bucket after this PIT snapshot.
            if opened_at + 60 > int(observed_at):
                continue
            result.append(_Candle(opened_at=opened_at, close=close))
        return sorted({item.opened_at: item for item in result}.values(), key=lambda item: item.opened_at)

    async def _candles(self, product_id: str, observed_at: int) -> list[_Candle]:
        response = await self._client.get(
            f"/products/{product_id}/candles",
            params={
                "granularity": "60",
                "start": self._iso(observed_at - 70 * 60),
                "end": self._iso(observed_at),
            },
        )
        response.raise_for_status()
        return self._parse_candles(response.json(), observed_at)

    @staticmethod
    def _close_at_or_before(candles: Sequence[_Candle], target_at: int, *, max_lag_seconds: int = 120) -> float | None:
        eligible = [item for item in candles if item.closed_at <= int(target_at)]
        if not eligible:
            return None
        item = eligible[-1]
        if int(target_at) - item.closed_at > max_lag_seconds:
            return None
        return item.close

    @classmethod
    def _features_for(cls, prefix: str, candles: Sequence[_Candle], observed_at: int) -> dict[str, float | None]:
        latest = cls._close_at_or_before(candles, observed_at)
        result: dict[str, float | None] = {}
        for minutes in (5, 15, 60):
            past = cls._close_at_or_before(candles, observed_at - minutes * 60)
            result[f"{prefix}_return_{minutes}m"] = latest / past - 1.0 if latest and past else None

        recent = [item.close for item in candles if observed_at - 15 * 60 <= item.closed_at <= observed_at]
        changes = [math.log(b / a) for a, b in zip(recent, recent[1:]) if a > 0 and b > 0]
        result[f"{prefix}_realized_vol_15m"] = statistics.pstdev(changes) if len(changes) >= 2 else None
        return result

    async def snapshot(self, *, observed_at: int | None = None, include_sol: bool = True, force: bool = False) -> PublicMarketSnapshot:
        now = int(observed_at or time.time())
        if (
            not force
            and self._cached is not None
            and time.monotonic() - self._last_refresh_monotonic < self.min_refresh_seconds
        ):
            return self._cached

        async with self._lock:
            if (
                not force
                and self._cached is not None
                and time.monotonic() - self._last_refresh_monotonic < self.min_refresh_seconds
            ):
                return self._cached

            products = ["BTC-USD"] + (["SOL-USD"] if include_sol else [])
            results = await asyncio.gather(
                *(self._candles(product, now) for product in products),
                return_exceptions=True,
            )
            features: dict[str, float | None] = {
                "btc_return_5m": None,
                "btc_return_15m": None,
                "btc_return_60m": None,
                "btc_realized_vol_15m": None,
                "coinbase_sol_return_5m": None,
                "coinbase_sol_return_15m": None,
                "coinbase_sol_return_60m": None,
                "coinbase_sol_realized_vol_15m": None,
            }
            errors: list[str] = []
            btc_available = False
            sol_available = False
            for product, result in zip(products, results, strict=True):
                if isinstance(result, BaseException):
                    errors.append(f"{product}:{type(result).__name__}")
                    continue
                prefix = "btc" if product == "BTC-USD" else "coinbase_sol"
                derived = self._features_for(prefix, result, now)
                features.update(derived)
                available = any(value is not None for value in derived.values())
                if product == "BTC-USD":
                    btc_available = available
                else:
                    sol_available = available

            snapshot = PublicMarketSnapshot(
                observed_at=now,
                features=features,
                btc_available=btc_available,
                sol_available=sol_available,
                errors=tuple(errors),
            )
            self._cached = snapshot
            self._last_refresh_monotonic = time.monotonic()
            return snapshot

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
