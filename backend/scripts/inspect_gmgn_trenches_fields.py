"""GMGN trenches field inspection script.

Call with: python -m backend.scripts.inspect_gmgn_trenches_fields

Tests different trench parameter combinations and records raw/normalized field names.
Results saved to logs/gmgn_field_probe_YYYYMMDD_HHMMSS.json.

If no GMGN credentials are configured, prints a message and skips real API probing.
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
        # Check if GMGN is configured
        creds = settings.get_gmgn_credentials()
        api_keys = settings.get_gmgn_api_keys()
        client_ids = settings.get_gmgn_client_ids()
        has_credentials = bool(creds or api_keys or client_ids) and bool(settings.GMGN_API_BASE_URL)

        if not has_credentials:
            print("GMGN credentials not configured, real API probe skipped.")
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(os.path.dirname(__file__), "..", "..", "logs", f"gmgn_field_probe_{ts}.json")
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            payload = {
                "probed_at": datetime.now(timezone.utc).isoformat(),
                "api_base": settings.GMGN_API_BASE_URL,
                "credential_count": len(creds),
                "note": "Real API probe skipped: GMGN credentials not configured",
                "probes": [],
            }
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            print(f"\nResults saved to {out_path}")
            return

        provider = GMGNProvider(repo)
        probes = []

        base_payload = {
            "chain": "sol",
            "type": "new_creation",
            "min_created": settings.GMGN_MIN_CREATED_SECONDS,
            "max_created": settings.GMGN_MAX_CREATED_SECONDS,
        }

        # Fields of interest for probe
        fields_to_probe = [
            "min_liquidity", "min_liquidity_usd",
            "min_marketcap", "min_market_cap",
            "min_volume_24h", "volume_24h",
            "top_holder_rate", "top_10_holder_rate",
            "holder_count",
            "progress",
            "smart_degen_count",
            "swaps_5m", "swaps_1h",
            "volume_5m",
            "price_change_percent1m", "price_change_percent5m", "price_change_percent1h",
            "renounced_mint", "renounced_freeze_account",
            "sell_tax",
            "burn_status",
            "sniper_count",
            "is_wash_trading",
            "rat_trader_amount_rate",
            "suspected_insider_hold_rate",
        ]

        test_cases = [
            ("Baseline: type only", {}),
            ("Min liquidity 5000", {"min_liquidity": 5000}),
            ("Max rug 0.15", {"max_rug_ratio": 0.15}),
            ("Full pre-filter set", {
                "max_rug_ratio": 0.15,
                "max_entrapment_ratio": 0.15,
                "min_liquidity": 5000,
                "min_top_holder_rate": 0.145,
                "max_top_holder_rate": 0.275,
                "max_fresh_wallet_rate": 0.15,
                "min_holder_count": 29,
                "min_marketcap": 2900,
                "min_volume_24h": 1200,
                "min_smart_degen_count": 1,
            }),
        ]

        for label, extra_params in test_cases:
            params = dict(base_payload)
            if extra_params:
                params.update(extra_params)
            t0 = time.perf_counter()
            error_msg = None
            raw_first: Dict[str, Any] = {}
            normalized_first: Dict[str, Any] = {}
            raw_response: Dict[str, Any] = {}
            request_body_sent: Dict[str, Any] = {}
            api_accepted_filters: Dict[str, bool] = {f: None for f in fields_to_probe}
            token_list: List[Dict[str, Any]] = []
            try:
                # Use raw fetch via _make_request to see actual API response
                request_body = dict(params)
                raw_response = await provider._make_request(
                    settings.GMGN_TRENCHES_PATH,
                    params=request_body,
                    method="POST",
                    json_body=request_body,
                )
                request_body_sent = request_body
                elapsed = int((time.perf_counter() - t0) * 1000)

                # Extract tokens from raw response
                data = raw_response.get("data", raw_response)
                if isinstance(data, dict):
                    token_list = data.get("items", data.get("list", data.get("rows", data.get("tokens", []))))
                elif isinstance(data, list):
                    token_list = data

                raw_first = token_list[0] if token_list else {}
                if raw_first:
                    normalized_first = GMGNProvider._normalize_token_data(raw_first)

                # Check which filter params the API accepted
                if token_list:
                    for field in fields_to_probe:
                        if field in extra_params:
                            api_accepted_filters[field] = True
                        # Check if field appears in response
                        appears_in_raw = any(field in t for t in token_list[:3])
                        api_accepted_filters[field] = appears_in_raw
            except Exception as e:
                token_list = []
                elapsed = int((time.perf_counter() - t0) * 1000)
                raw_first = {}
                normalized_first = {}
                error_msg = str(e)[:500]

            probe = {
                "label": label,
                "request_params": {k: v for k, v in params.items()},
                "request_body_sent": request_body_sent,
                "count": len(token_list),
                "latency_ms": elapsed,
                "error": error_msg,
                "raw_response_keys": sorted(raw_response.keys()) if raw_response else [],
                "raw_first_item": raw_first,
                "normalized_first_item": normalized_first,
                "field_hit_rate": {
                    field: any(field in t for t in (token_list[:5] if token_list else []))
                    for field in fields_to_probe
                },
                "api_accepted_filters": api_accepted_filters,
            }
            probes.append(probe)
            status = "OK" if not error_msg else f"ERROR: {error_msg[:80]}"
            print(f"[{label}] count={len(token_list)} elapsed={elapsed}ms {status}")

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(os.path.dirname(__file__), "..", "..", "logs", f"gmgn_field_probe_{ts}.json")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        payload = {
            "probed_at": datetime.now(timezone.utc).isoformat(),
            "api_base": settings.GMGN_API_BASE_URL,
            "credential_count": len(creds),
            "probes": probes,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nResults saved to {out_path}")
    finally:
        await repo.close()


if __name__ == "__main__":
    asyncio.run(probe())
