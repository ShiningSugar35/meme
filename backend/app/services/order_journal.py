from __future__ import annotations

import json
import uuid
from typing import Any

from ..database import Database, utc_now_iso
from ..trading.live.errors import LiveTradeError
from ..trading.live.journal import OrderRecord
from ..trading.live.models import (
    ExecutionResult,
    FailureKind,
    OrderSnapshot,
    OrderStatus,
    Submission,
    SwapIntent,
)


class SqliteOrderJournal:
    """Durable idempotency journal used by the live execution state machine."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def reserve(self, intent: SwapIntent) -> OrderRecord:
        fingerprint = intent.fingerprint()
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM trades WHERE client_order_id=?", (intent.client_order_id,)
            ).fetchone()
            if existing:
                record = self._row_to_record(dict(existing))
                if record.fingerprint != fingerprint:
                    raise LiveTradeError(
                        "client_order_id is already bound to a different trade intent",
                        kind=FailureKind.VALIDATION,
                        code="IDEMPOTENCY_CONFLICT",
                    )
                return record
            now = utc_now_iso()
            connection.execute(
                """
                INSERT INTO trades(
                    id, position_id, client_order_id, intent_fingerprint, journal_state,
                    side, account_kind, status, requested_amount, attempt_count,
                    request_json, created_at, updated_at
                ) VALUES(?,?,?,?,? ,?,?,?,?,? ,?,?,?)
                """,
                (
                    str(uuid.uuid4()),
                    intent.metadata.get("position_id"),
                    intent.client_order_id,
                    fingerprint,
                    "intent_created",
                    intent.side.value,
                    str(intent.metadata.get("account_kind") or "live"),
                    "created",
                    float(intent.input_amount_raw),
                    0,
                    json.dumps(
                        {
                            "chain": intent.chain,
                            "input_token": intent.input_token,
                            "output_token": intent.output_token,
                            "wallet": _mask(intent.wallet_address),
                        },
                        separators=(",", ":"),
                    ),
                    now,
                    now,
                ),
            )
        return OrderRecord(intent.client_order_id, fingerprint, "reserved")

    async def get(self, client_order_id: str) -> OrderRecord | None:
        row = self.database.fetch_one("SELECT * FROM trades WHERE client_order_id=?", (client_order_id,))
        return self._row_to_record(row) if row else None

    async def mark_quoting(self, client_order_id: str, attempt_index: int) -> OrderRecord:
        self.database.execute(
            """
            UPDATE trades
            SET journal_state='quoting', status='quoting', attempt_count=?,
                failure_category=NULL, failure_code=NULL, failure_message=NULL, updated_at=?
            WHERE client_order_id=?
            """,
            (attempt_index + 1, utc_now_iso(), client_order_id),
        )
        return await self._required(client_order_id)

    async def mark_submission_started(self, client_order_id: str, attempt_index: int) -> OrderRecord:
        self.database.execute(
            """
            UPDATE trades
            SET journal_state='submission_started', status='submitted', attempt_count=?,
                failure_category='pending', failure_code='SUBMISSION_STARTED', updated_at=?
            WHERE client_order_id=?
            """,
            (attempt_index + 1, utc_now_iso(), client_order_id),
        )
        return await self._required(client_order_id)

    async def mark_submitted(
        self,
        client_order_id: str,
        submission: Submission,
        attempt_index: int,
    ) -> OrderRecord:
        self.database.execute(
            """
            UPDATE trades SET journal_state='submitted', provider_order_id=?, transaction_hash=?,
                status=?, attempt_count=?, response_json=?, updated_at=? WHERE client_order_id=?
            """,
            (
                submission.order_id,
                submission.tx_hash,
                _db_status(submission.status),
                attempt_index + 1,
                json.dumps(dict(submission.raw), ensure_ascii=False, separators=(",", ":")),
                utc_now_iso(),
                client_order_id,
            ),
        )
        return await self._required(client_order_id)

    async def mark_retryable_terminal(
        self,
        client_order_id: str,
        snapshot: OrderSnapshot,
    ) -> OrderRecord:
        self.database.audit(
            category="trading",
            action="retryable_terminal_attempt",
            entity_type="client_order",
            entity_id=client_order_id,
            severity="warning",
            details={"order_id": snapshot.order_id, "status": snapshot.status.value, "code": snapshot.error_code},
        )
        self.database.execute(
            """
            UPDATE trades SET journal_state='retryable_terminal', provider_order_id=NULL,
                status=?, failure_category=?, failure_code=?, failure_message=?, updated_at=?
            WHERE client_order_id=?
            """,
            (
                _db_status(snapshot.status),
                snapshot.failure_kind.value if snapshot.failure_kind else None,
                snapshot.error_code,
                (snapshot.error_message or "")[:500],
                utc_now_iso(),
                client_order_id,
            ),
        )
        return await self._required(client_order_id)

    async def mark_result(self, client_order_id: str, result: ExecutionResult) -> OrderRecord:
        state = "terminal" if result.status.terminal else "pending"
        payload = _result_to_json(result)
        self.database.execute(
            """
            UPDATE trades SET journal_state=?, provider_order_id=COALESCE(?,provider_order_id),
                transaction_hash=COALESCE(?,transaction_hash), status=?, attempt_count=?,
                failure_category=?, failure_code=?, result_json=?, updated_at=?
            WHERE client_order_id=?
            """,
            (
                state,
                result.order_id,
                result.tx_hash,
                _db_status(result.status),
                result.attempts,
                result.failure_kind.value if result.failure_kind else None,
                result.error_code,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                utc_now_iso(),
                client_order_id,
            ),
        )
        return await self._required(client_order_id)

    async def mark_submission_unknown(self, client_order_id: str, attempt_index: int) -> OrderRecord:
        self.database.execute(
            """
            UPDATE trades SET journal_state='submission_unknown', status='pending', attempt_count=?,
                failure_category='pending', failure_code='SUBMISSION_UNKNOWN', updated_at=?
            WHERE client_order_id=?
            """,
            (attempt_index + 1, utc_now_iso(), client_order_id),
        )
        return await self._required(client_order_id)

    async def _required(self, client_order_id: str) -> OrderRecord:
        record = await self.get(client_order_id)
        if record is None:
            raise KeyError(client_order_id)
        return record

    @staticmethod
    def _row_to_record(row: dict[str, Any]) -> OrderRecord:
        status = OrderStatus(row["status"]) if row["status"] in {item.value for item in OrderStatus} else OrderStatus.UNKNOWN
        result = None
        if row.get("result_json"):
            data = json.loads(row["result_json"])
            result = ExecutionResult(
                client_order_id=data["client_order_id"],
                order_id=data.get("order_id"),
                status=OrderStatus(data["status"]),
                attempts=int(data["attempts"]),
                tx_hash=data.get("tx_hash"),
                failure_kind=FailureKind(data["failure_kind"]) if data.get("failure_kind") else None,
                error_code=data.get("error_code"),
                report=data.get("report") or {},
            )
        return OrderRecord(
            client_order_id=row["client_order_id"],
            fingerprint=row["intent_fingerprint"],
            state=row["journal_state"],
            order_id=row.get("provider_order_id"),
            attempt_index=max(0, int(row.get("attempt_count") or 0) - 1),
            status=status,
            result=result,
        )


def _result_to_json(result: ExecutionResult) -> dict[str, Any]:
    return {
        "client_order_id": result.client_order_id,
        "order_id": result.order_id,
        "status": result.status.value,
        "attempts": result.attempts,
        "tx_hash": result.tx_hash,
        "failure_kind": result.failure_kind.value if result.failure_kind else None,
        "error_code": result.error_code,
        "report": dict(result.report),
    }


def _db_status(status: OrderStatus) -> str:
    return "pending" if status is OrderStatus.UNKNOWN else status.value


def _mask(value: str) -> str:
    return f"{value[:5]}…{value[-5:]}" if len(value) > 12 else "***"

