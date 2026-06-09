"""Weight-aware credential rate limiter with 429 cooldown support."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..config import settings
from ..logging_config import logger


ENDPOINT_WEIGHTS: Dict[str, int] = {
    "/v1/trenches": 3,
    "/api/v1/trenches": 3,
    "/v1/market/token_kline": 2,
    "/v1/token/info": 1,
    "/v1/token/security": 1,
    "/v1/token/pool_info": 1,
    "/v1/market/token_top_holders": 5,
    "/v1/market/token_top_traders": 5,
}
DEFAULT_WEIGHT = 1


def _endpoint_weight(path: str) -> int:
    clean = path.split("?")[0].rstrip("/")
    for ep, w in ENDPOINT_WEIGHTS.items():
        if clean.endswith(ep.rstrip("/")) or ep.rstrip("/") in clean:
            return w
    return DEFAULT_WEIGHT


@dataclass
class CredentialSlot:
    slot: int
    role: str = "feature"
    cooldown_until: float = 0.0
    total_calls: int = 0
    total_weight: int = 0
    ok_calls: int = 0
    failed_calls: int = 0
    rate_limited_count: int = 0
    endpoints: Dict[str, int] = field(default_factory=dict)
    consecutive_failures: int = 0
    consecutive_non_network_failures: int = 0
    recent_failures: deque = field(default_factory=lambda: deque(maxlen=50))
    disabled_until: float = 0.0
    disabled_reason: str = ""
    disable_count: int = 0
    last_failure_at: float = 0.0
    last_success_at: float = 0.0

    def is_cooldown(self) -> bool:
        return time.monotonic() < self.cooldown_until

    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.monotonic())

    def is_available(self) -> bool:
        return not self.is_cooldown() and not self.is_disabled()

    def is_disabled(self) -> bool:
        return time.monotonic() < self.disabled_until

    def disabled_remaining(self) -> float:
        return max(0.0, self.disabled_until - time.monotonic())

    def cooldown_or_disabled_remaining(self) -> float:
        return max(self.cooldown_remaining(), self.disabled_remaining())


@dataclass
class EndpointCooldown:
    cooldown_until: float = 0.0

    def is_cooldown(self) -> bool:
        return time.monotonic() < self.cooldown_until


class RateLimiter:
    def __init__(self, credential_count: int = 12):
        self._lock = asyncio.Lock()
        self.slots: Dict[int, CredentialSlot] = {}
        self._endpoint_cooldowns: Dict[str, EndpointCooldown] = {}
        self.default_cooldown_s = float(getattr(settings, "GMGN_RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS", 300) or 300)

        discovery_slots = set(settings.get_discovery_slots())
        holding_slots = set(settings.get_holding_slots())

        for i in range(credential_count):
            if i in holding_slots:
                role = "holding_poll"
            elif i in discovery_slots:
                role = "discovery"
            else:
                role = "unassigned"
            self.slots[i] = CredentialSlot(slot=i, role=role)

        self._slot_buckets: Dict[int, float] = {}
        self._slot_last_refill: Dict[int, float] = {}
        self._bucket_capacity = float(getattr(settings, 'GMGN_RATE_LIMIT_BUCKET_CAPACITY', None) or 20)
        self._bucket_refill_rate = float(getattr(settings, 'GMGN_RATE_LIMIT_REFILL_WEIGHT_PER_SECOND', None) or 20)
        self._bucket_wait_max_s = float(getattr(settings, 'GMGN_RATE_LIMIT_BUCKET_WAIT_MAX_SECONDS', None) or 5.0)

    def _endpoint_key(self, path: str) -> str:
        return path.split("?")[0].rstrip("/")

    async def acquire(self, slot: int, path: str) -> bool:
        weight = _endpoint_weight(path)
        deadline = time.monotonic() + self._bucket_wait_max_s

        while True:
            async with self._lock:
                cred = self.slots.get(slot)
                if cred is None:
                    return True

                if not cred.is_available():
                    return False

                ep_key = self._endpoint_key(path)
                if ep_key in self._endpoint_cooldowns and self._endpoint_cooldowns[ep_key].is_cooldown():
                    return False

                now = time.monotonic()
                if slot not in self._slot_buckets:
                    self._slot_buckets[slot] = self._bucket_capacity
                    self._slot_last_refill[slot] = now

                elapsed = now - self._slot_last_refill.get(slot, now)
                self._slot_buckets[slot] = min(self._bucket_capacity, self._slot_buckets.get(slot, 0) + elapsed * self._bucket_refill_rate)
                self._slot_last_refill[slot] = now

                if self._slot_buckets[slot] >= weight:
                    self._slot_buckets[slot] -= weight
                    cred.total_calls += 1
                    cred.total_weight += weight
                    cred.endpoints[ep_key] = cred.endpoints.get(ep_key, 0) + 1
                    return True

                remaining = (weight - self._slot_buckets[slot]) / self._bucket_refill_rate
                remaining = min(remaining, self._bucket_wait_max_s)

            if time.monotonic() + remaining > deadline:
                return False
            await asyncio.sleep(min(remaining, 1.0))

    async def report_success(self, slot: int, endpoint: Optional[str] = None):
        async with self._lock:
            cred = self.slots.get(slot)
            if cred:
                cred.consecutive_failures = 0
                cred.consecutive_non_network_failures = 0
                cred.last_success_at = time.monotonic()
                cred.ok_calls += 1

    async def report_failure(self, slot: int, endpoint: Optional[str] = None, kind: str = "unknown", status_code: Optional[int] = None):
        async with self._lock:
            cred = self.slots.get(slot)
            if cred:
                cred.failed_calls += 1
                now = time.monotonic()
                cred.recent_failures.append((now, endpoint or "unknown", kind))
                cred.consecutive_failures += 1
                cred.last_failure_at = now

                non_network_kinds = ("rate_limit", "auth", "schema", "http4xx", "empty")
                if kind in non_network_kinds:
                    cred.consecutive_non_network_failures += 1

                if kind == "rate_limit" or status_code == 429:
                    cred.rate_limited_count += 1

                non_network_events = [(t, e, k) for t, e, k in cred.recent_failures if k in non_network_kinds]
                if len(non_network_events) >= 2:
                    first_ts = min(t for t, e, k in non_network_events)
                    if now - first_ts < 60:
                        if cred.consecutive_non_network_failures >= 2:
                            cred.cooldown_until = max(cred.cooldown_until, now + 300)
                        if cred.consecutive_non_network_failures >= 3:
                            cred.disabled_until = max(cred.disabled_until, now + 900)
                            cred.disabled_reason = "too many non-network failures"

                if cred.disable_count >= 3:
                    disabled_events = [t for t, e, k in cred.recent_failures if k == "disabled"]
                    if len(disabled_events) >= 3 and (now - min(disabled_events)) < 3600:
                        cred.disabled_until = max(cred.disabled_until, now + 3600)
                        cred.disabled_reason = "repeated disables"

    async def report_429(
        self,
        slot: int,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> float:
        cooldown_s = self.default_cooldown_s
        reset_at: Optional[float] = None

        if headers:
            xrl = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
            if xrl:
                try:
                    reset_at = float(xrl)
                except Exception:
                    pass
            retry = headers.get("Retry-After") or headers.get("retry-after")
            if retry:
                try:
                    cooldown_s = max(cooldown_s, float(retry))
                except Exception:
                    pass

        if body and isinstance(body, dict):
            raw = body.get("reset_at") or body.get("resetAt") or body.get("reset_ts") or body.get("resetTs")
            if raw:
                try:
                    v = float(raw)
                    if v > 1e12:
                        reset_at = v / 1000.0
                    elif v > 1e9:
                        reset_at = v
                except Exception:
                    pass
            msg = body.get("message") or body.get("msg") or ""
            if isinstance(msg, str):
                for part in msg.split():
                    try:
                        cooldown_s = max(cooldown_s, float(part))
                    except Exception:
                        pass

        if reset_at is not None:
            now_ts = time.time()
            if reset_at > now_ts:
                cooldown_s = max(cooldown_s, reset_at - now_ts)

        cooldown_s = min(max(cooldown_s, 30), 3600)

        async with self._lock:
            cred = self.slots.get(slot)
            if cred:
                cred.cooldown_until = max(cred.cooldown_until, time.monotonic() + cooldown_s)
                cred.failed_calls += 1
                cred.rate_limited_count += 1
                now = time.monotonic()
                cred.recent_failures.append((now, path, "rate_limit"))
                cred.consecutive_failures += 1
                cred.last_failure_at = now
                logger.warning(f"slot {slot} ({cred.role}) rate limited, cooldown {cooldown_s:.0f}s until {cred.cooldown_until}")

            ep_key = self._endpoint_key(path)
            ep_cooldown = self._endpoint_cooldowns.get(ep_key)
            if ep_cooldown is None:
                ep_cooldown = EndpointCooldown()
                self._endpoint_cooldowns[ep_key] = ep_cooldown
            ep_cooldown.cooldown_until = max(ep_cooldown.cooldown_until, time.monotonic() + min(cooldown_s * 0.5, 60))

        return cooldown_s

    async def report_response_anomaly(self, slot: int, endpoint: str, reason: str):
        async with self._lock:
            cred = self.slots.get(slot)
            if cred:
                cred.failed_calls += 1
                now = time.monotonic()
                cred.recent_failures.append((now, endpoint, "schema"))
                cred.consecutive_failures += 1
                cred.consecutive_non_network_failures += 1
                cred.last_failure_at = now

    async def disable_slot(self, slot: int, seconds: float, reason: str):
        async with self._lock:
            cred = self.slots.get(slot)
            if cred:
                now = time.monotonic()
                cred.disabled_until = max(cred.disabled_until, now + seconds)
                cred.disabled_reason = reason
                cred.disable_count += 1
                cred.recent_failures.append((now, "admin", "disabled"))

    def is_slot_available(self, slot: int) -> bool:
        cred = self.slots.get(slot)
        return cred.is_available() if cred else False

    def get_available_slots(self, pool_slots: List[int]) -> List[int]:
        return [s for s in pool_slots if self.slots.get(s) and self.slots[s].is_available()]

    def is_slot_429_cooldown(self, slot: int) -> bool:
        cred = self.slots.get(slot)
        return cred.is_cooldown() if cred else False

    def is_slot_cooldown(self, slot: int) -> bool:
        cred = self.slots.get(slot)
        if cred:
            return cred.is_cooldown() or cred.is_disabled()
        return False

    def get_slot_health(self, slot: int) -> Dict[str, Any]:
        cred = self.slots.get(slot)
        if not cred:
            return {}
        calls = max(cred.total_calls, 1)
        return {
            "slot": cred.slot,
            "role": cred.role,
            "total_calls": cred.total_calls,
            "total_weight": cred.total_weight,
            "ok_calls": cred.ok_calls,
            "failed_calls": cred.failed_calls,
            "rate_limited_count": cred.rate_limited_count,
            "cooldown_until": cred.cooldown_until if cred.is_cooldown() else None,
            "cooldown_remaining_s": round(cred.cooldown_remaining(), 1),
            "disabled_until": cred.disabled_until if cred.is_disabled() else None,
            "disabled_reason": cred.disabled_reason if cred.is_disabled() else "",
            "disabled_remaining_s": round(cred.disabled_remaining(), 1) if cred.is_disabled() else 0.0,
            "ok_rate": round(cred.ok_calls / calls, 3),
            "endpoints": dict(cred.endpoints),
        }

    def is_discovery_available(self) -> Tuple[bool, Optional[int]]:
        for slot in settings.get_discovery_slots():
            if not self.is_slot_cooldown(slot):
                return True, slot
        return False, None

    def get_all_health(self) -> List[Dict[str, Any]]:
        return [self.get_slot_health(i) for i in sorted(self.slots.keys())]


_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter(credential_count: int = 12) -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(credential_count=credential_count)
    return _rate_limiter
