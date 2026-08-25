from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.migrate_h1_no_completed import _build_provider

DB_PATH = PROJECT_ROOT / "data/meme_quant.db"
CACHE_PATH = PROJECT_ROOT / "artifacts/experiments/replay_2h_tp100_sl20_dyn10m_cache.jsonl"
RESULT_PATH = PROJECT_ROOT / "artifacts/experiments/replay_2h_tp100_sl20_dyn10m_result.json"

TP_MULT = 2.0
HARD_SL_MULT = 0.8
WINDOW_SECONDS = 2 * 60 * 60
ROLLING_SECONDS = 10 * 60
ROLLING_DRAWDOWN_MULT = 0.8


def _load_rows() -> list[sqlite3.Row]:
    db = sqlite3.connect(f"file:{DB_PATH.resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """
        SELECT id,address,entry_time,entry_price,tag
        FROM samples
        WHERE feature_schema_version='event1m_regime_v3'
          AND label_status='mature'
          AND tag IN (0,1)
          AND token_type IN ('new_creation','near_completion')
        ORDER BY entry_time,id
        """
    ).fetchall()
    db.close()
    return rows


def _load_cache() -> dict[int, dict]:
    result: dict[int, dict] = {}
    if not CACHE_PATH.exists():
        return result
    for line in CACHE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        result[int(item["id"])] = item
    return result


def _replay(entry_price: float, entry_ts: int, klines) -> dict:
    bars = [k for k in klines if entry_ts <= k.timestamp <= entry_ts + WINDOW_SECONDS]
    bars.sort(key=lambda k: k.timestamp)
    recent_highs: deque[tuple[int, float]] = deque()
    exit_reason = "timeout_2h"
    exit_at = entry_ts + WINDOW_SECONDS

    for k in bars:
        if k.high is None or k.low is None:
            continue
        high = float(k.high)
        low = float(k.low)
        ts = int(k.timestamp)

        cutoff = ts - ROLLING_SECONDS
        while recent_highs and recent_highs[0][0] < cutoff:
            recent_highs.popleft()
        rolling_peak = max((v for _, v in recent_highs), default=None)

        hard_hit = low <= entry_price * HARD_SL_MULT
        dynamic_hit = rolling_peak is not None and low <= rolling_peak * ROLLING_DRAWDOWN_MULT
        tp_hit = high >= entry_price * TP_MULT

        if hard_hit or dynamic_hit:
            exit_reason = "hard_sl_20" if hard_hit else "dynamic_10m_drawdown_20"
            exit_at = ts
            break
        if tp_hit:
            exit_reason = "tp_100"
            exit_at = ts
            break

        # Only completed prior bars enter the trailing 10-minute reference window.
        recent_highs.append((ts, high))

    return {
        "positive": exit_reason == "tp_100",
        "exit_reason": exit_reason,
        "exit_at": exit_at,
        "bars": len(bars),
    }


async def main() -> None:
    rows = _load_rows()
    cache = _load_cache()
    provider, transport = _build_provider()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with CACHE_PATH.open("a", encoding="utf-8") as handle:
            for idx, row in enumerate(rows, 1):
                sample_id = int(row["id"])
                if sample_id in cache:
                    continue
                entry_ts = int(row["entry_time"])
                entry_price = float(row["entry_price"])
                item = {
                    "id": sample_id,
                    "address": row["address"],
                    "entry_time": entry_ts,
                    "entry_price": entry_price,
                    "old_tag": int(row["tag"]),
                }
                try:
                    klines = await provider.klines(str(row["address"]), entry_ts, entry_ts + WINDOW_SECONDS)
                    item.update(_replay(entry_price, entry_ts, klines))
                    item["status"] = "ok"
                except Exception as exc:
                    item.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"[:500]})
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                handle.flush()
                cache[sample_id] = item
                if idx % 50 == 0:
                    ok = sum(v.get("status") == "ok" for v in cache.values())
                    pos = sum(v.get("status") == "ok" and v.get("positive") for v in cache.values())
                    print(json.dumps({"progress": idx, "total": len(rows), "ok": ok, "positive": pos}), flush=True)
    finally:
        await transport.close()

    final = [cache[int(r["id"])] for r in rows if int(r["id"]) in cache]
    ok = [x for x in final if x.get("status") == "ok"]
    errors = [x for x in final if x.get("status") != "ok"]
    reasons: dict[str, int] = {}
    for x in ok:
        reasons[x["exit_reason"]] = reasons.get(x["exit_reason"], 0) + 1
    old_positive = sum(int(r["tag"]) == 1 for r in rows)
    new_positive = sum(bool(x.get("positive")) for x in ok)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule": {
            "window_minutes": 120,
            "tp_multiple": TP_MULT,
            "hard_sl_multiple": HARD_SL_MULT,
            "dynamic_stop": "rolling_10m_peak_to_current_low_drawdown >=20%",
            "same_bar": "loss_first",
        },
        "dataset_total_mature": len(rows),
        "old_positive": old_positive,
        "old_positive_rate": old_positive / len(rows) if rows else None,
        "replayed_ok": len(ok),
        "errors": len(errors),
        "new_positive": new_positive,
        "new_positive_rate": new_positive / len(ok) if ok else None,
        "exit_reasons": reasons,
        "label_transitions": {
            "old0_new1": sum(x["old_tag"] == 0 and x.get("positive") for x in ok),
            "old1_new0": sum(x["old_tag"] == 1 and not x.get("positive") for x in ok),
            "old1_new1": sum(x["old_tag"] == 1 and x.get("positive") for x in ok),
            "old0_new0": sum(x["old_tag"] == 0 and not x.get("positive") for x in ok),
        },
        "error_examples": errors[:20],
    }
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
