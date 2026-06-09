"""CredentialRouter — maps endpoint/task_type to slot pools.

Integrates with existing RateLimiter for 429 cooldown, token bucket, and disable.
Round-robins within each pool to avoid hammering a single slot.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set

from ..config import settings
from .rate_limiter import get_rate_limiter


ENDPOINT_TO_POOL: Dict[str, str] = {
    "/v1/trenches": "discovery_new_creation",
    "/api/v1/trenches": "discovery_new_creation",
    "/v1/token/info": "token_info",
    "/v1/token/security": "token_info",
    "/v1/token/pool_info": "token_info",
    "/v1/market/token_kline": "kline",
    "/v1/market/token_top_holders": "holders",
    "/v1/market/token_top_traders": "holders",
}


def _pool_name(endpoint: str) -> str:
    for ep_prefix, pool_name in ENDPOINT_TO_POOL.items():
        if ep_prefix in endpoint or endpoint in ep_prefix:
            return pool_name
    return "token_info"


class CredentialRouter:
    def __init__(self):
        self.rl = get_rate_limiter()
        self._cursors: Dict[str, int] = {}

    def _pool_slots(self, endpoint: str) -> List[int]:
        pools = settings.get_gmgn_slot_pools()
        return pools.get(_pool_name(endpoint), [])

    def _pool_slots_for_task(self, task_type: str) -> List[int]:
        return settings.get_gmgn_slot_pools().get(task_type, [])

    def choose_slot(self, endpoint: str, task_type: str = "", preferred: Optional[int] = None) -> Optional[int]:
        if preferred is not None and self.rl.is_slot_available(preferred):
            return preferred

        pool = self._pool_slots(endpoint)
        if task_type:
            pool = self._pool_slots_for_task(task_type) or pool

        if not pool:
            return None

        cursor = self._cursors.setdefault(task_type or endpoint, 0)
        for offset in range(len(pool)):
            idx = (cursor + offset) % len(pool)
            slot = pool[idx]
            if self.rl.is_slot_available(slot):
                self._cursors[task_type or endpoint] = (idx + 1) % len(pool)
                return slot
        return None

    def choose_discovery_slot(self, type_name: str) -> Optional[int]:
        pool_name = f"discovery_{type_name}"
        pool = settings.get_gmgn_slot_pools().get(pool_name, [])
        if not pool:
            return None
        for slot in pool:
            if self.rl.is_slot_available(slot):
                return slot
        return None

    def choose_feature_slot(self, endpoint: str, exclude: Optional[Set[int]] = None) -> Optional[int]:
        exclude = exclude or set()
        pool_name = _pool_name(endpoint)
        pool = settings.get_gmgn_slot_pools().get(pool_name, [])
        if not pool:
            pool = settings.get_gmgn_slot_pools().get("feature", [])
        if not pool:
            return None
        cursor = self._cursors.setdefault(f"feature:{pool_name}", 0)
        for offset in range(len(pool)):
            idx = (cursor + offset) % len(pool)
            slot = pool[idx]
            if slot not in exclude and self.rl.is_slot_available(slot):
                self._cursors[f"feature:{pool_name}"] = (idx + 1) % len(pool)
                return slot
        return None

    def get_pool_health(self) -> Dict:
        all_pools = settings.get_gmgn_slot_pools()
        result = {}
        for pool_name, pool_slots in all_pools.items():
            slots_health = []
            for slot in pool_slots:
                slots_health.append(self.rl.get_slot_health(slot))
            result[pool_name] = {
                "pool_name": pool_name,
                "slots": pool_slots,
                "total_cooldown": sum(1 for s in slots_health if s.get("cooldown_until")),
                "slots_health": slots_health,
            }
        return result


_router: Optional[CredentialRouter] = None


def get_credential_router() -> CredentialRouter:
    global _router
    if _router is None:
        _router = CredentialRouter()
    return _router
