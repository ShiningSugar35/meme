from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import Counter
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "meme_quant.db"


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * q) - 1))
    return ordered[index]


def main() -> None:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()

    session = cursor.execute(
        "SELECT id, started_at FROM simulation_sessions WHERE status='active' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if session is None:
        raise SystemExit("no active simulation session")
    session_id = str(session["id"])

    rows = cursor.execute(
        """
        SELECT * FROM positions
        WHERE strategy_key='rules_only' AND simulation_session_id=? AND status='closed'
        ORDER BY exit_time, id
        """,
        (session_id,),
    ).fetchall()

    cost_rows = cursor.execute(
        """
        SELECT side,
               COUNT(*) AS trade_count,
               SUM(COALESCE(slippage_cost_usd,0)) AS recorded_slippage_usd,
               SUM(COALESCE(platform_fee_usd,0)) AS platform_fee_usd,
               SUM(COALESCE(network_fee_usd,0)) AS network_fee_usd
        FROM trades
        WHERE strategy_key='rules_only' AND simulation_session_id=?
        GROUP BY side ORDER BY side
        """,
        (session_id,),
    ).fetchall()

    reasons: Counter[str] = Counter()
    quote_sources: Counter[str] = Counter()
    accounting_diffs: list[float] = []
    stop_rows: list[dict[str, object]] = []
    stop_net: list[float] = []
    stop_exit_ratios: list[float] = []
    stop_delays: list[float] = []
    recorded_exit_slippage_usd = 0.0
    route_price_impact_usd = 0.0
    market_gap_usd = 0.0
    route_impact_observations = 0

    for row in rows:
        position_id = str(row["id"])
        reason = str(row["exit_reason"] or "unknown")
        reasons[reason] += 1
        trades = cursor.execute(
            """
            SELECT side,status,requested_amount,
                   COALESCE(platform_fee_usd,0) AS platform_fee_usd,
                   COALESCE(network_fee_usd,0) AS network_fee_usd,
                   COALESCE(slippage_cost_usd,0) AS slippage_cost_usd,
                   COALESCE(slippage_bps,0) AS slippage_bps
            FROM trades WHERE position_id=?
            ORDER BY created_at, id
            """,
            (position_id,),
        ).fetchall()
        paid_fees = sum(float(item["platform_fee_usd"]) + float(item["network_fee_usd"]) for item in trades)
        recomputed_net = (
            float(row["token_amount"] or 0.0) * float(row["exit_price"] or 0.0)
            - float(row["invested_usd"] or 0.0)
            - paid_fees
        )
        stored_net = float(row["net_pnl_usd"] or 0.0)
        accounting_diffs.append(stored_net - recomputed_net)

        if reason != "stop_loss_0_9x":
            continue

        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        entry_price = float(row["entry_price"] or 0.0)
        exit_price = float(row["exit_price"] or 0.0)
        exit_ratio = exit_price / entry_price if entry_price > 0 else 0.0
        stop_exit_ratios.append(exit_ratio)
        stop_net.append(stored_net)
        source = str(metadata.get("exit_quote_source") or "unknown")
        quote_sources[source] += 1
        delay_ms = metadata.get("exit_execution_delay_ms")
        if delay_ms is not None:
            stop_delays.append(float(delay_ms))

        route_probe = metadata.get("exit_route_probe") if isinstance(metadata, dict) else None
        route_probe = route_probe if isinstance(route_probe, dict) else {}
        price_impact_ratio = route_probe.get("price_impact_pct")
        try:
            price_impact_ratio_f = max(0.0, float(price_impact_ratio)) if price_impact_ratio is not None else None
        except (TypeError, ValueError):
            price_impact_ratio_f = None

        confirmed_sells = [
            item for item in trades if item["side"] == "sell" and item["status"] == "confirmed"
        ]
        recorded_exit_cost = sum(float(item["slippage_cost_usd"] or 0.0) for item in confirmed_sells)
        recorded_exit_bps = (
            float(confirmed_sells[-1]["slippage_bps"] or 0.0) if confirmed_sells else 0.0
        )
        recorded_exit_slippage_usd += recorded_exit_cost

        execution_deviation_bps = float(metadata.get("exit_execution_deviation_bps") or 0.0)
        denominator = 1.0 + execution_deviation_bps / 10_000.0
        trigger_reference_price = exit_price / denominator if denominator > 0 else None
        stop_price = float(row["stop_loss_price"] or 0.0)
        trigger_over_entry = (
            trigger_reference_price / entry_price
            if trigger_reference_price is not None and entry_price > 0
            else None
        )
        gap_below_stop = (
            max(0.0, (stop_price - trigger_reference_price) / stop_price)
            if trigger_reference_price is not None and stop_price > 0
            else None
        )

        if price_impact_ratio_f is not None and confirmed_sells:
            route_impact_observations += 1
            route_cost = sum(
                max(0.0, float(item["requested_amount"] or 0.0)) * price_impact_ratio_f
                for item in confirmed_sells
            )
            route_price_impact_usd += route_cost
        else:
            route_cost = None
        if gap_below_stop is not None:
            market_gap_usd += gap_below_stop * float(row["invested_usd"] or 0.0)

        stop_rows.append(
            {
                "id": position_id,
                "net_pnl_usd": stored_net,
                "gross_pnl_usd": float(row["gross_pnl_usd"] or 0.0),
                "exit_over_entry": exit_ratio,
                "recorded_exit_slippage_bps": recorded_exit_bps,
                "route_price_impact_ratio": price_impact_ratio_f,
                "recorded_exit_slippage_usd": recorded_exit_cost,
                "route_price_impact_usd": route_cost,
                "execution_deviation_bps": execution_deviation_bps,
                "trigger_reference_price": trigger_reference_price,
                "trigger_over_entry": trigger_over_entry,
                "gap_below_stop_ratio": gap_below_stop,
                "quote_source": source,
                "execution_delay_ms": float(delay_ms) if delay_ms is not None else None,
            }
        )

    bad_accounting = [value for value in accounting_diffs if abs(value) > 1e-6]
    total_net = sum(float(row["net_pnl_usd"] or 0.0) for row in rows)
    total_recorded_slippage = sum(float(row["recorded_slippage_usd"] or 0.0) for row in cost_rows)
    total_platform = sum(float(row["platform_fee_usd"] or 0.0) for row in cost_rows)
    total_network = sum(float(row["network_fee_usd"] or 0.0) for row in cost_rows)

    report = {
        "session_id": session_id,
        "session_started_at": session["started_at"],
        "closed_positions": len(rows),
        "exit_reasons": dict(reasons),
        "total_net_pnl_usd": total_net,
        "trade_costs_by_side": [dict(row) for row in cost_rows],
        "total_recorded_slippage_usd": total_recorded_slippage,
        "total_platform_fee_usd": total_platform,
        "total_network_fee_usd": total_network,
        "accounting_check": {
            "mismatch_count": len(bad_accounting),
            "max_abs_difference_usd": max((abs(value) for value in accounting_diffs), default=0.0),
            "formula": "token_amount*exit_price - invested_usd - sum(platform_fee_usd+network_fee_usd)",
            "slippage_is_not_subtracted_again": len(bad_accounting) == 0,
        },
        "stop_loss": {
            "count": len(stop_rows),
            "avg_net_pnl_usd": statistics.fmean(stop_net) if stop_net else None,
            "min_net_pnl_usd": min(stop_net) if stop_net else None,
            "median_exit_over_entry": statistics.median(stop_exit_ratios) if stop_exit_ratios else None,
            "p10_exit_over_entry": percentile(stop_exit_ratios, 0.10),
            "quote_sources": dict(quote_sources),
            "median_execution_delay_ms": statistics.median(stop_delays) if stop_delays else None,
            "p95_execution_delay_ms": percentile(stop_delays, 0.95),
            "recorded_exit_slippage_usd": recorded_exit_slippage_usd,
            "route_price_impact_observations": route_impact_observations,
            "estimated_route_price_impact_usd": route_price_impact_usd,
            "estimated_market_gap_at_first_observation_usd": market_gap_usd,
            "worst_12": sorted(stop_rows, key=lambda item: float(item["net_pnl_usd"]))[:12],
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
