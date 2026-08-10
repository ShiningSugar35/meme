from __future__ import annotations

import json
import uuid
from typing import Any

from ..config import Settings, get_settings
from ..database import Database, utc_now_iso
from ..repositories.models import ModelRepository
from ..risk.service import RiskService
from .dashboard import DashboardService
from .paper_trading import PaperTradingService
from .runtime import RuntimeService
from .training import TrainingService


class AgentService:
    """Read-only Agent context plus human-approved, non-live proposal actions."""

    ALLOWED_PROPOSAL_TYPES = {
        "train_model",
        "rollback_model",
        "reset_simulation",
        "pause_new_entries",
        "resume_new_entries",
    }

    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()

    def get_context(self) -> dict[str, Any]:
        runtime = RuntimeService(self.database, self.settings).status()
        risk = RiskService(self.database, self.settings).status()
        champion = ModelRepository(self.database).champion()
        dashboard = DashboardService(self.database)
        signals = dashboard.list_signals(limit=20)
        positions = dashboard.list_positions(include_closed=False, limit=20)
        audit_logs = self.database.fetch_all(
            "SELECT category, action, severity, created_at FROM audit_logs ORDER BY id DESC LIMIT 10"
        )

        return {
            "access_level": "read_only_and_human_approved_non_live_actions",
            "allowed_proposal_types": sorted(self.ALLOWED_PROPOSAL_TYPES),
            "explicitly_forbidden": [
                "live_buy",
                "live_sell",
                "live_liquidation",
                "wallet_transfer",
                "secret_access",
            ],
            "runtime": runtime,
            "risk": risk,
            "champion_model": champion,
            "open_positions": positions,
            "recent_signals": signals,
            "audit_summary": audit_logs,
        }

    def create_proposal(self, proposal_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        proposal_type = str(proposal_type).strip()
        if proposal_type not in self.ALLOWED_PROPOSAL_TYPES:
            raise ValueError(f"unsupported or unsafe proposal type: {proposal_type}")
        normalized_payload = self._validate_payload(proposal_type, payload)
        proposal_id = str(uuid.uuid4())
        created_at = utc_now_iso()
        self.database.execute(
            """
            INSERT INTO agent_proposals(
                id,proposal_type,payload_json,status,created_at
            ) VALUES(?,?,?,'pending_approval',?)
            """,
            (
                proposal_id,
                proposal_type,
                json.dumps(normalized_payload, ensure_ascii=False, separators=(",", ":")),
                created_at,
            ),
        )
        self.database.audit(
            category="agent",
            action="proposal_created",
            entity_type="proposal",
            entity_id=proposal_id,
            details={"proposal_type": proposal_type},
        )
        record = self.get_proposal(proposal_id)
        assert record is not None
        return record

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT * FROM agent_proposals WHERE id=?", (proposal_id,))
        return self._decode(row) if row else None

    def list_proposals(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT * FROM agent_proposals ORDER BY created_at DESC,id DESC LIMIT ?",
            (limit,),
        )
        return [self._decode(row) for row in rows]

    def reject_proposal(self, proposal_id: str, *, note: str | None = None) -> dict[str, Any]:
        now = utc_now_iso()
        changed = self.database.execute(
            """
            UPDATE agent_proposals
            SET status='rejected',decided_at=?,decision_note=?,error_message=NULL
            WHERE id=? AND status='pending_approval'
            """,
            (now, (note or "")[:500] or None, proposal_id),
        )
        if changed != 1:
            raise ValueError("proposal not found or no longer pending approval")
        self.database.audit(
            category="agent",
            action="proposal_rejected",
            entity_type="proposal",
            entity_id=proposal_id,
        )
        record = self.get_proposal(proposal_id)
        assert record is not None
        return record

    def approve_proposal(self, proposal_id: str, *, note: str | None = None) -> dict[str, Any]:
        proposal = self.get_proposal(proposal_id)
        if not proposal or proposal["status"] != "pending_approval":
            raise ValueError("proposal not found or no longer pending approval")
        now = utc_now_iso()
        changed = self.database.execute(
            """
            UPDATE agent_proposals
            SET status='approved',decided_at=?,decision_note=?
            WHERE id=? AND status='pending_approval'
            """,
            (now, (note or "")[:500] or None, proposal_id),
        )
        if changed != 1:
            raise ValueError("proposal approval raced with another decision")

        try:
            result = self._execute_non_live_action(
                str(proposal["proposal_type"]),
                dict(proposal.get("payload") or {}),
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:1000]
            self.database.execute(
                """
                UPDATE agent_proposals
                SET status='failed',executed_at=?,error_message=?
                WHERE id=? AND status='approved'
                """,
                (utc_now_iso(), message, proposal_id),
            )
            self.database.audit(
                category="agent",
                action="proposal_execution_failed",
                severity="error",
                entity_type="proposal",
                entity_id=proposal_id,
                details={"error": message},
            )
        else:
            self.database.execute(
                """
                UPDATE agent_proposals
                SET status='executed',executed_at=?,result_json=?,error_message=NULL
                WHERE id=? AND status='approved'
                """,
                (
                    utc_now_iso(),
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    proposal_id,
                ),
            )
            self.database.audit(
                category="agent",
                action="proposal_executed",
                entity_type="proposal",
                entity_id=proposal_id,
                details={"proposal_type": proposal["proposal_type"]},
            )
        record = self.get_proposal(proposal_id)
        assert record is not None
        return record

    def _execute_non_live_action(self, proposal_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if proposal_type == "train_model":
            features = payload.get("features")
            run_id = TrainingService(self.database, self.settings).create_run(
                "manual",
                feature_names=features,
            )
            return {"run_id": run_id, "status": "queued"}
        if proposal_type == "rollback_model":
            champion = TrainingService(self.database, self.settings).rollback_model(
                str(payload["model_id"])
            )
            return {"champion_id": champion["id"], "status": "rolled_back"}
        if proposal_type == "reset_simulation":
            status = PaperTradingService(self.database, self.settings).reset_simulation()
            return {"session_id": status["session"]["id"], "status": "reset"}
        if proposal_type == "pause_new_entries":
            reason = str(payload.get("reason") or "agent_proposal_manual_pause")[:200]
            RiskService(self.database, self.settings).pause_new_entries(reason)
            return {"status": "paused", "reason": reason}
        if proposal_type == "resume_new_entries":
            RiskService(self.database, self.settings).resume_new_entries()
            return {"status": "resumed"}
        raise ValueError(f"unsafe proposal type cannot execute: {proposal_type}")

    def _validate_payload(self, proposal_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("proposal payload must be an object")
        if proposal_type == "train_model":
            features = payload.get("features")
            if features is not None:
                if not isinstance(features, list):
                    raise ValueError("train_model features must be a list")
                features = list(TrainingService.normalize_feature_selection(features))
            return {"features": features}
        if proposal_type == "rollback_model":
            model_id = str(payload.get("model_id") or "").strip()
            if not model_id:
                raise ValueError("rollback_model requires model_id")
            return {"model_id": model_id}
        if proposal_type == "pause_new_entries":
            return {"reason": str(payload.get("reason") or "agent_proposal_manual_pause")[:200]}
        if proposal_type in {"reset_simulation", "resume_new_entries"}:
            return {}
        raise ValueError(f"unsupported proposal type: {proposal_type}")

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json") or "{}")
        result["result"] = json.loads(result.pop("result_json") or "null")
        return result
