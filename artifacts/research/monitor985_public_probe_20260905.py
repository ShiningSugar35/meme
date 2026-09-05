from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.public_social_signals import PublicSocialSignalProvider


async def main() -> int:
    provider = PublicSocialSignalProvider(timeout_seconds=8.0)
    try:
        feeds, failures, fetched_at = await provider._refresh()
        snapshot = await provider.snapshot("non-matching-probe-address", entry_time=int(time.time()))
        report = {
            "credentials_redacted": True,
            "provider": "985monitor_public",
            "fetched_at": fetched_at,
            "successful_sources": sorted(feeds),
            "incomplete_sources_15m": list(snapshot.incomplete_sources),
            "failed_sources": sorted(failures),
            "source_event_counts": {key: len(rows) for key, rows in sorted(feeds.items())},
            "feature_names": sorted(snapshot.features),
            "global_features": {
                key: value
                for key, value in snapshot.features.items()
                if key.startswith("ln(monitor_global_") or key in {"monitor_global_source_diversity_5m", "monitor_source_coverage"}
            },
            "note": "No event content, cookie, token, session, or credential is persisted by this probe.",
        }
        target = PROJECT_ROOT / "artifacts" / "research" / "monitor985_public_probe_20260905.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        await provider.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
