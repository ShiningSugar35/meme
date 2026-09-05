from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "meme_quant.db"
ROUND1_PATH = ROOT / "artifacts" / "research" / "admission_counterfactual_20260905.json"
ROUND2_PATH = ROOT / "artifacts" / "research" / "onchain_admission_backfill_20260905.json"
CACHE_PATH = ROOT / "artifacts" / "research" / "onchain_admission_backfill_20260905.jsonl"
OUTPUT_PATH = ROOT / "artifacts" / "research" / "onchain_admission_analysis_20260905.json"
CURRENT_LABEL = "sl090_tp180_m90_binary_v5"


def latest_rows() -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for line in CACHE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        rows[int(payload["sample_id"])] = payload
    return rows


def summary(ids: set[int], db_rows: dict[int, dict[str, Any]]) -> dict[str, Any]:
    items = [db_rows[sample_id] for sample_id in sorted(ids) if sample_id in db_rows]
    mature = [
        item for item in items
        if item["label_status"] == "mature"
        and item["tag"] in (0, 1)
        and item["label_version"] == CURRENT_LABEL
    ]
    positives = sum(int(item["tag"] == 1) for item in mature)
    by_type: dict[str, dict[str, Any]] = {}
    for token_type in ("new_creation", "near_completion"):
        typed = [item for item in mature if item["token_type"] == token_type]
        typed_positive = sum(int(item["tag"] == 1) for item in typed)
        by_type[token_type] = {
            "samples": sum(int(item["token_type"] == token_type) for item in items),
            "mature_v5": len(typed),
            "positives_v5": typed_positive,
            "positive_rate_v5": typed_positive / len(typed) if typed else None,
        }
    positive_rate = positives / len(mature) if mature else None
    return {
        "samples": len(items),
        "mature_v5": len(mature),
        "positives_v5": positives,
        "positive_rate_v5": positive_rate,
        "payoff_proxy_units_per_sample_3_to_1": 4.0 * positive_rate - 1.0 if positive_rate is not None else None,
        "by_type": by_type,
    }


def main() -> int:
    round1 = json.loads(ROUND1_PATH.read_text(encoding="utf-8"))
    round2 = json.loads(ROUND2_PATH.read_text(encoding="utf-8"))
    latest = latest_rows()
    first_round_ids = {int(value) for value in round1["deployed_sample_ids"]}
    final_ids = {int(value) for value in round2["final_sample_ids"]}
    swap_fail = {
        sample_id for sample_id, row in latest.items()
        if sample_id in first_round_ids and row.get("buy_ratio") is not None and not row.get("swap_pass")
    }
    swap_unresolved = {
        sample_id for sample_id, row in latest.items()
        if sample_id in first_round_ids and row.get("buy_ratio") is None
    }
    creator_fail = {
        sample_id for sample_id, row in latest.items()
        if sample_id in first_round_ids
        and row.get("swap_pass")
        and row.get("creator_launches_24h") is not None
        and not row.get("creator_pass")
    }
    creator_unresolved = {
        sample_id for sample_id, row in latest.items()
        if sample_id in first_round_ids
        and row.get("swap_pass")
        and row.get("creator_queried")
        and row.get("creator_launches_24h") is None
    }

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in first_round_ids)
    records = connection.execute(
        f"SELECT id,token_type,label_status,tag,label_version FROM samples WHERE id IN ({placeholders})",
        tuple(sorted(first_round_ids)),
    ).fetchall()
    db_rows = {int(row["id"]): dict(row) for row in records}

    baseline = round1["baseline"]
    first_round = summary(first_round_ids, db_rows)
    final = summary(final_ids, db_rows)
    baseline_rate = baseline.get("positive_rate_v5")
    first_rate = first_round.get("positive_rate_v5")
    final_rate = final.get("positive_rate_v5")
    result = {
        "membership_cutoff_utc": round1["policy"]["task_cutoff_iso_utc"],
        "policy": {
            "round1": round1["policy"]["deployed"],
            "baseline_admission": round1["policy"]["baseline"],
            "round1_admission": round1["policy"]["deployed"],
            "buy_swap_ratio_lt": round2["policy"]["buy_swap_ratio_lt"],
            "creator_launches_24h_lt": round2["policy"]["creator_launches_24h_lt"],
        },
        "baseline": baseline,
        "round1": first_round,
        "round2": final,
        "round1_fraction_of_baseline": len(first_round_ids) / int(baseline["samples"]),
        "round2_fraction_of_baseline": len(final_ids) / int(baseline["samples"]),
        "round2_fraction_of_round1": len(final_ids) / len(first_round_ids),
        "positive_rate_change_percentage_points": {
            "round1_minus_baseline": (first_rate - baseline_rate) * 100 if first_rate is not None and baseline_rate is not None else None,
            "round2_minus_round1": (final_rate - first_rate) * 100 if final_rate is not None and first_rate is not None else None,
            "round2_minus_baseline": (final_rate - baseline_rate) * 100 if final_rate is not None and baseline_rate is not None else None,
        },
        "rejections": {
            "buy_ratio_fail": summary(swap_fail, db_rows),
            "buy_ratio_unresolved": summary(swap_unresolved, db_rows),
            "creator_launches_fail": summary(creator_fail, db_rows),
            "creator_launches_unresolved": summary(creator_unresolved, db_rows),
        },
        "rpc_evidence": {
            key: round2[key]
            for key in (
                "alchemy_keys_used",
                "swap_workers_per_key",
                "creator_workers_per_key",
                "rpc_start_interval_seconds_account_wide",
                "unique_pools",
                "swap_query_clusters",
                "unique_creators",
                "creator_query_clusters",
                "swap",
                "creator",
                "gmgn_rpc_buy_ratio_validation",
                "errors",
            )
            if key in round2
        },
        "notes": [
            "Sample membership is frozen at task cutoff by entry_time; label maturity is read at analysis time.",
            "All round-2 historical chain facts are queried through Alchemy mainnet RPC/enhanced RPC, not GMGN and not Ankr.",
            "Historical creator-launch RPC classification is a conservative Solana proxy: creator must be a top-level signer and the transaction must contain initializeMint/initializeMint2. It can include non-launchpad mint creation when program identity is unavailable; production live admission prefers GMGN created_tokens/create_timestamp and uses this RPC path only as fallback.",
            "Payoff proxy uses the project's +3/-1 binary-label utility and is descriptive, not realized execution PnL.",
        ],
    }
    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "round1": result["round1"],
        "round2": result["round2"],
        "fractions": {
            "round2_of_baseline": result["round2_fraction_of_baseline"],
            "round2_of_round1": result["round2_fraction_of_round1"],
        },
        "rate_changes_pp": result["positive_rate_change_percentage_points"],
        "rejections": result["rejections"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
