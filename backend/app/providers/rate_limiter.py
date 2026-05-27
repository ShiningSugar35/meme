"""Weight-aware credential rate limiter with 429 cooldown support."""
from __future__ import annotations

import asyncio
import time
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

    def is_cooldown(self) -> bool:
        return time.monotonic() < self.cooldown_until

    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.monotonic())


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

        primary = getattr(settings, "GMGN_DISCOVERY_PRIMARY_SLOT", 0)
        reserve = getattr(settings, "GMGN_DISCOVERY_RESERVE_SLOT", 1)
        feature_slots = settings.get_feature_slots()

        for i in range(credential_count):
            if i == primary:
                role = "discovery_primary"
            elif i == reserve:
                role = "discovery_reserve"
            elif i in feature_slots:
                role = "feature"
            else:
                role = "unassigned"
            self.slots[i] = CredentialSlot(slot=i, role=role)

    def _endpoint_key(self, path: str) -> str:
        return path.split("?")[0].rstrip("/")

    async def acquire(self, slot: int, path: str) -> bool:
        async with self._lock:
            cred = self.slots.get(slot)
            if cred is None:
                return True

            if cred.is_cooldown():
                remaining = cred.cooldown_remaining()
                logger.debug(f"slot {slot} in cooldown ({remaining:.0f}s remaining)")
                return False

            ep_key = self._endpoint_key(path)
            if ep_key in self._endpoint_cooldowns and self._endpoint_cooldowns[ep_key].is_cooldown():
                logger.debug(f"endpoint {ep_key} in cooldown")
                return False

            weight = _endpoint_weight(path)
            cred.total_calls += 1
            cred.total_weight += weight
            cred.endpoints[ep_key] = cred.endpoints.get(ep_key, 0) + 1
            return True

    async def report_success(self, slot: int):
        async with self._lock:
            cred = self.slots.get(slot)
            if cred:
                cred.ok_calls += 1

    async def report_failure(self, slot: int):
        async with self._lock:
            cred = self.slots.get(slot)
            if cred:
                cred.failed_calls += 1

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
                cred.rate_limited_count += 1
                logger.warning(f"slot {slot} ({cred.role}) rate limited, cooldown {cooldown_s:.0f}s until {cred.cooldown_until}")

            ep_key = self._endpoint_key(path)
            ep_cooldown = self._endpoint_cooldowns.get(ep_key)
            if ep_cooldown is None:
                ep_cooldown = EndpointCooldown()
                self._endpoint_cooldowns[ep_key] = ep_cooldown
            ep_cooldown.cooldown_until = max(ep_cooldown.cooldown_until, time.monotonic() + min(cooldown_s * 0.5, 60))

        return cooldown_s

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
            "ok_rate": round(cred.ok_calls / calls, 3),
            "endpoints": dict(cred.endpoints),
        }

    def is_slot_cooldown(self, slot: int) -> bool:
        cred = self.slots.get(slot)
        return cred.is_cooldown() if cred else False

    def is_discovery_available(self) -> Tuple[bool, Optional[int]]:
        primary = getattr(settings, "GMGN_DISCOVERY_PRIMARY_SLOT", 0)
        reserve = getattr(settings, "GMGN_DISCOVERY_RESERVE_SLOT", 1)
        if not self.is_slot_cooldown(primary):
            return True, primary
        if not self.is_slot_cooldown(reserve):
            return True, reserve
        return False, None

    def get_all_health(self) -> List[Dict[str, Any]]:
        return [self.get_slot_health(i) for i in sorted(self.slots.keys())]


_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter(credential_count: int = 12) -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(credential_count=credential_count)
    return _rate_limiter
