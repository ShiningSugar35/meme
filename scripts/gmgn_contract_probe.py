from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.collector.constants import DISCOVERY_TYPES
from backend.app.collector.filters import normalize_token
from backend.app.database import Database
from backend.app.services.collector_worker import CollectorWorker


async def main() -> int:
    database = Database(PROJECT_ROOT / "data" / "meme_quant.db")
    worker = CollectorWorker(database, monitor_only=True)
    service = worker._build()
    result: dict[str, object] = {"request_policy": {}}
    address_sets: dict[str, set[str]] = {}

    try:
        request_policy = result["request_policy"]
        assert isinstance(request_policy, dict)
        for token_type in DISCOVERY_TYPES:
            request_policy[token_type] = service.discovery.request_body(token_type, 80)[token_type]
            candidates = await service.discovery.discover(token_type, limit=80)
            address_sets[token_type] = {candidate.address for candidate in candidates}
            reasons: Counter[str] = Counter()
            ages: list[float] = []
            coarse_pass = 0
            for candidate in candidates:
                normalized = normalize_token(candidate.raw, candidate.token_type)
                age = normalized.get("age")
                if isinstance(age, (int, float)):
                    ages.append(float(age))
                decision = service.enrichment.prefilter(candidate)
                if decision.accepted:
                    coarse_pass += 1
                else:
                    reasons.update(decision.reasons)
            result[token_type] = {
                "returned": len(candidates),
                "age_min_minutes": min(ages) if ages else None,
                "age_max_minutes": max(ages) if ages else None,
                "coarse_pass": coarse_pass,
                "coarse_rejection_reasons": dict(reasons.most_common()),
            }

        new_addresses = address_sets.get("new_creation", set())
        near_addresses = address_sets.get("near_completion", set())
        result["cross_lifecycle"] = {
            "intersection": len(new_addresses & near_addresses),
            "new_unique": len(new_addresses),
            "near_unique": len(near_addresses),
            "same_set": bool(new_addresses) and new_addresses == near_addresses,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        if worker._transport is not None:
            await worker._transport.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
