"""CredentialRouter — maps endpoint/task_type to slot pools.

Integrates with existing RateLimiter for 429 cooldown and token bucket.
Round-robins within each pool to avoid hammering a single slot.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..config import settings
from .rate_limiter import get_rate_limiter


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
        self._cursors: Dict[str, int] = {}

    def _pool_slots(self, endpoint: str) -> List[int]:
        pools = settings.get_gmgn_slot_pools()
        for ep_prefix, pool_name in ENDPOINT_TO_POOL.items():
            if ep_prefix in endpoint or endpoint in ep_prefix:
                return pools.get(pool_name, [])
        return pools.get("token_info", [])

    def _pool_slots_for_task(self, task_type: str) -> List[int]:
        return settings.get_gmgn_slot_pools().get(task_type, [])

    def choose_slot(self, endpoint: str, task_type: str = "", preferred: Optional[int] = None) -> Optional[int]:
        if preferred is not None and not self.rl.is_slot_cooldown(preferred):
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
            if not self.rl.is_slot_cooldown(slot):
                self._cursors[task_type or endpoint] = (idx + 1) % len(pool)
                return slot
        return None

    def get_pool_health(self, pool_name: str) -> Dict:
        pool = settings.get_gmgn_slot_pools().get(pool_name, [])
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
