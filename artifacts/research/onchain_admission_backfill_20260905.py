from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.collector.enrichment import recursive_find
from backend.app.collector.filters import to_float
from backend.app.services.onchain_admission import classify_swap_transaction, is_creator_launch_transaction
from backend.app.services.solana_rpc import SolanaRpcPool, configured_rpc_endpoints

INPUT_REPORT = PROJECT_ROOT / "artifacts" / "research" / "admission_counterfactual_20260905.json"
CACHE_PATH = PROJECT_ROOT / "artifacts" / "research" / "onchain_admission_backfill_20260905.jsonl"
RESULT_PATH = PROJECT_ROOT / "artifacts" / "research" / "onchain_admission_backfill_20260905.json"
CURRENT_LABEL = "sl090_tp180_m90_binary_v5"
MAX_BUY_RATIO = 0.95
MAX_LAUNCHES = 20
SWAP_MAX_PAGES = 100
CREATOR_MAX_PAGES = 2_000
CREATOR_CLUSTER_MAX_SPAN_SECONDS = 86_400
SWAP_WORKERS_PER_ALCHEMY = 20
CREATOR_WORKERS_PER_ALCHEMY = 12
RPC_REQUEST_INTERVAL_SECONDS_ACCOUNT = 0.34


class RpcStartGate:
    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = float(interval_seconds)
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            if self._next_at > now:
                await asyncio.sleep(self._next_at - now)
                now = loop.time()
            self._next_at = max(now, self._next_at) + self.interval_seconds


class ThrottledSolanaRpcPool(SolanaRpcPool):
    def __init__(self, *args, start_gate: RpcStartGate, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._start_gate = start_gate

    async def _rpc(self, endpoint, method: str, params: list[Any]):
        await self._start_gate.acquire()
        return await super()._rpc(endpoint, method, params)


@dataclass(slots=True)
class ResultRow:
    sample_id: int
    address_prefix: str
    provider: str = ""
    swap_transactions: int = 0
    swap_classified: int = 0
    rpc_buys: int = 0
    rpc_sells: int = 0
    buy_ratio: float | None = None
    swap_exhaustive: bool = False
    gmgn_buy_ratio: float | None = None
    swap_pass: bool = False
    creator_queried: bool = False
    creator_transactions: int = 0
    creator_launches_24h: int | None = None
    creator_exhaustive: bool = False
    creator_pass: bool = False
    final_pass: bool = False
    error: str | None = None


def _load_samples() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    admission = json.loads(INPUT_REPORT.read_text(encoding="utf-8"))
    ids = [int(value) for value in admission["deployed_sample_ids"]]
    connection = sqlite3.connect(PROJECT_ROOT / "data" / "meme_quant.db")
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT id,address,entry_time,age_minutes,raw_json,label_status,tag,label_version,token_type FROM samples WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    by_id = {int(row["id"]): dict(row) for row in rows}
    result: list[dict[str, Any]] = []
    for sample_id in ids:
        row = by_id[sample_id]
        raw = json.loads(row.get("raw_json") or "{}")
        pool_address = str(recursive_find(raw, ("biggest_pool_address", "pool_address", "pair_address", "amm_address", "pool_id")) or "")
        creator = str(recursive_find(raw, ("creator_address", "creator", "owner")) or "")
        gmgn_buys = to_float(recursive_find(raw, ("buys_1h", "buy_1h", "buy_count_1h")))
        gmgn_swaps = to_float(recursive_find(raw, ("swaps_1h", "swaps1h", "trade_1h", "trades_1h")))
        gmgn_ratio = gmgn_buys / gmgn_swaps if gmgn_buys is not None and gmgn_swaps and 0 <= gmgn_buys <= gmgn_swaps else None
        result.append({**row, "raw": raw, "pool_address": pool_address, "creator": creator, "gmgn_ratio": gmgn_ratio})
    return result, admission


def _load_cache() -> dict[int, ResultRow]:
    cached: dict[int, ResultRow] = {}
    if not CACHE_PATH.exists():
        return cached
    for line in CACHE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = ResultRow(**json.loads(line))
            if row.error is None:
                cached[row.sample_id] = row
        except Exception:
            continue
    return cached


def _append_cache(row: ResultRow) -> None:
    with CACHE_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(asdict(row), ensure_ascii=False, separators=(",", ":")) + "\n")


