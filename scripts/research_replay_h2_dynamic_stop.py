from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import deque
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

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


def replay(entry_price: float, entry_time: int, klines: list[Any]) -> dict[str, Any]:
    end_ts = entry_time + 2 * 60 * 60
    ordered = sorted((k for k in klines if entry_time <= k.timestamp <= end_ts), key=lambda k: k.timestamp)
    if not ordered:
        return {"valid": False, "reason": "no_kline"}

    tp_price = entry_price * 2.0
    hard_sl_price = entry_price * 0.8
    rolling: deque[tuple[int, float]] = deque()
    max_ratio = 0.0
    min_ratio = float("inf")

    for line in ordered:
        high = float(line.high if line.high is not None else line.close or 0.0)
        low = float(line.low if line.low is not None else line.close or 0.0)
        if high <= 0 or low <= 0:
            continue
        max_ratio = max(max_ratio, high / entry_price)
        min_ratio = min(min_ratio, low / entry_price)

        cutoff = line.timestamp - 10 * 60
        while rolling and rolling[0][0] < cutoff:
            rolling.popleft()
        rolling.append((line.timestamp, high))
        rolling_high = max(v for _, v in rolling)

        hard_sl = low <= hard_sl_price
        dynamic_sl = low <= rolling_high * 0.8
        tp = high >= tp_price

        # Conservative one-minute ambiguity rule: any stop in the same candle wins.
        if hard_sl or dynamic_sl:
            return {
                "valid": True,
                "tag": 0,
                "reason": "hard_sl" if hard_sl else "dynamic_sl_10m",
                "exit_ts": line.timestamp,
                "max_ratio": max_ratio,
                "min_ratio": min_ratio,
                "rolling_high_ratio": rolling_high / entry_price,
            }
        if tp:
            return {
                "valid": True,
                "tag": 1,
                "reason": "tp_2x",
                "exit_ts": line.timestamp,
                "max_ratio": max_ratio,
                "min_ratio": min_ratio,
                "rolling_high_ratio": rolling_high / entry_price,
            }

    return {
        "valid": True,
        "tag": 0,
        "reason": "timeout_2h",
        "exit_ts": ordered[-1].timestamp,
        "max_ratio": max_ratio,
        "min_ratio": min_ratio,
        "rolling_high_ratio": None,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rps", type=float, default=1.0)
    parser.add_argument("--out", default="artifacts/research/h2_tp100_sl20_dynamic10m.json")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings.sqlite_path)
    cfg = PlatformConfigurationService(db)
    credentials = cfg.provider_credentials("gmgn")
    roles = ApiKeyRoles.from_secrets(credentials)
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
    limiter = AsyncRateLimiter(args.rps)
    client = GMGNDataClient(base_url=env.get("GMGN_API_BASE_URL", ""), transport=transport, limiter=limiter, endpoints=endpoints)
    provider = GMGNEnrichmentProvider(client, roles, primary_attempts=2, primary_retry_seconds=2.0, fallback_delay_seconds=2.0)

    rows = db.fetch_all(
        """
        SELECT id,address,entry_time,entry_price,tag
        FROM samples
        WHERE label_status='mature' AND tag IN (0,1)
          AND token_type IN ('new_creation','near_completion')
          AND feature_schema_version='event1m_regime_v3'
        ORDER BY entry_time,id
        """
    )

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    started = time.time()
    completed = 0
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(4)

    async def process(row: dict[str, Any]) -> None:
        nonlocal completed
        async with semaphore:
            try:
                entry_time = int(row["entry_time"])
                entry_price = float(row["entry_price"])
                klines = list(await provider.klines(str(row["address"]), entry_time, entry_time + 2 * 60 * 60))
                outcome = replay(entry_price, entry_time, klines)
                async with lock:
                    if not outcome.get("valid"):
                        errors.append({"id": row["id"], "address": row["address"], "error": outcome.get("reason")})
                    else:
                        results.append({"id": row["id"], "address": row["address"], "old_tag": int(row["tag"]), **outcome})
                    completed += 1
                    if completed % 50 == 0 or completed == len(rows):
                        print(json.dumps({"progress": completed, "total": len(rows), "valid": len(results), "errors": len(errors), "elapsed_s": round(time.time() - started, 1)}, ensure_ascii=False), flush=True)
            except Exception as exc:
                async with lock:
                    errors.append({"id": row["id"], "address": row["address"], "error": f"{type(exc).__name__}: {exc}"[:500]})
                    completed += 1
                    if completed % 50 == 0 or completed == len(rows):
                        print(json.dumps({"progress": completed, "total": len(rows), "valid": len(results), "errors": len(errors), "elapsed_s": round(time.time() - started, 1)}, ensure_ascii=False), flush=True)

    await asyncio.gather(*(process(dict(row)) for row in rows))
    await transport.close()

    counts: dict[str, int] = {}
    for item in results:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    positives = sum(int(item["tag"] == 1) for item in results)
    old_positives = sum(int(item["old_tag"] == 1) for item in results)
    changed_0_to_1 = sum(int(item["old_tag"] == 0 and item["tag"] == 1) for item in results)
    changed_1_to_0 = sum(int(item["old_tag"] == 1 and item["tag"] == 0) for item in results)

    summary = {
        "experiment": {
            "window_minutes": 120,
            "take_profit_ratio": 2.0,
            "hard_stop_ratio": 0.8,
            "dynamic_stop": "rolling_10m_high_to_current_low_drawdown_ge_20pct",
            "same_candle_precedence": "stop_before_take_profit",
            "timeout_label": 0,
            "production_mutation": False,
        },
        "requested_samples": len(rows),
        "valid_samples": len(results),
        "error_samples": len(errors),
        "positive": positives,
        "negative": len(results) - positives,
        "positive_rate": positives / len(results) if results else None,
        "old_positive_on_same_valid_subset": old_positives,
        "old_positive_rate_on_same_valid_subset": old_positives / len(results) if results else None,
        "changed_0_to_1": changed_0_to_1,
        "changed_1_to_0": changed_1_to_0,
        "exit_reasons": counts,
        "elapsed_seconds": time.time() - started,
    }
    payload = {"summary": summary, "errors": errors, "results": results}
    out = PROJECT_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
