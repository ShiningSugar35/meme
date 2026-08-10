"""Safe live order state machine."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .errors import LiveTradeError, as_live_error
from .journal import OrderRecord
from .models import (
    ExecutionPolicy,
    ExecutionResult,
    FailureKind,
    OrderSnapshot,
    OrderStatus,
    SwapIntent,
)
from .protocols import AtomicSwapProvider, OrderJournal, PreparedSwapProvider, TransactionSigner


T = TypeVar("T")

# Escalating fees/slippage is allowed only for explicit terminal chain causes.
_EXPLICIT_ESCALATION_CODES = {
    "BLOCKHASH_EXPIRED",
    "TRANSACTION_EXPIRED",
    "PRIORITY_FEE_TOO_LOW",
    "TIP_TOO_LOW",
    "SLIPPAGE_EXCEEDED",
    "TX_DROPPED",
}


class LiveExecutionEngine:
    def __init__(
        self,
        provider: AtomicSwapProvider | PreparedSwapProvider,
        journal: OrderJournal,
        *,
        signer: TransactionSigner | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.provider = provider
        self.journal = journal
        self.signer = signer
        self._sleep = sleeper
        self._order_locks: dict[str, asyncio.Lock] = {}
        self._order_locks_guard = asyncio.Lock()
        if isinstance(provider, PreparedSwapProvider) and signer is None:
            raise ValueError("A TransactionSigner is required for prepared-transaction providers")

    async def _read_with_retry(
        self,
        operation: Callable[[], Awaitable[T]],
        policy: ExecutionPolicy,
    ) -> T:
        last: LiveTradeError | None = None
        for attempt in range(policy.read_retry_count):
            try:
                return await operation()
            except Exception as exc:
                error = as_live_error(exc)
                last = error
                # A 429 has a server-defined reset time; repeatedly calling can
                # extend the ban. Validation/balance/no-route cannot be healed
                # by transport retries either.
                if error.kind not in {FailureKind.NETWORK, FailureKind.API}:
                    raise error
                if attempt < policy.read_retry_count - 1 and policy.read_retry_backoff_seconds:
                    await self._sleep(policy.read_retry_backoff_seconds * (attempt + 1))
        assert last is not None
        raise last

    async def _poll_existing(
        self,
        intent: SwapIntent,
        record: OrderRecord,
        policy: ExecutionPolicy,
    ) -> tuple[ExecutionResult, OrderSnapshot | None]:
        if not record.order_id:
            raise LiveTradeError(
                "Cannot poll a submitted record without an order_id",
                kind=FailureKind.API,
                code="MISSING_ORDER_ID",
            )
        last: OrderSnapshot | None = None
        for poll_index in range(policy.max_polls_per_attempt):
            last = await self._read_with_retry(
                lambda: self.provider.get_order(record.order_id or ""),
                policy,
            )
            if last.status is OrderStatus.CONFIRMED:
                result = ExecutionResult(
                    intent.client_order_id,
                    last.order_id,
                    last.status,
                    record.attempt_index + 1,
                    tx_hash=last.tx_hash,
                    report=last.report,
                )
                await self.journal.mark_result(intent.client_order_id, result)
                return result, last
            if last.status in {OrderStatus.FAILED, OrderStatus.EXPIRED}:
                result = ExecutionResult(
                    intent.client_order_id,
                    last.order_id,
                    last.status,
                    record.attempt_index + 1,
                    tx_hash=last.tx_hash,
                    failure_kind=last.failure_kind or (
                        FailureKind.EXPIRED if last.status is OrderStatus.EXPIRED else FailureKind.CHAIN
                    ),
                    error_code=last.error_code,
                    report=last.report,
                )
                return result, last
            # pending and processed are explicitly non-terminal and must never
            # trigger another submission.
            if poll_index < policy.max_polls_per_attempt - 1 and policy.poll_interval_seconds:
                await self._sleep(policy.poll_interval_seconds)
        result = ExecutionResult(
            intent.client_order_id,
            record.order_id,
            OrderStatus.PENDING,
            record.attempt_index + 1,
            failure_kind=FailureKind.PENDING,
        )
        await self.journal.mark_result(intent.client_order_id, result)
        return result, last

    @staticmethod
    def _can_escalate(snapshot: OrderSnapshot | None) -> bool:
        if snapshot is None:
            return False
        if snapshot.status is OrderStatus.EXPIRED:
            return True
        return (
            snapshot.status is OrderStatus.FAILED
            and snapshot.failure_kind is FailureKind.CHAIN
            and str(snapshot.error_code or "").upper() in _EXPLICIT_ESCALATION_CODES
        )

    async def _lock_for(self, client_order_id: str) -> asyncio.Lock:
        async with self._order_locks_guard:
            return self._order_locks.setdefault(client_order_id, asyncio.Lock())

    async def execute(self, intent: SwapIntent, policy: ExecutionPolicy) -> ExecutionResult:
        """Execute once per logical client order, even under concurrent calls."""
        lock = await self._lock_for(intent.client_order_id)
        async with lock:
            return await self._execute_locked(intent, policy)

    async def _execute_locked(self, intent: SwapIntent, policy: ExecutionPolicy) -> ExecutionResult:
        record = await self.journal.reserve(intent)
        if record.result is not None and record.result.status.terminal:
            return record.result
        if record.state in {"submission_unknown", "submission_started", "reserved"} and not record.order_id:
            raise LiveTradeError(
                "Previous submission outcome is unknown; reconciliation is required before retry",
                kind=FailureKind.PENDING,
                code="SUBMISSION_UNKNOWN",
                submission_unknown=True,
            )
        if record.order_id:
            result, _ = await self._poll_existing(intent, record, policy)
            return result

        steps = policy.steps_for(intent.side)
        last_result: ExecutionResult | None = None
        for attempt_index, step in enumerate(steps):
            await self.journal.mark_quoting(intent.client_order_id, attempt_index)
            quote = await self._read_with_retry(
                lambda: self.provider.quote(intent, step),
                policy,
            )
            child_order_id = f"{intent.client_order_id}:{attempt_index + 1}"
            await self.journal.mark_submission_started(intent.client_order_id, attempt_index)
            try:
                if isinstance(self.provider, AtomicSwapProvider):
                    submission = await self.provider.submit_swap(
                        intent,
                        quote,
                        step,
                        submission_client_order_id=child_order_id,
                    )
                else:
                    assert isinstance(self.provider, PreparedSwapProvider)
                    assert self.signer is not None
                    prepared = await self.provider.build_swap(intent, quote, step)
                    signed = await self.signer.sign(prepared)
                    submission = await self.provider.submit(
                        signed,
                        submission_client_order_id=child_order_id,
                    )
            except Exception as exc:
                error = as_live_error(exc, submission_phase=True)
                # The submit call has started but no stable provider order id was
                # persisted. Every exception is therefore ambiguous across a
                # process crash boundary, even when the immediate error looks like
                # a 429/5xx rejection. Persist the unknown state before returning.
                await self.journal.mark_submission_unknown(intent.client_order_id, attempt_index)
                # Never resubmit automatically after a submit exception. Even
                # an HTTP 5xx can hide a successful downstream acceptance.
                raise error

            record = await self.journal.mark_submitted(
                intent.client_order_id,
                submission,
                attempt_index,
            )
            result, snapshot = await self._poll_existing(intent, record, policy)
            if result.status in {OrderStatus.CONFIRMED, OrderStatus.PENDING}:
                return result
            last_result = result
            if attempt_index >= len(steps) - 1 or not self._can_escalate(snapshot):
                await self.journal.mark_result(intent.client_order_id, result)
                return result
            await self.journal.mark_retryable_terminal(intent.client_order_id, snapshot)

        assert last_result is not None
        await self.journal.mark_result(intent.client_order_id, last_result)
        return last_result