def _swap_window(sample: dict[str, Any]) -> tuple[int, int]:
    entry_time = int(sample["entry_time"])
    lookback = min(3600, max(1, int(float(sample["age_minutes"]) * 60)))
    return entry_time - lookback, entry_time


def _pool_clusters(samples: list[dict[str, Any]]) -> list[tuple[str, str, list[dict[str, Any]], int, int]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault((str(sample["pool_address"]), str(sample["address"])), []).append(sample)
    clusters: list[tuple[str, str, list[dict[str, Any]], int, int]] = []
    for (pool_address, token_mint), items in grouped.items():
        ordered = sorted(items, key=lambda item: _swap_window(item)[0])
        current: list[dict[str, Any]] = []
        current_start = current_end = 0
        for item in ordered:
            start_ts, end_ts = _swap_window(item)
            if current and start_ts > current_end:
                clusters.append((pool_address, token_mint, current, current_start, current_end))
                current = []
            if not current:
                current_start = start_ts
                current_end = end_ts
            else:
                current_start = min(current_start, start_ts)
                current_end = max(current_end, end_ts)
            current.append(item)
        if current:
            clusters.append((pool_address, token_mint, current, current_start, current_end))
    return clusters


async def _swap_for_cluster(
    work: tuple[str, str, list[dict[str, Any]], int, int],
    pool: SolanaRpcPool,
) -> list[ResultRow]:
    pool_address, token_mint, samples, start_ts, end_ts = work
    try:
        rows, provider, exhaustive = await pool.transactions_for_address(
            pool_address,
            start_ts=start_ts,
            end_ts=end_ts,
            max_pages=SWAP_MAX_PAGES,
        )
        timed_rows: list[tuple[int, dict[str, Any]]] = []
        missing_block_time = False
        for tx in rows:
            try:
                block_time = int(tx.get("blockTime"))
            except (AttributeError, TypeError, ValueError):
                missing_block_time = True
                continue
            timed_rows.append((block_time, tx))
        results: list[ResultRow] = []
        resolved_exhaustive = exhaustive and not missing_block_time
        for sample in samples:
            sample_start, sample_end = _swap_window(sample)
            relevant = [tx for block_time, tx in timed_rows if sample_start <= block_time <= sample_end]
            directions = [classify_swap_transaction(tx, token_mint) for tx in relevant]
            buys = sum(direction > 0 for direction in directions)
            sells = sum(direction < 0 for direction in directions)
            total = buys + sells
            row = ResultRow(
                sample_id=int(sample["id"]),
                address_prefix=token_mint[:8],
                provider=provider,
                swap_transactions=len(relevant),
                swap_classified=total,
                rpc_buys=buys,
                rpc_sells=sells,
                buy_ratio=(buys / total if resolved_exhaustive and total else None),
                swap_exhaustive=resolved_exhaustive,
                gmgn_buy_ratio=sample.get("gmgn_ratio"),
            )
            row.swap_pass = row.buy_ratio is not None and row.buy_ratio < MAX_BUY_RATIO
            results.append(row)
        return results
    except Exception as exc:
        return [
            ResultRow(
                sample_id=int(sample["id"]),
                address_prefix=token_mint[:8],
                gmgn_buy_ratio=sample.get("gmgn_ratio"),
                error=type(exc).__name__,
            )
            for sample in samples
        ]


def _creator_clusters(samples: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]], int, int]]:
    by_creator: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        by_creator.setdefault(str(sample["creator"]), []).append(sample)
    clusters: list[tuple[str, list[dict[str, Any]], int, int]] = []
    for creator, items in by_creator.items():
        ordered = sorted(items, key=lambda item: int(item["entry_time"]))
        current: list[dict[str, Any]] = []
        current_end = 0
        first_entry = 0
        for item in ordered:
            entry = int(item["entry_time"])
            start = entry - 86400
            if current and (
                start > current_end
                or entry - first_entry > CREATOR_CLUSTER_MAX_SPAN_SECONDS
            ):
                clusters.append((creator, current, int(current[0]["entry_time"]) - 86400, current_end))
                current = []
            if not current:
                first_entry = entry
            current.append(item)
            current_end = max(current_end, entry)
        if current:
            clusters.append((creator, current, int(current[0]["entry_time"]) - 86400, current_end))
    return clusters


