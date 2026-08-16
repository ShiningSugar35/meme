from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import Database
from backend.app.services.collector_worker import CollectorWorker

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "marketcap": ("marketcap", "market_cap", "marketCap"),
    "holder_count": ("holder_count", "holders"),
}

FIELDS = (
    "volume_1m",
    "swaps_1m",
    "buys_1m",
    "sells_1m",
    "buy_volume_1m",
    "sell_volume_1m",
    "volume_2m",
    "dexscr_ad",
    "dexscr_boost_fee",
    "dexscr_trending_bar",
    "x_user_follower",
    "tg_call_count",
    "creator_token_status",
    "holder_count",
    "marketcap",
)


def recursive_find(value: Any, name: str, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(value, Mapping):
        candidate = value.get(name)
        if candidate not in (None, ""):
            return candidate
        for nested in value.values():
            found = recursive_find(nested, name, depth + 1)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for nested in value:
            found = recursive_find(nested, name, depth + 1)
            if found not in (None, ""):
                return found
    return None


async def main() -> int:
    database = Database(PROJECT_ROOT / "data" / "meme_quant.db")
    database.initialize()
    worker = CollectorWorker(database, monitor_only=True)
    service = worker._build()
    try:
        candidates = await service.discovery.discover("new_creation", limit=10)
        sample_count = min(3, len(candidates))
        availability: Counter[str] = Counter()
        type_counts: dict[str, Counter[str]] = {field: Counter() for field in FIELDS}
        for candidate in candidates[:sample_count]:
            try:
                bundle = await service.provider.token_bundle(candidate.address)
            except Exception:
                bundle = {}
            composite = {"trench": dict(candidate.raw), "bundle": bundle}
            for field in FIELDS:
                value = None
                for alias in FIELD_ALIASES.get(field, (field,)):
                    value = recursive_find(composite, alias)
                    if value not in (None, ""):
                        break
                if value not in (None, ""):
                    availability[field] += 1
                    type_counts[field][type(value).__name__] += 1

        regime = worker._regime_provider
        regime_snapshot = await regime.snapshot() if regime is not None else {"available": False, "errors": ["provider_missing"]}
        report = {
            "credentials_redacted": True,
            "candidate_count": len(candidates),
            "feature_probe_samples": sample_count,
            "field_presence_count": {field: availability[field] for field in FIELDS},
            "field_null_or_missing_count": {
                field: max(0, sample_count - availability[field]) for field in FIELDS
            },
            "field_type_counts": {
                field: dict(type_counts[field]) for field in FIELDS
            },
            "regime": {
                key: value
                for key, value in regime_snapshot.items()
                if key not in {"raw", "items", "tokens", "addresses"}
            },
        }
        target = PROJECT_ROOT / "artifacts" / "gmgn_event_feature_probe.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps({"artifact": str(target.relative_to(PROJECT_ROOT)), "candidate_count": len(candidates), "feature_probe_samples": sample_count}, ensure_ascii=False))
        return 0
    finally:
        if worker._transport is not None:
            await worker._transport.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
