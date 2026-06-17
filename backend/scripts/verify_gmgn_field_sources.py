#!/usr/bin/env python
"""
P-1: GMGN API field source verification.

Recursively maps every expected field to its actual JSON path in each
GMGN API response. Outputs a field source map JSON for downstream P0-P8 work.

Usage:
    python scripts/verify_gmgn_field_sources.py --sample-size 5 --out logs/verify.json
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys, time
from typing import Any, Dict, List, Optional, Set, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if os.path.dirname(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, os.path.dirname(PROJECT_ROOT))

os.environ.setdefault("PROVIDER_MODE", "online_readonly")
os.environ.setdefault("DRY_RUN", "true")

import aiosqlite
from app.config import ProviderMode, settings
from app.providers.gmgn_real import GMGNProvider
from app.db.repositories import Repositories

# ---- field alias catalog ----------------------------------------------------
FIELD_ALIASES: Dict[str, List[str]] = {
    "price_usd":               ["price_usd", "price", "usd_price"],
    "price_sol":               ["price_sol", "sol_price", "native_price"],
    "liquidity_usd":           ["liquidity_usd", "liquidity", "pool_liquidity_usd", "reserve_usd"],
    "market_cap":              ["market_cap", "marketcap", "fdv", "fully_diluted_valuation", "usd_market_cap"],
    "holder_count":            ["holder_count", "holders", "total_holders", "holder"],
    "top_10_holder_rate":      ["top_10_holder_rate", "top10_holder_rate", "top10_holder_percent", "top_10_rate"],
    "top1_holder_rate":        ["top1_holder_rate", "top_1_holder_rate", "top_holder_rate"],
    "fresh_wallet_rate":       ["fresh_wallet_rate", "fresh_wallets_rate", "fresh_wallet", "fresh_rate"],
    "max_rug_ratio":           ["max_rug_ratio", "rug_ratio", "max_rugged_ratio", "rug"],
    "max_entrapment_ratio":    ["max_entrapment_ratio", "entrapment_ratio", "entrapment"],
    "max_insider_ratio":       ["max_insider_ratio", "insider_ratio", "insider_rate", "max_insider_rate"],
    "max_bundler_rate":        ["max_bundler_rate", "bundler_rate", "bundler_trader_amount_rate", "bundler"],
    "is_wash_trading":         ["is_wash_trading", "wash_trading", "wash_trading_detected", "is_wash"],
    "rat_trader_amount_rate":  ["rat_trader_amount_rate", "rat_trader_rate", "rat_trader"],
    "suspected_insider_hold_rate": ["suspected_insider_hold_rate", "insider_hold_rate", "suspected_insider_rate"],
    "sniper_count":            ["sniper_count", "snipers", "sniper_trader_count", "sniper_cnt"],
    "sell_tax":                ["sell_tax", "sell_tax_rate"],
    "burn_status":             ["burn_status", "lp_burn_status", "burnt_status"],
    "creator_balance_rate":    ["creator_balance_rate", "creator_hold_rate", "dev_team_hold_rate", "dev_hold_rate", "creator_token_balance_rate"],
    "creator_token_balance":   ["creator_token_balance", "creator_balance", "dev_token_balance", "creator_amount"],
    "total_supply":            ["total_supply", "supply", "token_total_supply"],
    "swaps_1h":                ["swaps_1h", "swaps1h", "trade_1h", "trades_1h"],
    "volume_1h":               ["volume_1h", "volume1h", "trade_volume_1h", "volume_h1"],
    "buy_volume_1h":           ["buy_volume_1h", "buyVolume1h", "buy_volume1h"],
    "sell_volume_1h":          ["sell_volume_1h", "sellVolume1h", "sell_volume1h"],
    "volume_usd":              ["volume_usd", "volume", "volume_24h", "volume_h24"],
    "price_change_percent1h":  ["price_change_percent1h", "price_change_1h", "price_change_percent_1h", "change_1h"],
    "price_1h":                ["price_1h", "price1h"],
    "socials":                 ["socials", "links", "link"],
    "link":                    ["link", "website", "twitter"],
    "dev_team_hold_rate":      ["dev_team_hold_rate", "dev_hold_rate", "creator_hold_rate"],
    "dev_token_burn_ratio":    ["dev_token_burn_ratio", "burn_ratio", "lp_burn_ratio"],
    "pool_created_at":         ["pool_created_at", "creation_time", "created_at", "open_time", "launch_time", "created_timestamp"],
    "renounced_mint":          ["renounced_mint", "mint_renounced", "is_mint_renounced"],
    "renounced_freeze_account":["renounced_freeze_account", "freeze_renounced", "is_freeze_renounced", "freeze_authority_renounced"],
    "has_social":              ["has_social", "has_at_least_one_social", "has_twitter_or_telegram"],
    "creator_token_status":    ["creator_token_status", "creator_status"],
}

TRENCH_ONLY_SUSPECTS = {
    "symbol", "name", "launchpad", "platform", "type",
}

# ---- helpers ----------------------------------------------------------------

def recursive_find_paths(obj: Any, aliases: List[str], path: str = "", depth: int = 0) -> List[dict]:
    """Recursively search for any of the alias keys, return list of {json_path, value}."""
    results: List[dict] = []
    if depth > 8 or obj is None:
        return results
    if isinstance(obj, dict):
        for a in aliases:
            if a in obj:
                val = obj[a]
                if val is not None and val != "" and val != [] and val != {}:
                    results.append({"json_path": f"{path}.{a}" if path else a, "value": val})
        for k, v in obj.items():
            if k in ("raw_json", "request_json", "response_json"):
                continue
            results.extend(recursive_find_paths(v, aliases, f"{path}.{k}" if path else k, depth + 1))
    return results


def mask_key(v: Any) -> str:
    s = str(v or "")
    if len(s) <= 8:
        return "***"
    return s[:4] + "..." + s[-4:]


async def safe_call(provider, path: str, params: dict, method: str = "GET", json_body: dict = None) -> dict:
    """Call API endpoint, return {status, top_keys, code, message, data_keys, price_keys, pool_keys, error}."""
    result: dict = {"endpoint": path, "status": "UNKNOWN"}
    try:
        data = await provider._make_request(path, params, method=method, json_body=json_body)
        if isinstance(data, dict):
            result["status"] = "OK"
            result["code"] = data.get("code")
            result["message"] = str(data.get("message", ""))[:80]
            inner = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
            result["top_keys"] = list(data.keys())
            result["data_keys"] = list(inner.keys()) if isinstance(inner, dict) else []
            if isinstance(inner.get("price"), dict):
                result["price_keys"] = sorted(inner["price"].keys())
            if isinstance(inner.get("pool"), dict):
                result["pool_keys"] = sorted(inner["pool"].keys())
            result["raw"] = data
        else:
            result["status"] = "NOT_DICT"
            result["type"] = type(data).__name__
    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)[:200]
    return result


# ---- main -------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=3)
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args()

    db = await aiosqlite.connect(":memory:")
    await db.executescript("CREATE TABLE IF NOT EXISTS provider_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, endpoint TEXT, method TEXT, status_code INTEGER, latency_ms INTEGER, ok INTEGER, error_code TEXT, error_summary TEXT, request_json TEXT, response_json TEXT, created_at TEXT);")
    repo = Repositories(db)
    provider = GMGNProvider(repo, mode=ProviderMode.ONLINE_READONLY)
    settings.set_provider_mode(ProviderMode.ONLINE_READONLY)

    print(f"{'='*70}")
    print(f"  P-1 GMGN API Field Source Verification")
    print(f"  API: {settings.GMGN_API_BASE_URL}")
    print(f"  Credentials: {len(provider.credentials)}")
    print(f"{'='*70}\n")

    # 1) Fetch tokens from trenches
    platforms_str = getattr(settings, "GMGN_TRENCHES_PLATFORMS", "Pump.fun,Moonshot")
    platforms = [p.strip() for p in platforms_str.split(",") if p.strip()]
    print(f"[1] Fetching trenches (new_creation, limit={args.sample_size})...")
    trenches = await provider.fetch_trenches({
        "chain": "sol", "types": "new_creation",
        "platforms": platforms, "limit": args.sample_size,
    })
    if not trenches:
        print("    Trenches empty, using fallback mint.")
        trenches = [{"token_mint": "AQe3YYFrbeuTTFLa2VMSf2oZXaBLmesJkC12JY74pump"}]
    print(f"    Got {len(trenches)} tokens.\n")

    # 2) For each token, call all endpoints
    token_mints = [t.get("token_mint") or t.get("address") or t.get("mint", "") for t in trenches[:args.sample_size]]
    token_mints = [t for t in token_mints if t]

    all_endpoint_results: List[dict] = []
    field_source_map: Dict[str, dict] = {}

    for idx, tm in enumerate(token_mints):
        print(f"[2.{idx+1}] Token: {tm[:16]}...")
        params = {"chain": "sol", "address": tm}

        # Call each endpoint
        endpoints_to_call = [
            ("token_info", settings.GMGN_TOKEN_INFO_PATH),
            ("security", settings.GMGN_TOKEN_SECURITY_PATH),
            ("pool_info", settings.GMGN_TOKEN_POOL_INFO_PATH),
            ("top_holders", settings.GMGN_TOKEN_HOLDERS_PATH),
            ("kline", settings.GMGN_KLINE_PATH),
        ]
        # Kline needs extra params
        kline_params = {**params, "resolution": "1h", "limit": 5}

        for ep_name, ep_path in endpoints_to_call:
            if not ep_path:
                continue
            ep_params = kline_params if ep_name == "kline" else params
            print(f"    -> {ep_name} ({ep_path})...")
            res = await safe_call(provider, ep_path, ep_params, method="GET")
            res["endpoint_name"] = ep_name
            all_endpoint_results.append({"token_mint": tm, **res})

            # Recursively search for every field alias in the raw response
            raw = res.get("raw", {})
            if isinstance(raw, dict):
                for field_name, aliases in FIELD_ALIASES.items():
                    hits = recursive_find_paths(raw, aliases)
                    if hits:
                        existing = field_source_map.get(field_name)
                        if existing is None:
                            field_source_map[field_name] = {
                                "field": field_name,
                                "aliases": list(aliases),
                                "sources": [],
                            }
                        for h in hits:
                            # Truncate large values for report
                            val = h["value"]
                            if isinstance(val, float) and abs(val) > 1e9:
                                val_desc = f"{val:.2e}"
                            elif isinstance(val, float):
                                val_desc = f"{val:.8f}"
                            elif isinstance(val, str) and len(val) > 60:
                                val_desc = val[:57] + "..."
                            else:
                                val_desc = str(val)
                            field_source_map[field_name]["sources"].append({
                                "endpoint": ep_name,
                                "json_path": h["json_path"],
                                "token_mint": tm,
                                "sample_value": val_desc,
                            })

        # Also search the trenches data itself
        trench = trenches[idx] if idx < len(trenches) else {}
        for field_name, aliases in FIELD_ALIASES.items():
            hits = recursive_find_paths(trench, aliases)
            if hits:
                existing = field_source_map.get(field_name)
                if existing is None:
                    field_source_map[field_name] = {
                        "field": field_name,
                        "aliases": list(aliases),
                        "sources": [],
                    }
                for h in hits:
                    val_desc = str(h["value"])[:80]
                    field_source_map[field_name]["sources"].append({
                        "endpoint": "trenches",
                        "json_path": h["json_path"],
                        "token_mint": tm,
                        "sample_value": val_desc,
                    })

    # 3) Classify each field
    field_classifications: Dict[str, dict] = {}
    for field_name in FIELD_ALIASES:
        info = field_source_map.get(field_name, {})
        sources = info.get("sources", [])
        ep_counts: Dict[str, int] = {}
        for s in sources:
            ep_counts[s["endpoint"]] = ep_counts.get(s["endpoint"], 0) + 1

        # Determine classification
        endpoints_hit = set(ep_counts.keys())
        if not sources:
            classification = "API_NULL_OR_PLAN_UNAVAILABLE"
        elif "token_info" in endpoints_hit:
            # Check if it's in data.price or data.pool
            price_paths = [s["json_path"] for s in sources if "price." in s["json_path"] and s["endpoint"] == "token_info"]
            pool_paths = [s["json_path"] for s in sources if "pool." in s["json_path"] and s["endpoint"] == "token_info"]
            top_paths = [s["json_path"] for s in sources if s["endpoint"] == "token_info" and "price." not in s["json_path"] and "pool." not in s["json_path"]]
            if price_paths:
                classification = "DIRECT_FOUND"
            elif pool_paths:
                classification = "DIRECT_FOUND"
            elif top_paths:
                classification = "DIRECT_FOUND"
            else:
                classification = "DIRECT_FOUND"
        elif "trenches" in endpoints_hit and not endpoints_hit - {"trenches"}:
            classification = "TRENCH_ONLY"
        elif "security" in endpoints_hit:
            classification = "DIRECT_FOUND"
        elif "top_holders" in endpoints_hit:
            classification = "TOP_HOLDERS_ENDPOINT"
        elif "kline" in endpoints_hit:
            classification = "KLINE_ENDPOINT"
        else:
            classification = "DIRECT_FOUND"

        # Check if computed from other fields
        computed_from: list = []
        if field_name == "volume_1h" and "buy_volume_1h" in endpoints_hit and "sell_volume_1h" in endpoints_hit:
            computed_from = ["buy_volume_1h", "sell_volume_1h"]
        if field_name == "price_change_percent1h" and "price" in str(sources) and "price_1h" in str(sources):
            # Check if direct field exists
            direct = [s for s in sources if "percent" in s["json_path"].lower() or "change" in s["json_path"].lower()]
            if not direct:
                computed_from = ["price", "price_1h"]
        if field_name == "creator_balance_rate":
            cb = [s for s in sources if "balance_rate" in s["json_path"] or "hold_rate" in s["json_path"] or "creator_balance_rate" == s["json_path"]]
            if not cb:
                computed_from = ["creator_token_balance", "total_supply"]

        if computed_from:
            classification = "COMPUTED"

        field_classifications[field_name] = {
            "field": field_name,
            "aliases": FIELD_ALIASES[field_name],
            "classification": classification,
            "endpoints_hit": sorted(endpoints_hit),
            "endpoint_counts": ep_counts,
            "computed_from": computed_from,
            "sample_sources": sources[:5],
        }

    # 4) Output
    output = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "api_base": settings.GMGN_API_BASE_URL,
        "tokens_sampled": len(token_mints),
        "endpoint_summaries": [],
        "field_classifications": field_classifications,
    }

    # Endpoint summaries
    unique_endpoints: dict = {}
    for r in all_endpoint_results:
        nm = r.get("endpoint_name", "?")
        if nm not in unique_endpoints:
            unique_endpoints[nm] = {
                "endpoint_name": nm,
                "endpoint_path": r.get("endpoint", ""),
                "status": r.get("status", "?"),
                "code": r.get("code"),
                "data_top_keys": r.get("data_keys", []),
                "price_keys": r.get("price_keys", []),
                "pool_keys": r.get("pool_keys", []),
            }
    output["endpoint_summaries"] = list(unique_endpoints.values())

    # Field source map (raw data)
    output["field_source_map"] = field_source_map

    json_output = json.dumps(output, ensure_ascii=False, default=str, indent=2)

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(json_output)
        print(f"\nOutput written to {args.out}")

    # Print summary table
    print(f"\n{'='*70}")
    print(f"  FIELD CLASSIFICATION SUMMARY")
    print(f"{'='*70}")
    print(f"{'Field':<30} {'Classification':<30} {'From':<40}")
    print(f"{'-'*30} {'-'*30} {'-'*40}")
    for field_name in sorted(field_classifications.keys()):
        fc = field_classifications[field_name]
        print(f"{field_name:<30} {fc['classification']:<30} {','.join(fc['endpoints_hit']):<40}")

    # Print critical issues
    not_found = [f for f, fc in field_classifications.items() if fc["classification"] == "API_NULL_OR_PLAN_UNAVAILABLE"]
    trench_only = [f for f, fc in field_classifications.items() if fc["classification"] == "TRENCH_ONLY"]
    computed = [f for f, fc in field_classifications.items() if fc["classification"] == "COMPUTED"]

    print(f"\n    NOT FOUND: {len(not_found)} fields")
    for f in not_found:
        print(f"      - {f}")
    print(f"\n    TRENCH ONLY: {len(trench_only)} fields")
    for f in trench_only:
        print(f"      - {f}")
    print(f"\n    COMPUTED: {len(computed)} fields")
    for f in computed:
        print(f"      - {f}: from {field_classifications[f]['computed_from']}")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
