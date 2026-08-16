from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.solana_rpc import bounded_rate_probe, configured_rpc_endpoints


async def main() -> int:
    endpoints = configured_rpc_endpoints()
    counts = Counter(endpoint.provider for endpoint in endpoints)
    report = await bounded_rate_probe(endpoints)
    report["configured_provider_counts"] = dict(counts)
    report["expected_independent_alchemy_accounts"] = 4
    report["expected_ankr_freemium_projects"] = 2
    report["credentials_redacted"] = True
    target = PROJECT_ROOT / "artifacts" / "alchemy_same_ip_rate_probe.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "artifact": str(target.relative_to(PROJECT_ROOT)),
        "accounts_tested": report.get("accounts_tested"),
        "any_http_429": report.get("any_http_429"),
        "conclusion": report.get("same_ip_shared_rate_limit_conclusion"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
