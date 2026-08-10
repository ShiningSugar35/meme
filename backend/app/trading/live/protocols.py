"""Provider, signer and idempotency protocols."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    ExecutionResult,
    ExecutionStep,
    OrderSnapshot,
    PreparedTransaction,
    Quote,
    SignedTransaction,
    Submission,
    SwapIntent,
)


@runtime_checkable
class AtomicSwapProvider(Protocol):
    name: str

    async def quote(self, intent: SwapIntent, step: ExecutionStep) -> Quote: ...

    async def submit_swap(
        self,
        intent: SwapIntent,
        quote: Quote,
        step: ExecutionStep,
        *,
        submission_client_order_id: str,
    ) -> Submission: ...

    async def get_order(self, order_id: str) -> OrderSnapshot: ...


@runtime_checkable
class PreparedSwapProvider(Protocol):
    name: str

    async def quote(self, intent: SwapIntent, step: ExecutionStep) -> Quote: ...

    async def build_swap(
        self,
        intent: SwapIntent,
        quote: Quote,
        step: ExecutionStep,
    ) -> PreparedTransaction: ...

    async def submit(
        self,
        signed: SignedTransaction,
        *,
        submission_client_order_id: str,
    ) -> Submission: ...

    async def get_order(self, order_id: str) -> OrderSnapshot: ...


class TransactionSigner(Protocol):
    async def sign(self, transaction: PreparedTransaction) -> SignedTransaction: ...


class OrderJournal(Protocol):
    async def reserve(self, intent: SwapIntent) -> "OrderRecord": ...

    async def get(self, client_order_id: str) -> "OrderRecord | None": ...

    async def mark_quoting(
        self,
        client_order_id: str,
        attempt_index: int,
    ) -> "OrderRecord": ...

    async def mark_submission_started(
        self,
        client_order_id: str,
        attempt_index: int,
    ) -> "OrderRecord": ...

    async def mark_submitted(
        self,
        client_order_id: str,
        submission: Submission,
        attempt_index: int,
    ) -> "OrderRecord": ...

    async def mark_retryable_terminal(
        self,
        client_order_id: str,
        snapshot: OrderSnapshot,
    ) -> "OrderRecord": ...

    async def mark_result(self, client_order_id: str, result: ExecutionResult) -> "OrderRecord": ...

    async def mark_submission_unknown(
        self,
        client_order_id: str,
        attempt_index: int,
    ) -> "OrderRecord": ...


from .journal import OrderRecord  # noqa: E402  (typing cycle only)

