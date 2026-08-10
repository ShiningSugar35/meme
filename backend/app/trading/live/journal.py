"""Reference in-memory idempotency journal.

Production persistence can implement the same protocol with SQLite.  The
execution engine never depends on a database implementation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from .errors import LiveTradeError
from .models import ExecutionResult, FailureKind, OrderSnapshot, OrderStatus, Submission, SwapIntent


@dataclass(frozen=True, slots=True)
class OrderRecord:
    client_order_id: str
    fingerprint: str
    state: str
    order_id: str | None = None
    attempt_index: int = 0
    status: OrderStatus = OrderStatus.UNKNOWN
    result: ExecutionResult | None = None


class InMemoryOrderJournal:
    def __init__(self) -> None:
        self._records: dict[str, OrderRecord] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, intent: SwapIntent) -> OrderRecord:
        async with self._lock:
            existing = self._records.get(intent.client_order_id)
            fingerprint = intent.fingerprint()
            if existing:
                if existing.fingerprint != fingerprint:
                    raise LiveTradeError(
                        "client_order_id is already bound to a different trade intent",
                        kind=FailureKind.VALIDATION,
                        code="IDEMPOTENCY_CONFLICT",
                    )
                return existing
            record = OrderRecord(intent.client_order_id, fingerprint, "intent_created")
            self._records[intent.client_order_id] = record
            return record

    async def get(self, client_order_id: str) -> OrderRecord | None:
        async with self._lock:
            return self._records.get(client_order_id)

    async def mark_quoting(self, client_order_id: str, attempt_index: int) -> OrderRecord:
        async with self._lock:
            record = self._records[client_order_id]
            updated = replace(
                record,
                state="quoting",
                attempt_index=attempt_index,
                status=OrderStatus.UNKNOWN,
            )
            self._records[client_order_id] = updated
            return updated

    async def mark_submission_started(self, client_order_id: str, attempt_index: int) -> OrderRecord:
        async with self._lock:
            record = self._records[client_order_id]
            updated = replace(
                record,
                state="submission_started",
                attempt_index=attempt_index,
                status=OrderStatus.PENDING,
            )
            self._records[client_order_id] = updated
            return updated

    async def mark_submitted(
        self,
        client_order_id: str,
        submission: Submission,
        attempt_index: int,
    ) -> OrderRecord:
        async with self._lock:
            record = self._records[client_order_id]
            updated = replace(
                record,
                state="submitted",
                order_id=submission.order_id,
                attempt_index=attempt_index,
                status=submission.status,
            )
            self._records[client_order_id] = updated
            return updated

    async def mark_retryable_terminal(
        self,
        client_order_id: str,
        snapshot: OrderSnapshot,
    ) -> OrderRecord:
        async with self._lock:
            record = self._records[client_order_id]
            updated = replace(
                record,
                state="retryable_terminal",
                status=snapshot.status,
                order_id=None,
            )
            self._records[client_order_id] = updated
            return updated

    async def mark_result(self, client_order_id: str, result: ExecutionResult) -> OrderRecord:
        async with self._lock:
            record = self._records[client_order_id]
            updated = replace(
                record,
                state="terminal" if result.status.terminal else "pending",
                order_id=result.order_id,
                status=result.status,
                result=result,
            )
            self._records[client_order_id] = updated
            return updated

    async def mark_submission_unknown(self, client_order_id: str, attempt_index: int) -> OrderRecord:
        async with self._lock:
            record = self._records[client_order_id]
            updated = replace(
                record,
                state="submission_unknown",
                attempt_index=attempt_index,
                status=OrderStatus.UNKNOWN,
            )
            self._records[client_order_id] = updated
            return updated

