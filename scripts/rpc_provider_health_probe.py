from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.solana_rpc import SolanaRpcPool, configured_rpc_endpoints


async def main() -> int:
    rows: list[dict[str, object]] = []
    methods = (
        ("getSlot", []),
        ("getRecentPerformanceSamples", [1]),
        ("getRecentPrioritizationFees", []),
    )
    for endpoint in configured_rpc_endpoints():
        if endpoint.provider == "solana_public":
            continue
        pool = SolanaRpcPool((endpoint,))
        method_status: dict[str, str] = {}
        try:
            for method, params in methods:
                try:
                    await pool._rpc(endpoint, method, list(params))
                    method_status[method] = "ok"
                except Exception as exc:
                    method_status[method] = pool._safe_error(endpoint, exc)
            snapshot = await pool.snapshot()
            rows.append(
                {
                    "provider": endpoint.label,
                    "healthy": snapshot.healthy,
                    "method_status": method_status,
                    "non_vote_tps_available": snapshot.non_vote_tps is not None,
                    "slot_rate_available": snapshot.slot_rate is not None,
                    "priority_fee_p50_available": snapshot.priority_fee_p50 is not None,
                    "priority_fee_p90_available": snapshot.priority_fee_p90 is not None,
                    "latency_ms": snapshot.latency_ms,
                    "errors": list(snapshot.errors),
                }
            )
        finally:
            await pool.close()
    report = {"credentials_redacted": True, "providers": rows}
    target = PROJECT_ROOT / "artifacts" / "rpc_provider_health_probe.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"artifact": str(target.relative_to(PROJECT_ROOT)), "providers": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
