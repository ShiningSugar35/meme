from __future__ import annotations

import asyncio

from backend.app.collector.client import GMGNDataClient
from backend.app.collector.errors import CollectorRateLimitError
from backend.app.collector.models import ApiSlot, TransportResponse
from backend.app.collector.rate_limit import AsyncRateLimiter


class FakeLimiter:
    def __init__(self) -> None:
        self.acquires = 0
        self.reset_at = None

    async def acquire(self, weight=1) -> None:
        self.acquires += 1

    def note_rate_limit(self, reset_at) -> None:
        self.reset_at = reset_at


class RateLimitedTransport:
    async def request(self, method, url, **kwargs):
        return TransportResponse(
            429,
            {"code": 429, "error": "RATE_LIMIT_BANNED", "reset_at": 1_800_000_000},
            {"X-RateLimit-Reset": "1800000000"},
        )


def test_429_records_reset_at_without_exposing_key() -> None:
    limiter = FakeLimiter()
    client = GMGNDataClient(
        base_url="https://example.invalid",
        transport=RateLimitedTransport(),
        limiter=limiter,
    )

    async def run() -> CollectorRateLimitError:
        try:
            await client.request(ApiSlot(0, "never-print-me"), "/v1/test")
        except CollectorRateLimitError as exc:
            return exc
        raise AssertionError("expected rate limit")

    error = asyncio.run(run())
    assert error.reset_at == 1_800_000_000
    assert limiter.reset_at == 1_800_000_000
    assert "never-print-me" not in str(error)


def test_weighted_limiter_reserves_capacity_after_each_request() -> None:
    wall = [1_000.0]
    mono = [50.0]
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        wall[0] += seconds
        mono[0] += seconds

    limiter = AsyncRateLimiter(
        2.0,
        cooldown_buffer_seconds=0,
        wall_clock=lambda: wall[0],
        monotonic_clock=lambda: mono[0],
        sleeper=sleep,
    )

    async def run() -> None:
        await limiter.acquire(weight=3)
        await limiter.acquire(weight=1)

    asyncio.run(run())
    assert waits == [0, 0.5, 0, 0.5, 0.5]
    assert sum(waits) == 1.5
