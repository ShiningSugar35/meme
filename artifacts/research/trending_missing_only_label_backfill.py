from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from backend.app.collector import (
    ApiKeyRoles,
    AsyncRateLimiter,
    CollectedSample,
    CollectorEndpoints,
    GMGNDataClient,
    GMGNEnrichmentProvider,
    HttpxTransport,
    LabelPolicy,
)
from backend.app.collector.labels import LabelFinalizer
from backend.app.config import get_settings
from backend.app.database import Database
from backend.app.services.platform_configuration import ENV_PATH, PlatformConfigurationService

EXPERIMENT_ID = "disc24h_6eca0f7ef9a7"
SOURCES = ("trending:volume", "trending:smart_degen_count", "trending:change5m")
MISSING_ONLY_REASON = ["missing_or_invalid:insider_ratio"]
PROGRESS = ROOT / "artifacts" / "research" / "trending_missing_only_label_backfill_progress.json"
FINAL = ROOT / "artifacts" / "research" / "trending_missing_only_label_backfill_20260903.json"
BACKFILL_WEIGHTED_RPS = 0.30


def _env() -> dict[str, str]:
    return {str(k): str(v) for k, v in dotenv_values(ENV_PATH).items() if v is not None}


def _wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / d
    return [max(0.0, c - h), min(1.0, c + h)]


def _load_progress() -> dict[str, Any]:
    if not PROGRESS.exists():
        return {"experiment_id": EXPERIMENT_ID, "addresses": {}}
    try:
        data = json.loads(PROGRESS.read_text(encoding="utf-8"))
    except Exception:
        return {"experiment_id": EXPERIMENT_ID, "addresses": {}}
    return data if isinstance(data, dict) else {"experiment_id": EXPERIMENT_ID, "addresses": {}}


def _save_progress(data: dict[str, Any]) -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROGRESS)


def _candidates(db: Database) -> tuple[dict[str, list[dict[str, Any]]], int]:
    rows = db.fetch_all(
        """
        SELECT source_key,address,observed_at,source_rank,raw_json,rejection_reasons_json
        FROM discovery_experiment_observations
        WHERE experiment_id=? AND source_kind='trending'
          AND prefilter_accepted=1 AND outcome='enrichment_rejected'
        ORDER BY observed_at ASC
        """,
        (EXPERIMENT_ID,),
    )
    earliest: dict[tuple[str, str], dict[str, Any]] = {}
    not_due = 0
    now = int(time.time())
    policy = LabelPolicy()
    for row in rows:
        try:
            reasons = json.loads(row.get("rejection_reasons_json") or "[]")
            raw = json.loads(row.get("raw_json") or "{}")
        except Exception:
            continue
        if reasons != MISSING_ONLY_REASON:
            continue
        # Historical lifecycle equivalent: launchpad_status 0 is internal / not migrated.
        if str(raw.get("launchpad_status")) != "0":
            continue
        source = str(row["source_key"])
        if source not in SOURCES:
            continue
        price = raw.get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        entry_time = int(row["observed_at"])
        if entry_time + policy.window_seconds > now:
            not_due += 1
            continue
        key = (source, str(row["address"]))
        if key in earliest:
            continue
        earliest[key] = {
            "source_key": source,
            "address": str(row["address"]),
            "entry_time": entry_time,
            "entry_price": price,
            "source_rank": int(row.get("source_rank") or 0) or None,
            "qualification": {
                "insider_ratio": {
                    "source": "gmgn_market_rank",
                    "historical_request_max_insider_rate": 0.2,
                    "predicate": "server_qualified_lte",
                    "note": "Historical formal experiment used inclusive max_insider_rate=0.2; no numeric insider value is fabricated.",
                },
                "lifecycle_scope": {
                    "source": "rank.launchpad_status",
                    "launchpad_status": raw.get("launchpad_status"),
                    "scope": "internal_market_new_creation_or_near_completion",
                },
            },
        }
    by_address: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in earliest.values():
        by_address[item["address"]].append(item)
    return dict(by_address), not_due


