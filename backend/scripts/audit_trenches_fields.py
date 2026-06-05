"""Audit script: fetch GMGN trenches and verify field mapping.

Run: python -m backend.scripts.audit_trenches_fields
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.app.db.repositories import Repositories
from backend.app.config import settings, ProviderMode
from backend.app.providers.gmgn_real import GMGNProvider
from backend.app.providers.rate_limiter import get_rate_limiter
from backend.app.strategy.filters import (
    run_entry_local_risk_filter,
    _first_present as filters_first_present,
)
from backend.app.strategy.thresholds import build_trench_filters_for_x, KNOWN_TRENCH_FILTER_KEYS

# Fields required by run_entry_local_risk_filter (Stage 0)
REQUIRED_FIELDS = [
    ("renounced_mint", ["renounced_mint", "mint_renounced", "is_mint_renounced"]),
    ("renounced_freeze_account", ["renounced_freeze_account", "freeze_renounced", "is_freeze_renounced", "freeze_authority_renounced"]),
    ("is_wash_trading", ["is_wash_trading", "wash_trading", "wash_trading_detected"]),
    ("rat_trader_amount_rate", ["rat_trader_amount_rate", "rat_trader_rate"]),
    ("suspected_insider_hold_rate", ["suspected_insider_hold_rate", "insider_hold_rate", "max_insider_ratio"]),
    ("sell_tax", ["sell_tax", "sell_tax_rate"]),
    ("burn_status", ["burn_status", "lp_burn_status", "burnt_status"]),
    ("sniper_count", ["sniper_count", "snipers", "sniper_trader_count"]),
]

# Strategy fields used in trenches pushdown
TRENCH_FILTER_FIELDS = KNOWN_TRENCH_FILTER_KEYS + [
    "min_liquidity", "min_marketcap", "min_volume_24h",
]


async def audit():
    db_path = settings.SQLITE_PATH
    if not os.path.exists(db_path):
        db_path = None  # let create() handle default
    repo = await Repositories.create(db_path)

    try:
        gmgn = GMGNProvider(repo, mode=ProviderMode.ONLINE_READONLY)

        # Build a standard trench filter for x=0.2
        filters = build_trench_filters_for_x(0.2)

        params = {
            "chain": "sol",
            "type": "new_creation",
            "platforms": ["Pump.fun", "Moonshot"],
        }
        # Merge filters into params at top level (flattened)
        for k, v in filters.items():
            if k not in ("_x", "_computed_from_x"):
                params[k] = v

        print("=" * 70)
        print("GMGN TRENCHES FIELD AUDIT")
        print("=" * 70)
        print(f"\nRequest params ({len(params)} keys):")
        for k, v in sorted(params.items()):
            print(f"  {k}: {v}")

        items = await gmgn.fetch_trenches(params)
        print(f"\nFetched {len(items)} tokens from trenches")
        print("-" * 70)

        if not items:
            print("NO items returned — cannot audit fields.")
            return

        # Top-level keys across all items
        all_keys = set()
        for item in items:
            all_keys.update(item.keys())

        print(f"\nCombined top-level keys ({len(all_keys)}):")
        for k in sorted(all_keys):
            print(f"  {k}")

        # Check required fields
        print("\n" + "=" * 70)
        print("REQUIRED FIELD CHECK (run_entry_local_risk_filter)")
        print("=" * 70)

        for field_name, aliases in REQUIRED_FIELDS:
            present_in_all = 0
            present_in_any = 0
            missing_samples = []

            for i, item in enumerate(items[:10]):
                matched = False
                for alias in aliases:
                    if alias in item and item[alias] is not None and str(item[alias]).strip():
                        matched = True
                        break
                if matched:
                    present_in_all += 1
                    present_in_any += 1
                else:
                    if len(missing_samples) < 3:
                        missing_samples.append(i)

            status = "OK" if present_in_all == len(items[:10]) else "MISSING"
            print(f"  {field_name:40s} [{status:7s}] in {present_in_all}/{len(items[:10])} first 10 items")
            if missing_samples:
                print(f"    Aliases checked: {aliases}")
                print(f"    Missing in sample indices: {missing_samples}")

        # Show raw items for verification
        print("\n" + "=" * 70)
        print("RAW SAMPLE ITEMS (first 3)")
        print("=" * 70)
        for i, item in enumerate(items[:3]):
            print(f"\n--- Item {i} ---")
            # Show only keys present in normalized output
            item_clean = {k: v for k, v in item.items() if not k.startswith("raw_")}
            print(json.dumps(item_clean, indent=2, ensure_ascii=False, default=str)[:3000])

        # Test run_entry_local_risk_filter on each item
        print("\n" + "=" * 70)
        print("STAGE 0 RISK FILTER TEST")
        print("=" * 70)
        strategy = {"id": 1, "config_version": 1, "x": 0.2, "is_live": False}
        passed_count = 0
        failed_count = 0
        missing_field_items = []

        for i, item in enumerate(items[:10]):
            try:
                result = await run_entry_local_risk_filter(item, strategy)
                if result.passed:
                    passed_count += 1
                else:
                    failed_count += 1
                # Check for missing fields in details
                missing_details = [d for d in result.details if getattr(d, 'missing', False)]
                if missing_details:
                    missing_field_items.append({
                        "index": i,
                        "token": item.get("token_mint", "?"),
                        "missing_fields": [d.name for d in missing_details],
                    })
            except Exception as e:
                missing_field_items.append({
                    "index": i,
                    "token": item.get("token_mint", "?"),
                    "error": str(e),
                })

        print(f"  Passed: {passed_count}, Failed: {failed_count}")
        if missing_field_items:
            print(f"\n  Missing field issues ({len(missing_field_items)}):")
            for m in missing_field_items:
                print(f"    Item {m['index']} ({m['token']}): fields={m.get('missing_fields', [])} error={m.get('error', '')}")
        else:
            print("  No missing field issues in first 10 items!")

        print("\n" + "=" * 70)
        print("AUDIT COMPLETE")
        print("=" * 70)
        print(f"\nTotal items fetched: {len(items)}")
        print(f"Tokens analyzed: {len(items[:10])} (first 10)")

    finally:
        await repo.close()


if __name__ == "__main__":
    asyncio.run(audit())
