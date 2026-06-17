"""GMGN field probe script — 验证初筛/风控/价格面三条线 API 字段返回完整性.

用法:
    python -m backend.scripts.debug_gmgn_field_probe <TOKEN_MINT> [--mode LIVE|ONLINE_READONLY|SIM|MOCK]

对指定 token 依次执行:
  1. fetch_token_snapshot  → 检查 初筛(entry risk) + 风控(holding risk) 字段
  2. fetch_latest_price    → 检查最新价格字段
  3. fetch_kline (1m,1440) → 检查K线返回, 被价格面(Stage3)用于24h分位
  4. fetch_kline (5m,288)  → 补充K线覆盖
  5. 在本地运行 filter 函数  → 验证能否跑通而不抛异常

输出写入 logs/debug_gmgn_field_probe_<TS>.json。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import ProviderMode, settings
from app.db.repositories import Repositories
from app.providers.gmgn_real import GMGNProvider
from app.strategy.filters import (
    run_entry_local_risk_filter,
    run_holding_risk_filter,
    evaluate_price_activity_rules,
)
from app.strategy.thresholds import compute_thresholds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MOCK_MINTS = {"PASS1", "PASS1_150", "PASS1_510", "FAIL_INIT", "FAIL_SECOND"}


def _check_fields(snapshot: dict, required_map: Dict[str, tuple]) -> dict:
    """Check each required field/alias group; return report."""
    result = {}
    for label, aliases in required_map.items():
        found = {}
        for a in aliases:
            v = snapshot.get(a)
            if v is not None and v != "":
                found[a] = v
        result[label] = {
            "aliases_checked": list(aliases),
            "keys_present_in_snapshot": list(found.keys()),
            "present": len(found) > 0,
            "count_available": len(found),
        }
    return result


def _summarize(result: dict) -> dict:
    present = sum(1 for v in result.values() if v["present"])
    total = len(result)
    return {
        "groups_present": present,
        "groups_total": total,
        "completeness_pct": round(present / total * 100, 1) if total else 0,
        "missing_groups": [k for k, v in result.items() if not v["present"]],
    }


# ---------------------------------------------------------------------------
# Required field maps (from discovery_runner.py / position_risk_runner.py)
# ---------------------------------------------------------------------------

ENTRY_RISK_REQUIRED = {
    "renounced_mint": ("renounced_mint", "mint_renounced", "is_mint_renounced"),
    "renounced_freeze_account": ("renounced_freeze_account", "freeze_renounced",
                                 "is_freeze_renounced", "freeze_authority_renounced"),
    "is_wash_trading": ("is_wash_trading", "wash_trading", "wash_trading_detected"),
    "rat_trader_amount_rate": ("rat_trader_amount_rate", "rat_trader_rate"),
    "suspected_insider_hold_rate": ("suspected_insider_hold_rate", "insider_hold_rate",
                                    "max_insider_ratio"),
    "sell_tax": ("sell_tax", "sell_tax_rate"),
    "burn_status": ("burn_status", "lp_burn_status", "burnt_status"),
    "sniper_count": ("sniper_count", "snipers", "sniper_trader_count"),
    "liquidity_usd": ("liquidity_usd", "liquidity", "pool_liquidity_usd"),
    "holder_count": ("holder_count", "holders", "total_holders", "holder"),
}

HOLDING_RISK_REQUIRED = {
    "liquidity_usd": ("liquidity_usd", "liquidity", "pool_liquidity_usd"),
    "holder_count": ("holder_count", "holders", "total_holders", "holder"),
    "top10_holder_rate": ("top_10_holder_rate", "top10_holder_rate",
                          "top10_holder_percent", "top_10_rate"),
    "fresh_wallet_rate": ("fresh_wallet_rate", "fresh_wallets_rate", "fresh_wallet"),
    "dev_team_hold_rate": ("dev_team_hold_rate", "creator_balance_rate",
                           "creator_hold_rate", "dev_hold_rate"),
    "rug_ratio": ("max_rug_ratio", "rug_ratio", "max_rugged_ratio", "rug"),
    "entrapment_ratio": ("max_entrapment_ratio", "entrapment_ratio", "entrapment"),
    "insider_ratio": ("max_insider_ratio", "insider_ratio", "insider_rate"),
    "max_bundler_rate": ("max_bundler_rate", "bundler_trader_amount_rate",
                         "bundler_rate", "bundler"),
    "suspected_insider_hold_rate": ("suspected_insider_hold_rate", "insider_hold_rate"),
    "is_wash_trading": ("is_wash_trading", "wash_trading",
                        "wash_trading_detected", "is_wash"),
    "rat_trader_amount_rate": ("rat_trader_amount_rate", "rat_trader_rate", "rat_trader"),
    "sniper_count": ("sniper_count", "snipers", "sniper_trader_count", "sniper_cnt"),
}

PRICE_SURFACE_REQUIRED = {
    "price_usd": ("price_usd", "price", "usd_price"),
    "swaps_1h": ("swaps_1h", "swaps1h", "trade_1h", "trades_1h"),
    "volume_1h": ("volume_1h", "volume1h", "volume_1h_usd", "volume_h1"),
    "liquidity_usd": ("liquidity_usd", "liquidity", "pool_liquidity_usd"),
    "creation_time": ("pool_created_at", "creation_time", "created_at",
                      "open_time", "launch_time"),
    "market_cap": ("market_cap", "marketcap", "fdv"),
}

KLINE_FIELDS = ["open", "high", "low", "close", "volume_usd", "open_time"]


# ---------------------------------------------------------------------------
# Probe logic
# ---------------------------------------------------------------------------

async def probe_token(
    token_mint: str,
    provider: GMGNProvider,
    results: dict,
) -> dict:
    print(f"\n{'='*60}")
    print(f"Probing token: {token_mint}")
    print(f"{'='*60}")

    # --- 1. fetch_token_snapshot ---
    print("\n--- [1/5] fetch_token_snapshot ---")
    t0 = time.perf_counter()
    try:
        snapshot = await provider.fetch_token_snapshot(token_mint)
        snap_elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  Elapsed: {snap_elapsed}ms")
        print(f"  Snapshot keys ({len(snapshot)}): {sorted(snapshot.keys())}")
    except Exception as exc:
        snap_elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  FAILED ({snap_elapsed}ms): {exc}")
        snapshot = {}
    results["snapshot"] = {
        "elapsed_ms": snap_elapsed,
        "key_count": len(snapshot),
        "keys": sorted(snapshot.keys()) if snapshot else [],
        "raw_json_length": len(snapshot.get("raw_json", "")),
    }

    # 1a. 初筛 field check
    print("\n  --- 初筛(entry_risk) fields ---")
    entry_check = _check_fields(snapshot, ENTRY_RISK_REQUIRED)
    entry_summary = _summarize(entry_check)
    print(f"  Completeness: {entry_summary['completeness_pct']}% "
          f"({entry_summary['groups_present']}/{entry_summary['groups_total']})")
    if entry_summary["missing_groups"]:
        print(f"  MISSING: {entry_summary['missing_groups']}")
    results["entry_risk_fields"] = {
        "check": entry_check,
        "summary": entry_summary,
    }

    # 1b. 风控 field check
    print("\n  --- 风控(holding_risk) fields ---")
    holding_check = _check_fields(snapshot, HOLDING_RISK_REQUIRED)
    holding_summary = _summarize(holding_check)
    print(f"  Completeness: {holding_summary['completeness_pct']}% "
          f"({holding_summary['groups_present']}/{holding_summary['groups_total']})")
    if holding_summary["missing_groups"]:
        print(f"  MISSING: {holding_summary['missing_groups']}")
    results["holding_risk_fields"] = {
        "check": holding_check,
        "summary": holding_summary,
    }

    # 1c. 价格面 field check (from snapshot)
    print("\n  --- 价格面(price_surface) fields (from snapshot) ---")
    price_check = _check_fields(snapshot, PRICE_SURFACE_REQUIRED)
    price_summary = _summarize(price_check)
    print(f"  Completeness: {price_summary['completeness_pct']}% "
          f"({price_summary['groups_present']}/{price_summary['groups_total']})")
    if price_summary["missing_groups"]:
        print(f"  MISSING: {price_summary['missing_groups']}")
    results["price_surface_fields_snapshot"] = {
        "check": price_check,
        "summary": price_summary,
    }

    # --- 2. fetch_latest_price ---
    print("\n--- [2/5] fetch_latest_price ---")
    t0 = time.perf_counter()
    try:
        latest = await provider.fetch_latest_price(token_mint)
        latest_elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  Elapsed: {latest_elapsed}ms")
        print(f"  Latest keys ({len(latest)}): {sorted(latest.keys())}")

        latest_price = latest.get("data", latest) if isinstance(latest, dict) else latest
        if isinstance(latest_price, dict):
            price_usd = latest_price.get("price_usd") or latest_price.get("price")
            print(f"  price_usd={price_usd}, liquidity_usd={latest_price.get('liquidity_usd')}")
        results["latest_price"] = {
            "elapsed_ms": latest_elapsed,
            "key_count": len(latest),
            "keys": sorted(latest.keys()),
        }
    except Exception as exc:
        latest_elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  FAILED ({latest_elapsed}ms): {exc}")
        results["latest_price"] = {"elapsed_ms": latest_elapsed, "error": str(exc)[:300]}

    # --- 3. fetch_kline (1m, 1440) ---
    print("\n--- [3/5] fetch_kline (1m, 1440) ---")
    t0 = time.perf_counter()
    try:
        klines = await provider.fetch_kline(token_mint, "1m", 1440)
        kline_elapsed = int((time.perf_counter() - t0) * 1000)
        kline_count = len(klines)
        print(f"  Elapsed: {kline_elapsed}ms, count={kline_count}")

        valid_rows = 0
        missing_fields = set()
        if klines:
            first = klines[0]
            print(f"  First kline keys: {sorted(first.keys())}")
            print(f"  First kline: open={first.get('open')}, high={first.get('high')}, "
                  f"low={first.get('low')}, close={first.get('close')}")
            for k in klines:
                for f in KLINE_FIELDS:
                    if f not in k or k.get(f) is None or k.get(f) == "":
                        missing_fields.add(f)
                    else:
                        v = k[f]
                        if isinstance(v, (int, float)) and v > 0:
                            valid_rows += 1
                            break
            print(f"  Valid rows (at least one OHLCV > 0): {valid_rows}")
            if missing_fields:
                print(f"  Fields sometimes missing from individual rows: {sorted(missing_fields)}")
        else:
            print("  No klines returned")

        results["klines"] = {
            "elapsed_ms": kline_elapsed,
            "count": kline_count,
            "valid_rows": valid_rows,
            "missing_fields_in_rows": sorted(missing_fields),
        }
    except Exception as exc:
        kline_elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  FAILED ({kline_elapsed}ms): {exc}")
        results["klines"] = {"elapsed_ms": kline_elapsed, "error": str(exc)[:300]}
        klines = []

    # --- 4. fetch_kline (5m, 288) for extra coverage ---
    print("\n--- [4/5] fetch_kline (5m, 288) ---")
    t0 = time.perf_counter()
    try:
        klines_5m = await provider.fetch_kline(token_mint, "5m", 288)
        kline_5m_elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  Elapsed: {kline_5m_elapsed}ms, count={len(klines_5m)}")
        results["klines_5m"] = {
            "elapsed_ms": kline_5m_elapsed,
            "count": len(klines_5m),
        }
    except Exception as exc:
        kline_5m_elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  FAILED ({kline_5m_elapsed}ms): {exc}")
        results["klines_5m"] = {"elapsed_ms": kline_5m_elapsed, "error": str(exc)[:300]}
        klines_5m = []

    # --- 5. Run filter functions locally ---
    print("\n--- [5/5] Running filter functions ---")

    # 5a. Entry risk filter (初筛)
    print("  --- run_entry_local_risk_filter ---")
    t0 = time.perf_counter()
    strategy_group = {"group_name": "pump_fun", "platforms": ["Pump.fun"], "x": 0.2}
    try:
        entry_result = await run_entry_local_risk_filter(snapshot, strategy_group)
        entry_elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  Elapsed: {entry_elapsed}ms, passed={entry_result.passed}")
        for d in entry_result.details:
            print(f"    {d.name}: passed={d.passed}, value={d.value}, reason={d.reason}")
        results["filter_entry_risk"] = {
            "elapsed_ms": entry_elapsed,
            "passed": entry_result.passed,
            "details": [
                {"name": d.name, "passed": d.passed, "value": str(d.value)[:60],
                 "reason": d.reason, "missing": d.missing}
                for d in entry_result.details
            ],
        }
    except Exception as exc:
        entry_elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  FAILED ({entry_elapsed}ms): {exc}")
        results["filter_entry_risk"] = {"elapsed_ms": entry_elapsed, "error": str(exc)[:300]}

    # 5b. Holding risk filter (风控)
    print("  --- run_holding_risk_filter ---")
    t0 = time.perf_counter()
    try:
        hold_result = await run_holding_risk_filter(snapshot, strategy_group)
        hold_elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  Elapsed: {hold_elapsed}ms, passed={hold_result.passed}")
        for d in hold_result.details:
            print(f"    {d.name}: passed={d.passed}, value={d.value}, "
                  f"threshold={d.threshold}, missing={d.missing}")
        results["filter_holding_risk"] = {
            "elapsed_ms": hold_elapsed,
            "passed": hold_result.passed,
            "details": [
                {"name": d.name, "passed": d.passed, "value": str(d.value)[:60],
                 "threshold": str(d.threshold)[:30], "reason": d.reason, "missing": d.missing}
                for d in hold_result.details
            ],
        }
    except Exception as exc:
        hold_elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  FAILED ({hold_elapsed}ms): {exc}")
        results["filter_holding_risk"] = {"elapsed_ms": hold_elapsed, "error": str(exc)[:300]}

    # 5c. Price activity filter (价格面)
    print("  --- evaluate_price_activity_rules ---")
    t0 = time.perf_counter()
    try:
        snap_price = {
            "price_usd": snapshot.get("price_usd"),
            "swaps_1h": snapshot.get("swaps_1h"),
            "volume_1h": snapshot.get("volume_1h"),
            "liquidity_usd": snapshot.get("liquidity_usd"),
            "pool_created_at": snapshot.get("pool_created_at"),
            "market_cap": snapshot.get("market_cap"),
        }

        price_result = await evaluate_price_activity_rules(
            {}, strategy_group, snap_price, klines=klines
        )
        price_elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  Elapsed: {price_elapsed}ms, passed={price_result.passed}")
        for d in price_result.details:
            print(f"    {d.get('rule')}: passed={d.get('passed')}, "
                  f"value={d.get('pct_change') or d.get('percentile') or d.get('current')}")
        if price_result.feature_vector:
            fv = price_result.feature_vector
            print(f"  Feature vector: kline_api_ok={fv.get('kline_api_ok')}, "
                  f"kline_valid_ohlcv_count={fv.get('kline_valid_ohlcv_count')}, "
                  f"kline_validation_pass={fv.get('kline_validation_pass')}, "
                  f"price_change_source={fv.get('price_change_source')}, "
                  f"age_minutes={fv.get('age_minutes')}")
        results["filter_price_activity"] = {
            "elapsed_ms": price_elapsed,
            "passed": price_result.passed,
            "details": [
                {"name": d.get("rule"), "passed": d.get("passed"),
                 "value": str(d.get("pct_change") or d.get("percentile")
                              or d.get("amount") or d.get("current"))[:60]}
                for d in price_result.details
            ],
            "feature_vector": {
                k: price_result.feature_vector[k]
                for k in ("kline_api_ok", "kline_valid_ohlcv_count",
                          "kline_validation_pass", "price_change_source",
                          "age_minutes", "current_price", "swaps_1h",
                          "volume_1h", "price_range_24h_percentile")
                if k in price_result.feature_vector
            },
        }
    except Exception as exc:
        price_elapsed = int((time.perf_counter() - t0) * 1000)
        import traceback
        print(f"  FAILED ({price_elapsed}ms): {exc}")
        traceback.print_exc()
        results["filter_price_activity"] = {
            "elapsed_ms": price_elapsed, "error": str(exc)[:500],
        }

    return results


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="GMGN field probe — 验证初筛/风控/价格面字段完整性",
    )
    parser.add_argument("token_mint", type=str,
                        help="Token mint address to probe (e.g. PASS1 for mock)")
    parser.add_argument("--mode", type=str, default=None,
                        choices=["LIVE", "ONLINE_READONLY", "SIM", "MOCK"],
                        help="Provider mode override")
    args = parser.parse_args()

    token_mint = args.token_mint
    mode = ProviderMode[args.mode] if args.mode else None

    db_path = getattr(settings, "SQLITE_PATH", "./data/trading_bot.sqlite3")
    repo = await Repositories.create(db_path)
    try:
        provider = GMGNProvider(repo, mode=mode) if mode else GMGNProvider(repo)

        results: Dict[str, Any] = {
            "probed_at": datetime.now(timezone.utc).isoformat(),
            "token_mint": token_mint,
            "provider_mode": str(provider.mode),
            "api_base": provider.api_base_url,
            "credential_count": len(provider.credentials),
            "is_mock": token_mint in MOCK_MINTS or provider.mode == ProviderMode.MOCK,
            "probes": {},
        }

        await probe_token(token_mint, provider, results["probes"])

        # Summary
        print(f"\n{'='*60}")
        print("OVERALL SUMMARY")
        print(f"{'='*60}")
        snap = results["probes"].get("snapshot", {})
        print(f"Snapshot keys: {snap.get('key_count', 0)}")

        for section, label in [
            ("entry_risk_fields", "初筛(entry_risk)"),
            ("holding_risk_fields", "风控(holding_risk)"),
            ("price_surface_fields_snapshot", "价格面(price_surface)"),
        ]:
            s = results["probes"].get(section, {}).get("summary", {})
            print(f"  {label}: {s.get('completeness_pct', 0)}% "
                  f"({s.get('groups_present', 0)}/{s.get('groups_total', 0)})"
                  + (f"  MISSING: {s['missing_groups']}" if s.get("missing_groups") else ""))

        klines = results["probes"].get("klines", {})
        print(f"Klines (1m, 1440): count={klines.get('count', 0)}, "
              f"valid={klines.get('valid_rows', 0)}")

        for filt, label in [
            ("filter_entry_risk", "初筛 filter"),
            ("filter_holding_risk", "风控 filter"),
            ("filter_price_activity", "价格面 filter"),
        ]:
            f = results["probes"].get(filt, {})
            status = "OK" if f.get("passed") is not None else "ERROR"
            print(f"  {label}: {status}, passed={f.get('passed')}")

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_dir = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
        os.makedirs(log_dir, exist_ok=True)
        out_path = os.path.join(log_dir, f"debug_gmgn_field_probe_{ts}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nFull results saved to {out_path}")

    finally:
        await repo.close()


if __name__ == "__main__":
    asyncio.run(main())
