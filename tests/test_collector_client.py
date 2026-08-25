from __future__ import annotations

import asyncio

import pytest

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
    def __init__(self) -> None:
        self.calls = 0

    async def request(self, method, url, **kwargs):
        self.calls += 1
        return TransportResponse(
            429,
            {"code": 429, "error": "RATE_LIMIT_BANNED", "reset_at": 1_800_000_000},
            {"X-RateLimit-Reset": "1800000000"},
        )


def test_429_cools_only_the_slot_without_poisoning_global_limiter() -> None:
    limiter = FakeLimiter()
    transport = RateLimitedTransport()
    client = GMGNDataClient(
        base_url="https://example.invalid",
        transport=transport,
        limiter=limiter,
    )
    slot = ApiSlot(0, "never-print-me")

    async def run() -> tuple[CollectorRateLimitError, CollectorRateLimitError]:
        with pytest.raises(CollectorRateLimitError) as first:
            await client.request(slot, "/v1/test")
        with pytest.raises(CollectorRateLimitError) as second:
            await client.request(slot, "/v1/test")
        return first.value, second.value

    first, second = asyncio.run(run())
    assert first.reset_at == 1_800_000_000
    assert second.reset_at == 1_800_000_000
    assert limiter.reset_at is None
    assert limiter.acquires == 1
    assert transport.calls == 1
    assert client.slot_rate_limit_remaining(slot) > 0
    assert "never-print-me" not in str(first)


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
