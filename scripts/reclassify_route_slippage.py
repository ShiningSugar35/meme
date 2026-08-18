from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "meme_quant.db"
METRIC_VERSION = "jupiter_route_price_impact_v2"


def decode_json(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reclassify route-validated paper sell slippage to Jupiter priceImpact without changing PnL."
    )
    parser.add_argument("--apply", action="store_true", help="persist the correction; default is dry-run")
    args = parser.parse_args()

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT t.id,t.requested_amount,t.slippage_bps,t.slippage_cost_usd,t.response_json,
               p.metadata_json
        FROM trades t
        JOIN positions p ON p.id=t.position_id
        WHERE t.account_kind='simulation' AND t.side='sell' AND t.status='confirmed'
        ORDER BY t.created_at,t.id
        """
    ).fetchall()

    corrections: list[tuple[float, float, str, str]] = []
    before_total = 0.0
    after_total = 0.0
    skipped = 0
    for row in rows:
        metadata = decode_json(row["metadata_json"])
        route = metadata.get("exit_route_probe")
        if not isinstance(route, dict) or route.get("state") != "quoted":
            skipped += 1
            continue
        price_impact = route.get("price_impact_pct")
        if price_impact is None:
            skipped += 1
            continue
        try:
            price_impact_ratio = max(0.0, float(price_impact))
        except (TypeError, ValueError):
            skipped += 1
            continue

        old_bps = float(row["slippage_bps"] or 0.0)
        old_cost = float(row["slippage_cost_usd"] or 0.0)
        requested = max(0.0, float(row["requested_amount"] or 0.0))
        new_bps = int(round(price_impact_ratio * 10_000.0))
        new_cost = requested * price_impact_ratio
        response = decode_json(row["response_json"])
        response.setdefault("legacy_execution_deviation_bps", old_bps)
        response.setdefault("legacy_execution_deviation_cost_usd", old_cost)
        response["slippage_metric_version"] = METRIC_VERSION
        response["jupiter_price_impact_ratio"] = price_impact_ratio
        before_total += old_cost
        after_total += new_cost
        corrections.append(
            (
                new_bps,
                new_cost,
                json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                str(row["id"]),
            )
        )

    if args.apply and corrections:
        with connection:
            connection.executemany(
                """
                UPDATE trades
                SET slippage_bps=?,slippage_cost_usd=?,response_json=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                corrections,
            )

    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "eligible_route_validated_sells": len(corrections),
                "skipped_non_route_or_missing_impact": skipped,
                "recorded_slippage_before_usd": before_total,
                "route_price_impact_after_usd": after_total,
                "reclassified_usd": before_total - after_total,
                "pnl_changed": False,
                "metric_version": METRIC_VERSION,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
