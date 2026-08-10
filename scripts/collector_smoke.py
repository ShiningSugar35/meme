from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import Database
from backend.app.services.collector_worker import CollectorWorker


async def main() -> int:
    """Read-only deployment smoke test for the GMGN data path.

    This intentionally does not persist samples, start workers, sign transactions,
    or print credentials/token addresses. It validates current local `.env`
    configuration by constructing the normal adapter, issuing one discovery request,
    and, when possible, one enrichment attempt.
    """

    database = Database(PROJECT_ROOT / "data" / "meme_quant.db")
    worker = CollectorWorker(database, monitor_only=False)
    try:
        try:
            service = worker._build()
        except Exception as exc:
            print(f"build=blocked error_type={type(exc).__name__}")
            return 2

        try:
            candidates = await service.discovery.discover("new_creation", limit=1)
        except Exception as exc:
            print(f"discovery=failed error_type={type(exc).__name__}")
            return 3

        print(f"discovery=ok candidates={len(candidates)}")
        if not candidates:
            print("enrichment=skipped reason=no_candidate_returned")
            return 0

        try:
            result = await service.enrichment.enrich(candidates[0])
        except Exception as exc:
            print(f"enrichment=failed error_type={type(exc).__name__}")
            return 4

        if result.sample is not None:
            feature_count = len(result.sample.features)
            populated = sum(value is not None for value in result.sample.features.values())
            print(f"enrichment=accepted features={feature_count} populated={populated}")
        else:
            print(f"enrichment=rejected reasons={','.join(result.decision.reasons[:5])}")
        return 0
    finally:
        if worker._transport is not None:
            await worker._transport.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
