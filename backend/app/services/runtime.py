from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..config import Settings, get_settings
from ..database import Database
from .modeling_gate import modeling_readiness


@dataclass(frozen=True, slots=True)
class PreparedAction:
    challenge: str
    expires_at: int
    summary: dict[str, Any]
    can_confirm: bool
    blocker: str | None = None


class RuntimeService:
    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()

    @staticmethod
    def _mask_wallet(address: str | None) -> str | None:
        if not address:
            return None
        if len(address) <= 10:
            return "***"
        return f"{address[:5]}…{address[-5:]}"

    def _unresolved_trade_count(self) -> int:
        row = self.database.fetch_one(
            """
            SELECT COUNT(*) AS count FROM trades
            WHERE journal_state IN (
                    'intent_created','quoting','submission_started','reserved',
                    'submitted','submission_unknown','pending'
                  )
               OR status IN ('created','quoting','submitted','pending','processed')
            """
        )
        return int(row["count"] if row else 0)

    def prepare_live_start(self) -> PreparedAction:
        blocker = None
        unresolved_orders = self._unresolved_trade_count()
        if self.settings.dry_run:
            blocker = "DRY_RUN is enabled"
        elif not self.settings.wallet_public_key:
            blocker = "WALLET_PUBLIC_KEY is missing"
        elif self.settings.trading_provider == "disabled":
            blocker = "trading provider is disabled"
        elif unresolved_orders:
            blocker = "unresolved orders require reconciliation before live trading"
        summary = {
            "wallet": self._mask_wallet(self.settings.wallet_public_key),
            "max_positions": self.settings.max_open_positions,
            "max_daily_loss_percent": self.settings.max_daily_loss_fraction * 100,
            "sol_reserve": self.settings.wallet_sol_reserve,
            "consecutive_loss_limit": self.settings.consecutive_loss_limit,
            "provider": self.settings.trading_provider,
            "unresolved_orders": unresolved_orders,
        }
        return self._prepare("live_start", summary, blocker)

    def confirm_live_start(self, challenge: str) -> dict[str, Any]:
        self._consume("live_start", challenge)
        if self._unresolved_trade_count():
            raise ValueError("unresolved orders require reconciliation before live trading")
        self.database.set_runtime_state("live_trading_enabled", True)
        self.database.audit(category="runtime", action="live_trading_enabled", severity="warning")
        return self.status()

    def stop_live(self) -> dict[str, Any]:
        self.database.set_runtime_state("live_trading_enabled", False)
        self.database.audit(category="runtime", action="live_trading_disabled")
        return self.status()

    def _open_positions(self, scope: str = "all") -> list[dict[str, Any]]:
        if scope not in {"all", "simulation", "live"}:
            raise ValueError("liquidation scope must be all, simulation or live")
        clauses = ["status IN ('opening','open','closing')"]
        parameters: list[Any] = []
        if scope == "live":
            clauses.append("account_kind='live'")
        elif scope == "simulation":
            active = self.database.fetch_one(
                "SELECT id FROM simulation_sessions WHERE status='active' ORDER BY started_at DESC LIMIT 1"
            )
            if not active:
                return []
            clauses.append("account_kind!='live'")
            clauses.append("simulation_session_id=?")
            parameters.append(str(active["id"]))
        where = " AND ".join(clauses)
        return self.database.fetch_all(
            f"""
            SELECT id, token_address, account_kind, invested_usd, status
            FROM positions WHERE {where}
            ORDER BY CASE account_kind WHEN 'live' THEN 0 ELSE 1 END, entry_time
            """,
            tuple(parameters),
        )

    def prepare_liquidation(self, scope: str = "all") -> PreparedAction:
        open_rows = self._open_positions(scope)
        active_job = self.database.get_runtime_state("liquidation_job") or {}
        summary = {
            "scope": scope,
            "position_count": len(open_rows),
            "live_position_count": sum(row["account_kind"] == "live" for row in open_rows),
            "estimated_capital_usd": round(sum(float(row["invested_usd"] or 0) for row in open_rows), 2),
            "execution": "sequential",
        }
        if active_job.get("status") in {"queued", "running"}:
            blocker = "liquidation already in progress"
        else:
            blocker = "no open positions" if not open_rows else None
        return self._prepare(f"liquidate_{scope}", summary, blocker)

    def confirm_liquidation(self, challenge: str, scope: str = "all") -> dict[str, Any]:
        self._consume(f"liquidate_{scope}", challenge)
        open_rows = self._open_positions(scope)
        if not open_rows:
            raise ValueError("no open positions remain")
        job_id = f"liq_{uuid.uuid4().hex}"
        requested_at = int(time.time())
        job = {
            "id": job_id,
            "status": "queued",
            "requested_at": requested_at,
            "mode": "sequential",
            "scope": scope,
            "position_ids": [str(row["id"]) for row in open_rows],
            "total_positions": len(open_rows),
            "results": {},
        }
        if scope in {"all", "live"}:
            # Live liquidation revokes ordinary live execution immediately.
            # Authorized SELL exits can still use the frozen liquidation job id.
            self.database.set_runtime_state("live_trading_enabled", False)
            self.database.set_runtime_state("new_entries_paused", True)
            self.database.set_runtime_state("new_entries_pause_reason", "liquidation_in_progress")
        if scope in {"all", "simulation"}:
            self.database.set_runtime_state("simulation_entries_paused", True)
            self.database.set_runtime_state("simulation_entries_pause_reason", "liquidation_in_progress")
        self.database.set_runtime_state("liquidation_job", job)
        self.database.audit(
            category="trading",
            action="liquidation_queued",
            severity="warning",
            entity_type="liquidation_job",
            entity_id=job_id,
            details={
                "scope": scope,
                "position_count": len(open_rows),
                "live_position_count": sum(row["account_kind"] == "live" for row in open_rows),
            },
        )
        return {"id": job_id, "status": "queued", "execution": "sequential", "scope": scope}

    def collector_events(self, limit: int = 200) -> list[dict[str, Any]]:
        events = self.database.get_runtime_state("collector_events", [])
        if not isinstance(events, list):
            return []
        bounded = max(1, min(int(limit), 250))
        return [dict(item) for item in events[-bounded:] if isinstance(item, dict)]

    def status(self) -> dict[str, Any]:
        return {
            "app_env": self.settings.app_env,
            "dry_run": self.settings.dry_run,
            "simulation_enabled": self.settings.simulation_enabled,
            "modeling": modeling_readiness(self.database, self.settings).as_dict(),
            "live_trading_enabled": bool(self.database.get_runtime_state("live_trading_enabled", False)),
            "trading_provider": self.settings.trading_provider,
            "wallet": self._mask_wallet(self.settings.wallet_public_key),
            "collector": self.database.get_runtime_state("collector_status", {"state": "stopped"}),
            "prediction_worker": self.database.get_runtime_state("prediction_worker_status", {"state": "stopped"}),
            "scheduler": self.database.get_runtime_state("scheduler_status", {"state": "stopped"}),
            "paper_monitor": self.database.get_runtime_state("position_monitor_status", {"state": "stopped"}),
            "position_monitor": self.database.get_runtime_state("position_monitor_status", {"state": "stopped"}),
            "model_health": self.database.get_runtime_state("model_health_status", {"state": "not_evaluated"}),
            "model_health_worker": self.database.get_runtime_state("model_health_worker_status", {"state": "stopped"}),
            "training_worker": self.database.get_runtime_state("training_worker_status", {"state": "stopped"}),
            "reconciliation": self.database.get_runtime_state("reconciliation_status"),
            "reconciliation_worker": self.database.get_runtime_state(
                "reconciliation_worker_status", {"state": "stopped"}
            ),
            "liquidation": self.database.get_runtime_state("liquidation_job"),
            "liquidation_worker": self.database.get_runtime_state(
                "liquidation_worker_status", {"state": "stopped"}
            ),
        }

    def _prepare(self, action: str, summary: dict[str, Any], blocker: str | None) -> PreparedAction:
        challenge = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + self.settings.live_confirmation_ttl_seconds
        self.database.set_runtime_state(
            f"confirmation:{action}",
            {
                "hash": hashlib.sha256(challenge.encode()).hexdigest(),
                "expires_at": expires_at,
                "can_confirm": blocker is None,
            },
        )
        return PreparedAction(challenge, expires_at, summary, blocker is None, blocker)

    def _consume(self, action: str, challenge: str) -> None:
        state = self.database.get_runtime_state(f"confirmation:{action}")
        self.database.set_runtime_state(f"confirmation:{action}", None)
        if not state or not state.get("can_confirm"):
            raise ValueError("action is not confirmable")
        if int(state.get("expires_at", 0)) < int(time.time()):
            raise ValueError("confirmation expired")
        supplied = hashlib.sha256(challenge.encode()).hexdigest()
        if not hmac.compare_digest(str(state.get("hash", "")), supplied):
            raise ValueError("invalid confirmation")
