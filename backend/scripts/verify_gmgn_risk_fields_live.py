#!/usr/bin/env python
"""
P-2: GMGN risk field live probe.
Verifies that rug_ratio, suspected_insider_hold_rate, is_wash_trading
exist in real API responses before modifying the normalizer.
"""
from __future__ import annotations

import asyncio, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("PROVIDER_MODE", "online_readonly")
os.environ.setdefault("DRY_RUN", "true")

from app.config import ProviderMode, settings
from app.providers.gmgn_real import GMGNProvider
from app.db.repositories import Repositories

FIELD_CANDIDATES = {
    "rug_ratio": [
        "rug_ratio", "rug-ratio", "max_rug_ratio", "max-rug-ratio", "max_rugged_ratio",
    ],
    "suspected_insider_hold_rate": [
        "suspected_insider_hold_rate", "suspected-insider-hold-rate",
        "insider_hold_rate", "insider-hold-rate", "suspected_insider_rate",
    ],
    "is_wash_trading": [
        "is_wash_trading", "is-wash-trading", "wash_trading", "wash-trading",
        "wash_trading_detected", "is_wash",
    ],
}

PLATFORMS = [
    "Pump.fun", "Moonshot", "moonshot_app", "letsbonk", "memoo",
    "token_mill", "jup_studio", "bags", "believe", "heaven",
]


def walk_dict(obj, path=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            out.append((path, str(k), v))
            out.extend(walk_dict(v, p))
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:20]):
            out.extend(walk_dict(item, f"{path}[{i}]"))
    return out


def find_field_candidates(raw_obj):
    flattened = walk_dict(raw_obj)
    result = {}
    for canonical, candidates in FIELD_CANDIDATES.items():
        hits = []
        for p, key, value in flattened:
            for cand in candidates:
                if key == cand:
                    hits.append({"candidate": cand, "path": p, "key": key, "value": value})
        result[canonical] = hits
    return result


def extract_raw_trench_items(raw):
    data = raw.get("data", raw) if isinstance(raw, dict) else {}
    items = []
    for key in ("new_creation", "pump", "near_completion", "completed"):
        val = data.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    items.append({"section": key, "raw": item})
        elif isinstance(val, dict):
            for inner_key in ("list", "items", "rows", "data", "tokens"):
                arr = val.get(inner_key)
                if isinstance(arr, list):
                    for item in arr:
                        if isinstance(item, dict):
                            items.append({"section": key, "raw": item})
    return items