async def _creator_for_cluster(
    work: tuple[str, list[dict[str, Any]], int, int],
    pool: SolanaRpcPool,
) -> list[tuple[int, int | None, int, bool, str | None]]:
    creator, samples, start_ts, end_ts = work
    if not creator:
        return [(int(sample["id"]), None, 0, False, None) for sample in samples]
    try:
        states = {
            int(sample["id"]): {
                "entry": int(sample["entry_time"]),
                "start": int(sample["entry_time"]) - 86400,
                "count": 0,
                "threshold_fail": False,
                "window_complete": False,
            }
            for sample in samples
        }
        launch_time_missing = False

        def stop_after_page(page: Sequence[Mapping[str, Any]]) -> bool:
            nonlocal launch_time_missing
            page_times: list[int] = []
            for tx in page:
                block_time = tx.get("blockTime")
                try:
                    timestamp = int(block_time)
                    page_times.append(timestamp)
                except (TypeError, ValueError):
                    if is_creator_launch_transaction(tx, creator):
                        launch_time_missing = True
                    continue
                if not is_creator_launch_transaction(tx, creator):
                    continue
                for state in states.values():
                    if state["start"] <= timestamp <= state["entry"]:
                        state["count"] += 1
                        if state["count"] >= MAX_LAUNCHES:
                            state["threshold_fail"] = True
            if page_times and not launch_time_missing:
                oldest = min(page_times)
                for state in states.values():
                    if not state["threshold_fail"] and oldest < state["start"]:
                        state["window_complete"] = True
            return all(state["threshold_fail"] or state["window_complete"] for state in states.values())

        rows, provider, exhaustive = await pool.transactions_for_address(
            creator,
            start_ts=start_ts,
            end_ts=end_ts,
            max_pages=CREATOR_MAX_PAGES,
            stop_after_page=stop_after_page,
        )
        if exhaustive and not launch_time_missing:
            for state in states.values():
                if not state["threshold_fail"]:
                    state["window_complete"] = True
        result: list[tuple[int, int | None, int, bool, str | None]] = []
        for sample in samples:
            state = states[int(sample["id"])]
            resolved = bool(state["threshold_fail"] or state["window_complete"])
            result.append((
                int(sample["id"]),
                int(state["count"]) if resolved else None,
                len(rows),
                bool(state["window_complete"]),
                provider,
            ))
        return result
    except Exception:
        raise


def _wilson(successes: int, total: int, z: float = 1.959963984540054) -> list[float] | None:
    if total <= 0:
        return None
    p = successes / total
    z2 = z * z
    d = 1 + z2 / total
    center = (p + z2 / (2 * total)) / d
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total) / d
    return [max(0.0, center - margin), min(1.0, center + margin)]


