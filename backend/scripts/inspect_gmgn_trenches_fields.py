"""GMGN trenches field inspection script.

Call with: python -m backend.scripts.inspect_gmgn_trenches_fields

Tests different trench parameter combinations and records raw/normalized field names.
Results saved to logs/gmgn_field_probe_YYYYMMDD_HHMMSS.json.
"""
import asyncio, json, time, sys, os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings
from app.providers.gmgn_real import GMGNProvider
from app.db.repositories import Repositories


def mask_key(k: str) -> str:
    if len(k) > 8:
        return k[:4] + "..." + k[-4:]
    return "***"


async def probe():
    db_path = getattr(settings, "SQLITE_PATH", "./data/trading_bot.sqlite3")
    repo = await Repositories.create(db_path)
    try:
        provider = GMGNProvider(repo)
        probes = []

        base_payload = {
            "chain": "sol",
            "type": "new_creation",
            "min_created": settings.GMGN_MIN_CREATED_SECONDS,
            "max_created": settings.GMGN_MAX_CREATED_SECONDS,
        }

        test_cases = [
            ("Baseline: type only", {}),
            ("Min liquidity 5000", {"min_liquidity": 5000}),
            ("Max rug 0.15", {"max_rug_ratio": 0.15}),
            ("Full pre-filter set", {
                "max_rug_ratio": 0.15,
                "max_entrapment_ratio": 0.15,
                "min_liquidity_usd": 5000,
                "min_top_holder_rate": 0.145,
                "max_top_holder_rate": 0.275,
                "max_fresh_wallet_rate": 0.15,
                "min_holder_count": 29,
                "min_marketcap": 2900,
                "min_volume_24h": 1200,
            }),
        ]

        for label, extra_params in test_cases:
            params = dict(base_payload)
            if extra_params:
                params.update(extra_params)
            t0 = time.perf_counter()
            try:
                tokens = await provider.fetch_trenches(params)
                elapsed = int((time.perf_counter() - t0) * 1000)
                raw_first = tokens[0] if tokens else {}
                normalized_first = {}
                if raw_first:
                    normalized_first = GMGNProvider._normalize_token_data(raw_first)
            except Exception as e:
                tokens = []
                elapsed = int((time.perf_counter() - t0) * 1000)
                raw_first = {}
                normalized_first = {}
                error_msg = str(e)

            probe = {
                "label": label,
                "params": {k: v for k, v in params.items()},
                "count": len(tokens),
                "latency_ms": elapsed,
                "error": error_msg if "error_msg" in dir() else None,
                "raw_keys": sorted(raw_first.keys()) if raw_first else [],
                "normalized_keys": sorted(normalized_first.keys()) if normalized_first else [],
                "field_hit_rate": {
                    "launchpad_platform": "launchpad_platform" in (raw_first or {}),
                    "liquidity": bool(raw_first.get("liquidity") or raw_first.get("liquidity_usd")),
                    "max_rug_ratio": bool(raw_first.get("max_rug_ratio") or raw_first.get("rug_ratio")),
                    "max_entrapment_ratio": bool(raw_first.get("max_entrapment_ratio") or raw_first.get("entrapment_ratio")),
                    "burn_status": bool(raw_first.get("burn_status")),
                    "sniper_count": bool(raw_first.get("sniper_count")),
                    "top_10_holder_rate": bool(raw_first.get("top_10_holder_rate")),
                },
            }
            probes.append(probe)
            print(f"[{label}] count={len(tokens)} elapsed={elapsed}ms")

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(os.path.dirname(__file__), "..", "..", "logs", f"gmgn_field_probe_{ts}.json")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        payload = {
            "probed_at": datetime.now(timezone.utc).isoformat(),
            "api_base": settings.GMGN_API_BASE_URL,
            "credential_count": len(settings.get_gmgn_credentials()),
            "probes": probes,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nResults saved to {out_path}")
    finally:
        await repo.close()


if __name__ == "__main__":
    asyncio.run(probe())