def _endpoints(env: dict[str, str]) -> CollectorEndpoints:
    return CollectorEndpoints(
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


def _summary(labels: list[dict[str, Any]]) -> dict[str, Any]:
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in labels:
        by_source[row["source_key"]].append(row)
    result: dict[str, Any] = {}
    for source in SOURCES:
        values = by_source.get(source, [])
        n = len(values)
        pos = sum(int(v["tag"]) for v in values)
        neg = n - pos
        units = 3 * pos - neg
        result[source] = {
            "n": n,
            "positive": pos,
            "negative": neg,
            "positive_rate": pos / n if n else None,
            "wilson95": _wilson(pos, n),
            "profit_units_3_to_1": units,
            "units_per_trade": units / n if n else None,
            "J_trade_all": units / pos if pos else None,
        }
    # Union uses earliest entry among sources per address.
    earliest_by_address: dict[str, dict[str, Any]] = {}
    for row in labels:
        current = earliest_by_address.get(row["address"])
        if current is None or int(row["entry_time"]) < int(current["entry_time"]):
            earliest_by_address[row["address"]] = row
    values = list(earliest_by_address.values())
    n = len(values)
    pos = sum(int(v["tag"]) for v in values)
    neg = n - pos
    units = 3 * pos - neg
    result["trending:union_earliest"] = {
        "n": n,
        "positive": pos,
        "negative": neg,
        "positive_rate": pos / n if n else None,
        "wilson95": _wilson(pos, n),
        "profit_units_3_to_1": units,
        "units_per_trade": units / n if n else None,
        "J_trade_all": units / pos if pos else None,
    }
    return result


async def main() -> None:
    settings = get_settings()
    db = Database(settings.database_path)
    db.initialize()
    candidates, not_due = _candidates(db)
    cfg = PlatformConfigurationService(db)
    roles = ApiKeyRoles.from_secrets(cfg.provider_credentials("gmgn"))
    env = _env()
    transport = HttpxTransport()
    progress = _load_progress()
    progress.setdefault("addresses", {})
    progress["candidate_unique_addresses"] = len(candidates)
    progress["not_due_observation_rows_skipped"] = not_due
    progress["backfill_weighted_rps"] = BACKFILL_WEIGHTED_RPS
    policy = LabelPolicy()
    finalizer = LabelFinalizer(policy)
    try:
        client = GMGNDataClient(
            base_url=env.get("GMGN_API_BASE_URL", ""),
            transport=transport,
            limiter=AsyncRateLimiter(BACKFILL_WEIGHTED_RPS),
            endpoints=_endpoints(env),
        )
        provider = GMGNEnrichmentProvider(client, roles)
        total = len(candidates)
        for index, (address, entries) in enumerate(sorted(candidates.items()), start=1):
            if progress["addresses"].get(address, {}).get("status") == "done":
                continue
            min_entry = min(int(e["entry_time"]) for e in entries)
            max_entry = max(int(e["entry_time"]) for e in entries)
            start = min_entry - policy.history_seconds
            end = max_entry + policy.window_seconds
            record: dict[str, Any] = {"status": "pending", "entries": entries, "labels": []}
            try:
                klines = ()
                for kline_attempt in range(4):
                    try:
                        klines = tuple(await provider.klines(address, start, end))
                        break
                    except Exception:
                        if kline_attempt >= 3:
                            raise
                        cooldown = max(
                            (client.slot_rate_limit_remaining(slot) for slot in roles.kline),
                            default=0.0,
                        )
                        await asyncio.sleep(max(15.0, min(cooldown + 1.0, 330.0)))
                for entry in entries:
                    sample = CollectedSample(
                        address=address,
                        token_type="trending",
                        entry_time=int(entry["entry_time"]),
                        entry_price=float(entry["entry_price"]),
                        launchpad="",
                        liquidity=0.0,
                        features={},
                        source={"historical_missing_only_counterfactual": True, **entry["qualification"]},
                    )
                    label = finalizer.finalize(sample, klines)
                    record["labels"].append({
                        **entry,
                        "tag": int(label.tag),
                        "exit_reason": label.exit_reason,
                        "max_price_ratio": label.max_price_ratio,
                        "min_price_ratio": label.min_price_ratio,
                        "final_close_ratio": label.final_close_ratio,
                        "first_take_profit_at": label.first_take_profit_at,
                        "first_stop_loss_at": label.first_stop_loss_at,
                        "label_version": label.label_version,
                        "kline_count_shared_address_window": len(klines),
                    })
                record["status"] = "done"
            except Exception as exc:
                record["status"] = "error"
                record["error"] = f"{type(exc).__name__}: {exc}"
            progress["addresses"][address] = record
            progress["completed_or_attempted"] = index
            _save_progress(progress)
            if index % 10 == 0 or index == total:
                print(json.dumps({"progress": index, "total": total, "address": address, "status": record["status"]}, ensure_ascii=False), flush=True)
    finally:
        await transport.close()

    labels = [
        label
        for rec in progress["addresses"].values()
        if rec.get("status") == "done"
        for label in rec.get("labels", [])
    ]
    errors = {address: rec.get("error") for address, rec in progress["addresses"].items() if rec.get("status") == "error"}
    output = {
        "experiment_id": EXPERIMENT_ID,
        "method": "missing-only counterfactual: first qualifying observation per source/address; launchpad_status=0; historical server max_insider_rate<=0.2; 1m Kline; 90m first-touch TP1.8/SL0.9",
        "caveat": "This is a counterfactual upper-bound screen because historical top-holder hard gate was never reached and cannot be reconstructed point-in-time. No missing insider numeric value is fabricated.",
        "candidate_unique_addresses": len(candidates),
        "completed_addresses": sum(rec.get("status") == "done" for rec in progress["addresses"].values()),
        "error_count": len(errors),
        "errors": errors,
        "not_due_observation_rows_skipped": not_due,
        "summary": _summary(labels),
        "labels": labels,
    }
    FINAL.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"final": str(FINAL), **{k: v for k, v in output.items() if k != "labels"}}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
