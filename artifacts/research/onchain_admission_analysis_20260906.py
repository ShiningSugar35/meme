from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from statistics import mean, median
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADMISSION_PATH = PROJECT_ROOT / "artifacts" / "research" / "admission_counterfactual_20260905.json"
CACHE_PATH = PROJECT_ROOT / "artifacts" / "research" / "onchain_admission_backfill_20260905.jsonl"
OUTPUT_PATH = PROJECT_ROOT / "artifacts" / "research" / "onchain_admission_analysis_20260906.json"
CURRENT_LABEL = "sl090_tp180_m90_binary_v5"


def _wilson(successes: int, total: int, z: float = 1.959963984540054) -> list[float] | None:
    if total <= 0:
        return None
    p = successes / total
    z2 = z * z
    denominator = 1 + z2 / total
    center = (p + z2 / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total) / denominator
    return [max(0.0, center - margin), min(1.0, center + margin)]


def _latest_cache() -> dict[int, dict[str, Any]]:
    latest: dict[int, dict[str, Any]] = {}
    for raw in CACHE_PATH.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            latest[int(row["sample_id"])] = row
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return latest


def build_report(database_path: Path) -> dict[str, Any]:
    admission = json.loads(ADMISSION_PATH.read_text(encoding="utf-8"))
    latest = _latest_cache()
    first_round_ids = {int(value) for value in admission["deployed_sample_ids"]}
    rows = [latest[sample_id] for sample_id in sorted(first_round_ids) if sample_id in latest]
    final_ids = [int(row["sample_id"]) for row in rows if bool(row.get("final_pass"))]
    unresolved_ids = [
        int(row["sample_id"])
        for row in rows
        if bool(row.get("swap_pass")) and row.get("creator_launches_24h") is None
    ]

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        mature_tags: list[int] = []
        for sample_id in final_ids:
            row = connection.execute(
                "SELECT tag,label_status,label_version FROM samples WHERE id=?",
                (sample_id,),
            ).fetchone()
            if (
                row is not None
                and row["label_status"] == "mature"
                and row["tag"] in (0, 1)
                and row["label_version"] == CURRENT_LABEL
            ):
                mature_tags.append(int(row["tag"]))
    finally:
        connection.close()

    positives = sum(mature_tags)
    paired_deltas = [
        abs(float(row["buy_ratio"]) - float(row["gmgn_buy_ratio"]))
        for row in rows
        if row.get("buy_ratio") is not None and row.get("gmgn_buy_ratio") is not None
    ]
    original_samples = int(admission["baseline"]["samples"])
    first_round_samples = len(first_round_ids)
    final_samples = len(final_ids)
    first_round_rate = float(admission["deployed_liquidity_gt5000_age_gt5"]["positive_rate_v5"])
    final_rate = positives / len(mature_tags) if mature_tags else None

    return {
        "generated_from": {
            "admission_report": str(ADMISSION_PATH.relative_to(PROJECT_ROOT)),
            "rpc_cache": str(CACHE_PATH.relative_to(PROJECT_ROOT)),
            "database": str(database_path),
            "credentials_redacted": True,
        },
        "policy": {
            "marketcap_gt": 5000.0,
            "liquidity_gt": 5000.0,
            "age_minutes_gt": 5.0,
            "age_minutes_lt": 300.0,
            "buy_swap_ratio_1h_lt": 0.95,
            "creator_launches_24h_lt": 20,
            "historical_unresolved_policy": "fail_closed",
            "creator_origin_semantics": "Solana creation transaction fee payer/originating signer proxy; no EVM tx.origin",
        },
        "baseline": admission["baseline"],
        "first_round": {
            "samples": first_round_samples,
            "fraction_of_original": first_round_samples / original_samples,
            "mature_v5": int(admission["deployed_liquidity_gt5000_age_gt5"]["mature_v5"]),
            "positives_v5": int(admission["deployed_liquidity_gt5000_age_gt5"]["positives_v5"]),
            "positive_rate_v5": first_round_rate,
        },
        "rpc_second_round": {
            "rows_with_swap_result": sum(row.get("buy_ratio") is not None for row in rows),
            "swap_pass": sum(bool(row.get("swap_pass")) for row in rows),
            "swap_fail": sum(row.get("buy_ratio") is not None and not bool(row.get("swap_pass")) for row in rows),
            "creator_resolved": sum(row.get("creator_launches_24h") is not None for row in rows),
            "creator_pass": sum(bool(row.get("creator_pass")) for row in rows),
            "creator_fail": sum(
                row.get("creator_launches_24h") is not None and not bool(row.get("creator_pass"))
                for row in rows
            ),
            "creator_unresolved_fail_closed": len(unresolved_ids),
            "final_samples": final_samples,
            "fraction_of_original": final_samples / original_samples,
            "fraction_of_first_round": final_samples / first_round_samples if first_round_samples else None,
            "mature_v5": len(mature_tags),
            "positives_v5": positives,
            "positive_rate_v5": final_rate,
            "positive_rate_wilson95_v5": _wilson(positives, len(mature_tags)),
            "positive_rate_change_pp_vs_first_round": (
                (final_rate - first_round_rate) * 100.0 if final_rate is not None else None
            ),
            "positive_rate_change_pp_vs_original": (
                (final_rate - float(admission["baseline"]["positive_rate_v5"])) * 100.0
                if final_rate is not None
                else None
            ),
        },
        "gmgn_vs_rpc_buy_ratio": {
            "paired": len(paired_deltas),
            "absolute_error_mean": mean(paired_deltas) if paired_deltas else None,
            "absolute_error_median": median(paired_deltas) if paired_deltas else None,
            "within_0_05": (
                sum(delta <= 0.05 for delta in paired_deltas) / len(paired_deltas)
                if paired_deltas
                else None
            ),
        },
        "limitations": [
            "15 first-round samples could not prove a complete 24h creator window within the bounded Alchemy archival budget and are excluded fail-closed.",
            "The historical creator rule is a conservative Solana signer/fee-payer proxy for the user's tx.origin intent; Solana has no EVM tx.origin field.",
            "This report is retrospective rule analysis only and was not used to tune against the frozen final holdout.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "data" / "meme_quant.db",
        help="Pre-reset database or verified backup containing the frozen historical labels.",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = build_report(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report["rpc_second_round"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
