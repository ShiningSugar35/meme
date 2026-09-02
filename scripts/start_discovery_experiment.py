from __future__ import annotations

import json
import math
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.collector.discovery_experiment import DiscoveryExperimentManager
from backend.app.config import get_settings
from backend.app.database import Database
from backend.app.services.platform_configuration import PlatformConfigurationService


def _load_budget(database: Database, *, rps: float, poll_seconds: float) -> dict[str, float | int]:
    rows = database.fetch_all(
        "SELECT payload_json FROM collector_cycle_snapshots ORDER BY observed_at DESC LIMIT 120"
    )
    enrichment_counts: list[int] = []
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        reached = max(
            0,
            int(payload.get("discovered") or 0)
            - int(payload.get("prefilter_rejected") or 0)
            - int(payload.get("duplicates") or 0),
        )
        enrichment_counts.append(reached)
    if enrichment_counts:
        ordered = sorted(enrichment_counts)
        p95 = ordered[int(0.95 * (len(ordered) - 1))]
        mean = sum(ordered) / len(ordered)
    else:
        p95 = 5
        mean = 2.0
    production_discovery_weight = 6.0  # two Trenches routes * weight 3
    trending_discovery_weight = 3.0  # three Trending routes * weight 1
    max_enrichment_weight = 12.0
    collector_budget = float(rps) * float(poll_seconds) * 0.70
    production_p95_weight = production_discovery_weight + max_enrichment_weight * p95
    available = max(0.0, collector_budget - production_p95_weight - trending_discovery_weight)
    cap = max(0, int(math.floor(available / max_enrichment_weight)))
    return {
        "history_cycles": len(enrichment_counts),
        "mean_candidates_reaching_enrichment": mean,
        "p95_candidates_reaching_enrichment": p95,
        "production_p95_weight_per_cycle": production_p95_weight,
        "trending_fixed_weight_per_cycle": trending_discovery_weight,
        "collector_weight_budget_per_cycle": collector_budget,
        "reserved_shared_capacity_fraction": 0.30,
        "max_shadow_enrich_weight_per_address": max_enrichment_weight,
        "max_shadow_enrich_per_cycle": cap,
    }


def main() -> None:
    settings = get_settings()
    database = Database(settings.database_path)
    database.initialize()
    platform = PlatformConfigurationService(database)
    runtime = platform.runtime_values()
    budget = _load_budget(
        database,
        rps=float(runtime["gmgn_global_rps"]),
        poll_seconds=float(settings.collector_poll_seconds),
    )
    manager = DiscoveryExperimentManager(database)
    experiment = manager.create_or_resume(
        duration_seconds=86_400,
        interval="5m",
        limit_per_source=min(int(settings.gmgn_trenches_limit), 80),
        max_shadow_enrich_per_cycle=int(budget["max_shadow_enrich_per_cycle"]),
        config={
            "load_budget": budget,
            "gmgn_global_weighted_rps": float(runtime["gmgn_global_rps"]),
            "collector_poll_seconds": float(settings.collector_poll_seconds),
            "official_route_weights": {"trending": 1, "trenches": 3, "kline": 2, "top_holders": 5},
        },
    )
    print(json.dumps({"experiment": experiment, "load_budget": budget}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
