"""Prepared-transaction adapter for Jupiter-compatible gateways."""

from __future__ import annotations

from typing import Protocol

from .models import (
    ExecutionStep,
    OrderSnapshot,
    PreparedTransaction,
    Quote,
    SignedTransaction,
    Submission,
    SwapIntent,
)


class JupiterGateway(Protocol):
    async def quote(self, intent: SwapIntent, step: ExecutionStep) -> Quote: ...

    async def build_transaction(
        self,
        intent: SwapIntent,
        quote: Quote,
        step: ExecutionStep,
    ) -> PreparedTransaction: ...

    async def submit_transaction(
        self,
        signed: SignedTransaction,
        *,
        client_order_id: str,
    ) -> Submission: ...

    async def transaction_status(self, order_id: str) -> OrderSnapshot: ...


class JupiterProvider:
    """Keeps Jupiter mechanics behind the same executor contract as GMGN."""

    name = "jupiter"

    def __init__(self, gateway: JupiterGateway) -> None:
        self.gateway = gateway

    async def quote(self, intent: SwapIntent, step: ExecutionStep) -> Quote:
        return await self.gateway.quote(intent, step)

    async def build_swap(
        self,
        intent: SwapIntent,
        quote: Quote,
        step: ExecutionStep,
    ) -> PreparedTransaction:
        return await self.gateway.build_transaction(intent, quote, step)

    async def submit(
        self,
        signed: SignedTransaction,
        *,
        submission_client_order_id: str,
    ) -> Submission:
        return await self.gateway.submit_transaction(
            signed,
            client_order_id=submission_client_order_id,
        )

    async def get_order(self, order_id: str) -> OrderSnapshot:
        return await self.gateway.transaction_status(order_id)