async def main():
    repo = await Repositories.create()
    gmgn = GMGNProvider(repo, mode=settings.get_provider_mode())

    if settings.get_provider_mode() == ProviderMode.MOCK:
        raise RuntimeError("Must run in ONLINE_READONLY or LIVE mode, not MOCK.")

    out_dir = Path("logs")
    out_dir.mkdir(parents=True, exist_ok=True)

    section = {
        "filters": ["offchain", "onchain"],
        "launchpad_platform_v2": True,
        "quote_address_type": [4, 5, 3, 1, 13, 0],
        "limit": 10,
        "launchpad_platform": PLATFORMS,
        "renounced_mint": 1,
        "renounced_freeze_account": 1,
    }

    body = {"version": "v2", "new_creation": dict(section), "near_completion": dict(section)}

    raw_trenches = await gmgn._make_request(
        settings.GMGN_TRENCHES_PATH,
        {"chain": "sol"},
        method="POST",
        json_body=body,
        credential_slot=0,
    )

    raw_items = extract_raw_trench_items(raw_trenches)[:10]

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "field_candidates": FIELD_CANDIDATES,
        "raw_trenches_top_keys": list(raw_trenches.keys()) if isinstance(raw_trenches, dict) else [],
        "items": [],
        "summary": {},
    }

    for idx, item in enumerate(raw_items):
        raw_pool = item["raw"]
        token_mint = (
            raw_pool.get("token_mint") or raw_pool.get("token_address")
            or raw_pool.get("address") or raw_pool.get("mint")
            or raw_pool.get("base_address")
        )

        item_report = {
            "idx": idx,
            "section": item["section"],
            "token_mint": token_mint,
            "trenches_raw_key_count": len(raw_pool.keys()),
            "trenches_raw_keys": sorted(list(raw_pool.keys())),
            "trenches_field_hits": find_field_candidates(raw_pool),
            "token_info_field_hits": {},
            "token_security_field_hits": {},
            "normalized_snapshot": {},
        }

        if token_mint:
            info_path = getattr(settings, "GMGN_TOKEN_INFO_PATH", None)
            if info_path:
                try:
                    raw_info = await gmgn._make_request(
                        info_path, {"chain": "sol", "address": token_mint},
                        method="GET", credential_slot=0,
                    )
                    item_report["token_info_top_keys"] = list(raw_info.keys()) if isinstance(raw_info, dict) else []
                    item_report["token_info_field_hits"] = find_field_candidates(raw_info)
                except Exception as e:
                    item_report["token_info_error"] = str(e)

            sec_path = getattr(settings, "GMGN_TOKEN_SECURITY_PATH", None)
            if sec_path:
                try:
                    raw_sec = await gmgn._make_request(
                        sec_path, {"chain": "sol", "address": token_mint},
                        method="GET", credential_slot=0,
                    )
                    item_report["token_security_top_keys"] = list(raw_sec.keys()) if isinstance(raw_sec, dict) else []
                    item_report["token_security_field_hits"] = find_field_candidates(raw_sec)
                except Exception as e:
                    item_report["token_security_error"] = str(e)

            try:
                snap = await gmgn.fetch_token_snapshot(token_mint, credential_slot=0)
                item_report["normalized_snapshot"] = {
                    k: snap.get(k) for k in [
                        "rug_ratio", "max_rug_ratio",
                        "suspected_insider_hold_rate",
                        "is_wash_trading",
                        "rat_trader_amount_rate",
                        "bundler_rate", "max_bundler_rate",
                    ] if k in snap
                }
            except Exception as e:
                item_report["normalized_snapshot_error"] = str(e)

        report["items"].append(item_report)

    summary = {}
    for canonical in FIELD_CANDIDATES:
        summary[canonical] = {
            "trenches_hits": 0, "token_info_hits": 0, "token_security_hits": 0, "normalized_nonnull": 0,
        }

    for item in report["items"]:
        for canonical in FIELD_CANDIDATES:
            if item.get("trenches_field_hits", {}).get(canonical):
                summary[canonical]["trenches_hits"] += 1
            if item.get("token_info_field_hits", {}).get(canonical):
                summary[canonical]["token_info_hits"] += 1
            if item.get("token_security_field_hits", {}).get(canonical):
                summary[canonical]["token_security_hits"] += 1
        snap = item.get("normalized_snapshot") or {}
        if snap.get("rug_ratio") is not None or snap.get("max_rug_ratio") is not None:
            summary["rug_ratio"]["normalized_nonnull"] += 1
        if snap.get("suspected_insider_hold_rate") is not None:
            summary["suspected_insider_hold_rate"]["normalized_nonnull"] += 1
        if snap.get("is_wash_trading") is not None:
            summary["is_wash_trading"]["normalized_nonnull"] += 1

    report["summary"] = summary

    errors = []
    for canonical, s in summary.items():
        evidence = s["trenches_hits"] + s["token_info_hits"] + s["token_security_hits"] + s["normalized_nonnull"]
        if evidence <= 0:
            errors.append(f"No evidence found for {canonical}")

    report["errors"] = errors

    out_path = out_dir / f"gmgn_risk_field_probe_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"[OK] report written: {out_path}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))

    await repo.close()

    if errors:
        raise RuntimeError("; ".join(errors))


if __name__ == "__main__":
    asyncio.run(main())
