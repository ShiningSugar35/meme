from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Protocol, Sequence

from ..collector.models import Kline
from ..config import Settings, get_settings
from ..database import Database, utc_now_iso
from .paper_trading import PaperTradingService


class KlineProvider(Protocol):
    async def klines(self, address: str, from_ts: int, to_ts: int) -> Sequence[Kline]: ...


@dataclass(frozen=True, slots=True)
class PaperMonitorCycle:
    checked_positions: int
    market_requests: int
    closed_positions: int
    pending_positions: int
    blocked_positions: int
    open_positions: int
    skipped_liquidation_positions: int
    completed_at: str


class PaperPositionMonitor:
    """Restorable market-driven exit monitor for paper/shadow positions."""

    ACCOUNT_KINDS = ("paper", "shadow_aggressive", "shadow_conservative")

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        paper_service: PaperTradingService | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.paper = paper_service or PaperTradingService(database, self.settings)

    async def run_cycle(
        self,
        provider: KlineProvider,
        *,
        now_ts: int | None = None,
    ) -> PaperMonitorCycle:
        current_ts = int(now_ts or time.time())
        if not self.settings.simulation_enabled or not self.settings.paper_market_monitor_enabled:
            report = PaperMonitorCycle(0, 0, 0, 0, 0, 0, 0, utc_now_iso())
            self.database.set_runtime_state("paper_monitor_status", {"state": "disabled", **asdict(report)})
            return report

        rows = self.database.fetch_all(
            """
            SELECT id, token_address, status, entry_time, expires_at, metadata_json
            FROM positions
            WHERE account_kind IN ('paper','shadow_aggressive','shadow_conservative')
              AND status IN ('open','closing')
            ORDER BY entry_time, id
            """
        )
        liquidation = self.database.get_runtime_state("liquidation_job") or {}
        liquidation_ids = (
            {str(value) for value in liquidation.get("position_ids", [])}
            if liquidation.get("status") in {"queued", "running"}
            else set()
        )

        candidates = [row for row in rows if str(row["id"]) not in liquidation_ids]
        skipped = len(rows) - len(candidates)
        grouped: dict[str, list[dict]] = {}
        direct_retry: list[dict] = []
        for row in candidates:
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if row["status"] == "closing" and isinstance(metadata.get("paper_exit_pending"), dict):
                direct_retry.append(row)
            else:
                grouped.setdefault(str(row["token_address"]), []).append(row)

        closed = pending = blocked = open_count = 0
        checked = 0
        market_requests = 0

        for row in direct_retry:
            result = self.paper.monitor_position(str(row["id"]), (), now_ts=current_ts)
            checked += 1
            closed += int(result.state == "closed")
            pending += int(result.state == "pending")
            blocked += int(result.state == "blocked")
            open_count += int(result.state == "open")

        for address, positions in grouped.items():
            from_ts = min(self._iso_epoch(str(row["entry_time"])) for row in positions)
            latest_needed = max(
                min(current_ts, self._iso_epoch(str(row["expires_at"])))
                for row in positions
            )
            to_ts = max(from_ts + 60, latest_needed)
            try:
                klines = await provider.klines(address, from_ts, to_ts)
                market_requests += 1
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:300]
                for row in positions:
                    blocked += 1
                    checked += 1
                self.database.audit(
                    category="simulation",
                    action="paper_market_data_failed",
                    severity="warning",
                    entity_type="token",
                    entity_id=address,
                    details={"positions": len(positions), "error": message},
                )
                continue

            for row in positions:
                result = self.paper.monitor_position(str(row["id"]), klines, now_ts=current_ts)
                checked += 1
                closed += int(result.state == "closed")
                pending += int(result.state == "pending")
                blocked += int(result.state == "blocked")
                open_count += int(result.state == "open")

        report = PaperMonitorCycle(
            checked_positions=checked,
            market_requests=market_requests,
            closed_positions=closed,
            pending_positions=pending,
            blocked_positions=blocked,
            open_positions=open_count,
            skipped_liquidation_positions=skipped,
            completed_at=utc_now_iso(),
        )
        self.database.set_runtime_state("paper_monitor_status", {"state": "running", **asdict(report)})
        return report

    @staticmethod
    def _iso_epoch(value: str) -> int:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp())
