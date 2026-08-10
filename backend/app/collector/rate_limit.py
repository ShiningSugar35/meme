"""Shared per-IP rate limiter and 429 cooldown gate."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class AsyncRateLimiter:
    def __init__(
        self,
        requests_per_second: float = 2.0,
        *,
        cooldown_buffer_seconds: float = 15.0,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.requests_per_second = requests_per_second
        self.cooldown_buffer_seconds = max(0.0, cooldown_buffer_seconds)
        self._wall_clock = wall_clock
        self._monotonic = monotonic_clock
        self._sleep = sleeper
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._pause_until = 0.0

    @property
    def pause_until(self) -> float:
        return self._pause_until

    def note_rate_limit(self, reset_at: int | None) -> None:
        fallback = self._wall_clock() + 300.0
        target = float(reset_at) if reset_at else fallback
        self._pause_until = max(
            self._pause_until,
            target + self.cooldown_buffer_seconds,
        )

    async def acquire(self, weight: float = 1.0) -> None:
        if weight <= 0:
            raise ValueError("request weight must be positive")
        async with self._lock:
            remaining = self._pause_until - self._wall_clock()
            if remaining > 0:
                await self._sleep(remaining)
            now = self._monotonic()
            wait_for_slot = self._next_request_at - now
            if wait_for_slot > 0:
                await self._sleep(wait_for_slot)
            granted_at = self._monotonic()
            self._next_request_at = max(granted_at, self._next_request_at) + (
                weight / self.requests_per_second
            )