async def main() -> int:
    samples, admission = _load_samples()
    cached = _load_cache()
    by_id = {int(sample["id"]): sample for sample in samples}
    endpoints = [endpoint for endpoint in configured_rpc_endpoints() if endpoint.provider == "alchemy"][:3]
    if len(endpoints) < 3:
        raise RuntimeError(f"need_three_alchemy_endpoints:{len(endpoints)}")

    rpc_start_gate = RpcStartGate(RPC_REQUEST_INTERVAL_SECONDS_ACCOUNT)
    remaining_swap_samples = [sample for sample in samples if int(sample["id"]) not in cached]
    swap_clusters = _pool_clusters(remaining_swap_samples)
    swap_queue: asyncio.Queue[tuple[str, str, list[dict[str, Any]], int, int]] = asyncio.Queue()
    for work in swap_clusters:
        swap_queue.put_nowait(work)
    swap_done = len(cached)
    swap_lock = asyncio.Lock()

    async def swap_worker(endpoint_index: int) -> None:
        nonlocal swap_done
        pool = ThrottledSolanaRpcPool((endpoints[endpoint_index],), timeout_seconds=25.0, circuit_seconds=5.0, start_gate=rpc_start_gate)
        try:
            while True:
                try:
                    work = swap_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                rows = await _swap_for_cluster(work, pool)
                for retry in range(2):
                    if all(row.error is None for row in rows):
                        break
                    await asyncio.sleep(6.0 * (retry + 1))
                    rows = await _swap_for_cluster(work, pool)
                completed_now = 0
                for row in rows:
                    if row.error is None:
                        cached[row.sample_id] = row
                        _append_cache(row)
                    completed_now += 1
                async with swap_lock:
                    before = swap_done
                    swap_done += completed_now
                    if swap_done // 100 != before // 100 or swap_done == len(samples):
                        print(json.dumps({"stage": "swap", "completed": swap_done, "total": len(samples)}, ensure_ascii=False), flush=True)
                swap_queue.task_done()
        finally:
            await pool.close()

    await asyncio.gather(*(
        swap_worker(endpoint_index)
        for endpoint_index in range(len(endpoints))
        for _ in range(SWAP_WORKERS_PER_ALCHEMY)
    ))

    missing_swap_ids = [
        int(sample["id"])
        for sample in samples
        if int(sample["id"]) not in cached
    ]
    if missing_swap_ids:
        raise RuntimeError(f"swap_backfill_incomplete:{len(missing_swap_ids)}")

    swap_pass_samples = [
        sample for sample in samples
        if (cached.get(int(sample["id"])) is not None and cached[int(sample["id"])].swap_pass)
    ]
    clusters = _creator_clusters([
        sample for sample in swap_pass_samples
        if (
            not cached[int(sample["id"])].creator_queried
            or cached[int(sample["id"])].creator_launches_24h is None
        )
    ])
    creator_queue: asyncio.Queue[tuple[str, list[dict[str, Any]], int, int]] = asyncio.Queue()
    for work in clusters:
        creator_queue.put_nowait(work)
    creator_done = 0
    creator_total = sum(len(work[1]) for work in clusters)
    creator_lock = asyncio.Lock()

    async def creator_worker(endpoint_index: int) -> None:
        nonlocal creator_done
        pool = ThrottledSolanaRpcPool((endpoints[endpoint_index],), timeout_seconds=25.0, circuit_seconds=5.0, start_gate=rpc_start_gate)
        try:
            while True:
                try:
                    work = creator_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                results = None
                for retry in range(5):
                    try:
                        results = await _creator_for_cluster(work, pool)
                        break
                    except Exception:
                        await asyncio.sleep(5.0 * (retry + 1))
                if results is None:
                    creator_queue.task_done()
                    continue
                for sample_id, launch_count, tx_count, exhaustive, provider in results:
                    row = cached[sample_id]
                    row.creator_queried = True
                    row.creator_transactions = tx_count
                    row.creator_launches_24h = launch_count
                    row.creator_exhaustive = exhaustive
                    row.creator_pass = launch_count is not None and launch_count < MAX_LAUNCHES
                    row.final_pass = row.swap_pass and row.creator_pass
                    if provider:
                        row.provider = provider
                    _append_cache(row)
                async with creator_lock:
                    creator_done += len(results)
                    if creator_done % 100 < len(results) or creator_done == creator_total:
                        print(json.dumps({"stage": "creator", "completed": creator_done, "total": creator_total}, ensure_ascii=False), flush=True)
                creator_queue.task_done()
        finally:
            await pool.close()

    await asyncio.gather(*(
        creator_worker(endpoint_index)
        for endpoint_index in range(len(endpoints))
        for _ in range(CREATOR_WORKERS_PER_ALCHEMY)
    ))

    unresolved_creator_ids = [
        int(sample["id"])
        for sample in swap_pass_samples
        if cached[int(sample["id"])].creator_launches_24h is None
    ]
    if unresolved_creator_ids:
        raise RuntimeError(f"creator_backfill_incomplete:{len(unresolved_creator_ids)}")

    ordered = [cached[int(sample["id"])] for sample in samples if int(sample["id"]) in cached]
    final_ids = {row.sample_id for row in ordered if row.final_pass}
    mature = [
        by_id[sample_id]
        for sample_id in final_ids
        if by_id[sample_id]["label_status"] == "mature"
        and by_id[sample_id]["tag"] in (0, 1)
        and by_id[sample_id]["label_version"] == CURRENT_LABEL
    ]
    positives = sum(int(item["tag"] == 1) for item in mature)
    deltas = [
        abs(float(row.buy_ratio) - float(row.gmgn_buy_ratio))
        for row in ordered
        if row.buy_ratio is not None and row.gmgn_buy_ratio is not None
    ]
    summary = {
        "credentials_redacted": True,
        "alchemy_keys_used": len(endpoints),
        "swap_workers_per_key": SWAP_WORKERS_PER_ALCHEMY,
        "creator_workers_per_key": CREATOR_WORKERS_PER_ALCHEMY,
        "rpc_start_interval_seconds_account": RPC_REQUEST_INTERVAL_SECONDS_ACCOUNT,
        "policy": {
            "buy_swap_ratio_lt": MAX_BUY_RATIO,
            "creator_launches_24h_lt": MAX_LAUNCHES,
            "swap_rpc_max_pages": SWAP_MAX_PAGES,
            "creator_rpc_max_pages": CREATOR_MAX_PAGES,
        },
        "first_round_samples": len(samples),
        "original_samples": int(admission["baseline"]["samples"]),
        "rpc_rows_completed": len(ordered),
        "unique_pools": len({str(sample["pool_address"]) for sample in samples}),
        "swap_query_clusters": len(_pool_clusters(samples)),
        "unique_creators": len({str(sample["creator"]) for sample in samples}),
        "creator_query_clusters": len(_creator_clusters(swap_pass_samples)),
        "swap": {
            "pass": sum(row.swap_pass for row in ordered),
            "fail": sum(row.buy_ratio is not None and not row.swap_pass for row in ordered),
            "unresolved": sum(row.buy_ratio is None for row in ordered),
            "exhaustive": sum(row.swap_exhaustive for row in ordered),
            "classified_transactions": sum(row.swap_classified for row in ordered),
            "raw_transactions": sum(row.swap_transactions for row in ordered),
        },
        "creator": {
            "queried": sum(row.creator_queried for row in ordered),
            "pass": sum(row.creator_pass for row in ordered),
            "fail": sum(row.creator_launches_24h is not None and not row.creator_pass for row in ordered if row.creator_queried),
            "unresolved": sum(row.creator_queried and row.creator_launches_24h is None for row in ordered),
            "exhaustive": sum(row.creator_exhaustive for row in ordered),
        },
        "final": {
            "samples": len(final_ids),
            "fraction_of_original": len(final_ids) / int(admission["baseline"]["samples"]),
            "fraction_of_first_round": len(final_ids) / len(samples) if samples else None,
            "mature_v5": len(mature),
            "positives_v5": positives,
            "positive_rate_v5": positives / len(mature) if mature else None,
            "positive_rate_wilson95_v5": _wilson(positives, len(mature)),
        },
        "gmgn_rpc_buy_ratio_validation": {
            "paired": len(deltas),
            "absolute_error_mean": mean(deltas) if deltas else None,
            "absolute_error_median": median(deltas) if deltas else None,
            "within_0_05": sum(delta <= 0.05 for delta in deltas) / len(deltas) if deltas else None,
        },
        "errors": {name: sum(row.error == name for row in ordered) for name in sorted({row.error for row in ordered if row.error})},
        "final_sample_ids": sorted(final_ids),
    }
    RESULT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("first_round_samples", "original_samples", "swap", "creator", "final", "gmgn_rpc_buy_ratio_validation", "errors")}, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

