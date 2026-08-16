from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.collector.dexscreener_fallback import DexScreenerFallbackProvider
from backend.app.services.crypto_market import CoinbasePublicMarketProvider


async def main() -> int:
    coinbase = CoinbasePublicMarketProvider()
    dexscreener = DexScreenerFallbackProvider()
    try:
        crypto = await coinbase.snapshot(force=True)
        dex = await dexscreener.snapshot(force=True)
        report = {
            "credentials_required": False,
            "coinbase": {
                "source": crypto.source,
                "btc_available": crypto.btc_available,
                "sol_available": crypto.sol_available,
                "available_feature_count": sum(value is not None for value in crypto.features.values()),
                "errors": list(crypto.errors),
            },
            "dexscreener": {
                "source": dex.get("source"),
                "available": bool(dex.get("available")),
                "coverage": dex.get("coverage"),
                "errors": list(dex.get("errors") or []),
            },
        }
        target = PROJECT_ROOT / "artifacts" / "public_market_probe.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"artifact": str(target.relative_to(PROJECT_ROOT)), **report}, ensure_ascii=False))
        return 0
    finally:
        await coinbase.close()
        await dexscreener.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
