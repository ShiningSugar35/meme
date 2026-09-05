from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.collector.enrichment import recursive_find
from backend.app.services.onchain_admission import classify_swap_transaction
from backend.app.services.solana_rpc import SolanaRpcPool, configured_rpc_endpoints


def load_sample(sample_id: int = 2807):
    db = sqlite3.connect(ROOT / "data" / "meme_quant.db")
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT id,address,entry_time,age_minutes,raw_json FROM samples WHERE id=?", (sample_id,)).fetchone()
    raw = json.loads(row["raw_json"] or "{}")
    pool = str(recursive_find(raw, ("biggest_pool_address", "pool_address", "pair_address", "amm_address", "pool_id")) or "")
    return dict(row), pool


async def fetch(pool: SolanaRpcPool, endpoint, address: str, start: int, end: int, filtered: bool):
    rows = []
    token = None
    for _ in range(3):
        filters = {"status": "succeeded", "blockTime": {"gte": start, "lte": end}}
        if filtered:
            filters["tokenAccounts"] = "balanceChanged"
        config = {
            "transactionDetails": "full",
            "sortOrder": "desc",
            "limit": 100,
            "commitment": "finalized",
            "encoding": "jsonParsed",
            "filters": filters,
        }
        if token:
            config["paginationToken"] = token
        result, _ = await pool._rpc(endpoint, "getTransactionsForAddress", [address, config])
        data = result.get("data") if isinstance(result, dict) else []
        rows.extend(data or [])
        token = result.get("paginationToken") if isinstance(result, dict) else None
        if not token:
            break
    return rows, bool(token)


async def main():
    row, address = load_sample()
    endpoint = [x for x in configured_rpc_endpoints() if x.provider == "alchemy"][0]
    rpc = SolanaRpcPool((endpoint,), timeout_seconds=20.0)
    try:
        end = int(row["entry_time"])
        start = end - min(3600, int(float(row["age_minutes"]) * 60))
        unfiltered, unfiltered_more = await fetch(rpc, endpoint, address, start, end, False)
        filtered, filtered_more = await fetch(rpc, endpoint, address, start, end, True)
        def summary(rows):
            dirs = [classify_swap_transaction(tx, str(row["address"])) for tx in rows]
            return {"rows": len(rows), "buys": sum(x > 0 for x in dirs), "sells": sum(x < 0 for x in dirs), "classified": sum(x != 0 for x in dirs)}
        report = {
            "credentials_redacted": True,
            "sample_id": int(row["id"]),
            "unfiltered": {**summary(unfiltered), "more_pages": unfiltered_more},
            "balance_changed": {**summary(filtered), "more_pages": filtered_more},
        }
        (ROOT / "artifacts" / "research" / "alchemy_token_filter_probe_20260905.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        await rpc.close()


if __name__ == "__main__":
    asyncio.run(main())
