from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings, get_settings
from ..database import Database


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str
    investment_usd: float = 0.0


class RiskService:
    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()

    @staticmethod
    def investment_for_liquidity(liquidity_usd: float) -> float:
        if liquidity_usd <= 0:
            return 0.0
        return round(min(liquidity_usd * 0.01, 50.0), 8)

    def check_new_position(
        self,
        *,
        token_address: str,
        liquidity_usd: float,
        account_kind: str,
        available_usd: float,
        wallet_total_usd: float,
        sol_balance: float | None = None,
    ) -> RiskDecision:
        if self.database.get_runtime_state("new_entries_paused", False):
            return RiskDecision(False, "new_entries_paused")
        if account_kind == "live" and not self.database.get_runtime_state("live_trading_enabled", False):
            return RiskDecision(False, "live_trading_disabled")
        if account_kind == "live" and sol_balance is not None and sol_balance < self.settings.wallet_sol_reserve:
            return RiskDecision(False, "sol_reserve_below_minimum")

        open_count = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM positions WHERE account_kind=? AND status IN ('opening','open','closing')",
            (account_kind,),
        )["count"]
        if int(open_count) >= self.settings.max_open_positions:
            return RiskDecision(False, "max_open_positions")

        if account_kind == "live":
            existing = self.database.fetch_one(
                """
                SELECT id FROM positions
                WHERE token_address=? AND account_kind='live' AND status IN ('opening','open','closing')
                LIMIT 1
                """,
                (token_address,),
            )
            if existing:
                return RiskDecision(False, "duplicate_live_token_position")

        if self._consecutive_live_losses() >= self.settings.consecutive_loss_limit:
            self.pause_new_entries("consecutive_loss_circuit_breaker")
            return RiskDecision(False, "consecutive_loss_circuit_breaker")

        daily_loss = self._daily_live_loss()
        if wallet_total_usd > 0 and daily_loss >= wallet_total_usd * self.settings.max_daily_loss_fraction:
            self.pause_new_entries("daily_loss_circuit_breaker")
            return RiskDecision(False, "daily_loss_circuit_breaker")

        investment = self.investment_for_liquidity(liquidity_usd)
        if investment <= 0:
            return RiskDecision(False, "invalid_liquidity")
        if investment > max(available_usd, 0):
            return RiskDecision(False, "insufficient_available_usd")
        return RiskDecision(True, "allowed", investment)

    def pause_new_entries(self, reason: str) -> None:
        self.database.set_runtime_state("new_entries_paused", True)
        self.database.set_runtime_state("new_entries_pause_reason", reason)
        self.database.audit(category="risk", action="new_entries_paused", severity="warning", details={"reason": reason})

    def resume_new_entries(self) -> None:
        self.database.set_runtime_state("new_entries_paused", False)
        self.database.set_runtime_state("new_entries_pause_reason", None)
        self.database.audit(category="risk", action="new_entries_resumed")

    def status(self) -> dict[str, Any]:
        return {
            "new_entries_paused": bool(self.database.get_runtime_state("new_entries_paused", False)),
            "pause_reason": self.database.get_runtime_state("new_entries_pause_reason"),
            "max_open_positions": self.settings.max_open_positions,
            "open_live_positions": self._open_live_positions(),
            "consecutive_live_losses": self._consecutive_live_losses(),
            "consecutive_loss_limit": self.settings.consecutive_loss_limit,
            "daily_live_loss_usd": self._daily_live_loss(),
            "max_daily_loss_fraction": self.settings.max_daily_loss_fraction,
            "wallet_sol_reserve": self.settings.wallet_sol_reserve,
        }

    def _open_live_positions(self) -> int:
        row = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM positions WHERE account_kind='live' AND status IN ('opening','open','closing')"
        )
        return int(row["count"] if row else 0)

    def _consecutive_live_losses(self) -> int:
        rows = self.database.fetch_all(
            """
            SELECT net_pnl_usd FROM positions
            WHERE account_kind='live' AND status='closed' AND net_pnl_usd IS NOT NULL
            ORDER BY exit_time DESC LIMIT ?
            """,
            (self.settings.consecutive_loss_limit,),
        )
        losses = 0
        for row in rows:
            if float(row["net_pnl_usd"]) < 0:
                losses += 1
            else:
                break
        return losses

    def _daily_live_loss(self) -> float:
        now_local = datetime.now(ZoneInfo(self.settings.training_timezone))
        local_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        utc_start = local_start.astimezone(timezone.utc).isoformat()
        utc_end = (local_start + timedelta(days=1)).astimezone(timezone.utc).isoformat()
        row = self.database.fetch_one(
            """
            SELECT COALESCE(SUM(CASE WHEN net_pnl_usd < 0 THEN -net_pnl_usd ELSE 0 END), 0) AS loss
            FROM positions
            WHERE account_kind='live' AND status='closed' AND exit_time >= ? AND exit_time < ?
            """,
            (utc_start, utc_end),
        )
        return float(row["loss"] if row else 0.0)

