from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx


class DexScreenerFallbackProvider:
    """Low-rate, no-auth fallback for aggregate paid-attention indicators.

    GMGN remains the production primary.  This provider is never used for
    per-token admission/enrichment and is invoked only when the GMGN aggregate
    feed is stale/unavailable or its DEX-promotion subfamily is missing. Results
    are cached so a GMGN
    outage cannot turn DEX Screener Public API into a new high-frequency
    dependency.
    """

    BASE_URL = "https://api.dexscreener.com"

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
        self._cached: dict[str, Any] | None = None
        self._last_refresh_monotonic = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def _items(payload: Any) -> list[Mapping[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        if isinstance(payload, Mapping):
            return [payload]
        return []

    async def _get(self, path: str) -> list[Mapping[str, Any]]:
        response = await self._client.get(path)
        response.raise_for_status()
        return self._items(response.json())

    @staticmethod
    def _sum_nonnegative(items: list[Mapping[str, Any]], key: str) -> float:
        total = 0.0
        for item in items:
            try:
                total += max(0.0, float(item.get(key) or 0.0))
            except (TypeError, ValueError):
                continue
        return total

    async def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        if (
            not force
            and self._cached is not None
            and time.monotonic() - self._last_refresh_monotonic < self.min_refresh_seconds
        ):
            return dict(self._cached)

        async with self._lock:
            if (
                not force
                and self._cached is not None
                and time.monotonic() - self._last_refresh_monotonic < self.min_refresh_seconds
            ):
                return dict(self._cached)

            ads_result, boosts_result = await asyncio.gather(
                self._get("/ads/latest/v1"),
                self._get("/token-boosts/latest/v1"),
                return_exceptions=True,
            )
            errors: list[str] = []
            ads_ok = not isinstance(ads_result, BaseException)
            boosts_ok = not isinstance(boosts_result, BaseException)
            ads: list[Mapping[str, Any]] = []
            boosts: list[Mapping[str, Any]] = []
            if not ads_ok:
                errors.append(f"ads:{type(ads_result).__name__}")
            else:
                ads = [
                    item
                    for item in ads_result
                    if str(item.get("chainId") or "").lower() == "solana"
                ]
            if not boosts_ok:
                errors.append(f"boosts:{type(boosts_result).__name__}")
            else:
                boosts = [
                    item
                    for item in boosts_result
                    if str(item.get("chainId") or "").lower() == "solana"
                ]

            payload = {
                "available": bool(ads_ok or boosts_ok),
                "source": "dexscreener_public_fallback",
                "observed_at": int(time.time()),
                "coverage": (int(ads_ok) + int(boosts_ok)) / 2.0,
                # A failed endpoint is missing, not zero.  A successful empty
                # response is a genuine observed zero for this snapshot.
                "dexscreener_sol_ads_latest_count": float(len(ads)) if ads_ok else None,
                "dexscreener_sol_ad_impressions_latest": (
                    self._sum_nonnegative(ads, "impressions") if ads_ok else None
                ),
                "dexscreener_sol_boosts_latest_count": float(len(boosts)) if boosts_ok else None,
                "dexscreener_sol_boost_amount_latest": (
                    self._sum_nonnegative(boosts, "amount") if boosts_ok else None
                ),
                "errors": errors,
            }
            self._cached = payload
            self._last_refresh_monotonic = time.monotonic()
            return dict(payload)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
