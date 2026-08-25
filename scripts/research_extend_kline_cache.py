from __future__ import annotations

import asyncio
import gzip
import json
import sys
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT_BOOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT_BOOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT_BOOT))

from backend.app.collector import (
    ApiKeyRoles,
    AsyncRateLimiter,
    CollectorEndpoints,
    GMGNDataClient,
    GMGNEnrichmentProvider,
    HttpxTransport,
)
from backend.app.config import PROJECT_ROOT, get_settings
from backend.app.database import Database
from backend.app.services.platform_configuration import PlatformConfigurationService

CACHE = PROJECT_ROOT / "artifacts/research/kline_cache_v3_2h.json.gz"


async def main() -> None:
    with gzip.open(CACHE, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    items = payload["items"]

    database = Database(get_settings().sqlite_path)
    rows = [
        dict(row)
        for row in database.fetch_all(
            """
            SELECT id,address,entry_time,entry_price,age_minutes,tag
            FROM samples
            WHERE label_status='mature' AND tag IN (0,1)
              AND token_type IN ('new_creation','near_completion')
              AND feature_schema_version='event1m_regime_v3'
            ORDER BY entry_time,id
            """
        )
    ]
    missing = [row for row in rows if str(row["id"]) not in items]
    print(json.dumps({"mature_snapshot": len(rows), "cached": len(items), "missing": len(missing)}), flush=True)
    if not missing:
        payload["sample_count"] = len(rows)
        return

    config = PlatformConfigurationService(database)
    roles = ApiKeyRoles.from_secrets(config.provider_credentials("gmgn"))
    env = dotenv_values(PROJECT_ROOT / ".env")
    endpoints = CollectorEndpoints(
        trenches=env.get("GMGN_TRENCHES_PATH", "/v1/trenches"),
        token_info=env.get("GMGN_TOKEN_INFO_PATH", "/v1/token/info"),
        token_security=env.get("GMGN_TOKEN_SECURITY_PATH", "/v1/token/security"),
        token_pool_info=env.get("GMGN_TOKEN_POOL_INFO_PATH", "/v1/token/pool_info"),
        top_holders=env.get("GMGN_TOKEN_HOLDERS_PATH", "/v1/market/token_top_holders"),
        kline=env.get("GMGN_KLINE_PATH", "/v1/market/token_kline"),
        trending=env.get("GMGN_TRENDING_PATH", "/v1/market/rank"),
        signal=env.get("GMGN_SIGNAL_PATH", "/v1/market/token_signal"),
        hot_searches=env.get("GMGN_HOT_SEARCHES_PATH", "/v1/market/hot_searches"),
        created_tokens=env.get("GMGN_PORTFOLIO_CREATED_TOKENS_PATH", "/v1/user/created_tokens"),
    )
    transport = HttpxTransport()
    client = GMGNDataClient(
        base_url=env.get("GMGN_API_BASE_URL", ""),
        transport=transport,
        limiter=AsyncRateLimiter(1.0),
        endpoints=endpoints,
    )
    provider = GMGNEnrichmentProvider(client, roles)
    errors: list[dict[str, str]] = []
    for index, row in enumerate(missing, start=1):
        try:
            entry_time = int(row["entry_time"])
            bars = await provider.klines(str(row["address"]), entry_time, entry_time + 7200)
            items[str(row["id"])] = {
                "row": row,
                "bars": [
                    [int(bar.timestamp), bar.open, bar.high, bar.low, bar.close]
                    for bar in bars
                ],
            }
            print(json.dumps({"done": index, "missing": len(missing), "id": row["id"]}), flush=True)
        except Exception as exc:
            errors.append({"id": str(row["id"]), "error": f"{type(exc).__name__}: {exc}"[:300]})
    await transport.close()
    if errors:
        raise RuntimeError(json.dumps(errors, ensure_ascii=False))

    payload["sample_count"] = len(rows)
    payload["items"] = items
    with gzip.open(CACHE, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({"saved": str(CACHE), "sample_count": len(rows), "cached": len(items)}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
