"""CredentialRouter — maps endpoint/task_type to slot pools.

Integrates with existing RateLimiter for 429 cooldown and token bucket.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set

from .rate_limiter import get_rate_limiter


SLOT_POOLS: Dict[str, List[int]] = {
    "discovery": [0, 1, 2, 3],
    "token_info": [4, 5],
    "kline": [6, 7],
    "holders": [8, 9, 10, 11],
}

ENDPOINT_TO_POOL: Dict[str, str] = {
    "/v1/trenches": "discovery",
    "/api/v1/trenches": "discovery",
    "/v1/token/info": "token_info",
    "/v1/token/security": "token_info",
    "/v1/token/pool_info": "token_info",
    "/v1/market/token_kline": "kline",
    "/v1/market/token_top_holders": "holders",
    "/v1/market/token_top_traders": "holders",
}


class CredentialRouter:
    def __init__(self):
        self.rl = get_rate_limiter()

    def _pool_slots(self, endpoint: str) -> List[int]:
        for ep_prefix, pool_name in ENDPOINT_TO_POOL.items():
            if ep_prefix in endpoint or endpoint in ep_prefix:
                return SLOT_POOLS.get(pool_name, [4, 5, 6, 7, 8, 9, 10, 11])
        return SLOT_POOLS.get("token_info", [4, 5, 6, 7])

    def _pool_slots_for_task(self, task_type: str) -> List[int]:
        return SLOT_POOLS.get(task_type, [4, 5, 6, 7, 8, 9, 10, 11])

    def choose_slot(self, endpoint: str, task_type: str = "", preferred: Optional[int] = None) -> Optional[int]:
        if preferred is not None and not self.rl.is_slot_cooldown(preferred):
            return preferred

        pool = self._pool_slots(endpoint)
        if task_type:
            pool = self._pool_slots_for_task(task_type) or pool

        for slot in pool:
            if not self.rl.is_slot_cooldown(slot):
                return slot
        return None

    def choose_feature_slot(self, exclude_discovery: bool = True) -> Optional[int]:
        excluded: Set[int] = set(SLOT_POOLS.get("discovery", [])) if exclude_discovery else set()
        all_feature_pools = (
            SLOT_POOLS.get("token_info", [])
            + SLOT_POOLS.get("kline", [])
            + SLOT_POOLS.get("holders", [])
        )
        for slot in all_feature_pools:
            if slot in excluded:
                continue
            if not self.rl.is_slot_cooldown(slot):
                return slot
        return None

    def get_pool_health(self, pool_name: str) -> Dict:
        pool = SLOT_POOLS.get(pool_name, [])
        slots_health = []
        for slot in pool:
            slots_health.append(self.rl.get_slot_health(slot))
        return {
            "pool_name": pool_name,
            "slots": pool,
            "total_cooldown": sum(1 for s in slots_health if s.get("cooldown_until")),
            "slots_health": slots_health,
        }


_router: Optional[CredentialRouter] = None


def get_credential_router() -> CredentialRouter:
    global _router
    if _router is None:
        _router = CredentialRouter()
    return _router
