from __future__ import annotations

import asyncio

from backend.app.trading.live.errors import LiveTradeError
from backend.app.trading.live.executor import LiveExecutionEngine
from backend.app.trading.live.journal import InMemoryOrderJournal
from backend.app.trading.live.models import (
    ExecutionPolicy,
    ExecutionStep,
    FailureKind,
    OrderSnapshot,
    OrderStatus,
    Quote,
    Submission,
    SwapIntent,
    TradeSide,
)


async def no_sleep(_: float) -> None:
    return None


def intent(client_order_id: str = "logical-1") -> SwapIntent:
    return SwapIntent(
        chain="sol",
        wallet_address="wallet",
        input_token="SOL",
        output_token="TOKEN",
        input_amount_raw="1000000",
        side=TradeSide.BUY,
        client_order_id=client_order_id,
    )


def policy(*steps: ExecutionStep, polls: int = 3) -> ExecutionPolicy:
    configured = tuple(steps) or (ExecutionStep(0.01, 0.0001, 0.0001),)
    return ExecutionPolicy(
        buy_steps=configured,
        sell_steps=configured,
        poll_interval_seconds=0,
        max_polls_per_attempt=polls,
        read_retry_count=1,
        read_retry_backoff_seconds=0,
    )


class FakeAtomicProvider:
    name = "fake"

    def __init__(self, status_sequences):
        self.status_sequences = [list(sequence) for sequence in status_sequences]
        self.active: dict[str, list[OrderSnapshot]] = {}
        self.submit_count = 0
        self.steps: list[ExecutionStep] = []
        self.submit_error: BaseException | None = None

    async def quote(self, trade, step):
        return Quote("SOL", "TOKEN", trade.input_amount_raw, "100", "90", step.slippage)

    async def submit_swap(self, trade, quote, step, *, submission_client_order_id):
        self.submit_count += 1
        self.steps.append(step)
        if self.submit_error:
            raise self.submit_error
        order_id = f"order-{self.submit_count}"
        self.active[order_id] = self.status_sequences[self.submit_count - 1]
        return Submission(order_id, OrderStatus.PENDING)

    async def get_order(self, order_id):
        sequence = self.active[order_id]
        return sequence.pop(0) if len(sequence) > 1 else sequence[0]


def test_pending_and_processed_only_poll_original_order() -> None:
    provider = FakeAtomicProvider([[
        OrderSnapshot("order-1", OrderStatus.PENDING),
        OrderSnapshot("order-1", OrderStatus.PROCESSED),
        OrderSnapshot("order-1", OrderStatus.CONFIRMED, tx_hash="hash"),
    ]])
    engine = LiveExecutionEngine(provider, InMemoryOrderJournal(), sleeper=no_sleep)
    result = asyncio.run(engine.execute(intent(), policy(polls=3)))
    assert result.status is OrderStatus.CONFIRMED
    assert provider.submit_count == 1


def test_concurrent_same_client_order_id_submits_only_once() -> None:
    provider = FakeAtomicProvider([[
        OrderSnapshot("order-1", OrderStatus.CONFIRMED),
    ]])
    engine = LiveExecutionEngine(provider, InMemoryOrderJournal(), sleeper=no_sleep)

    async def run_both():
        return await asyncio.gather(
            engine.execute(intent(), policy()),
            engine.execute(intent(), policy()),
        )

    results = asyncio.run(run_both())
    assert all(result.status is OrderStatus.CONFIRMED for result in results)
    assert provider.submit_count == 1


def test_timed_out_pending_call_can_resume_poll_without_resubmit() -> None:
    provider = FakeAtomicProvider([[
        OrderSnapshot("order-1", OrderStatus.PENDING),
        OrderSnapshot("order-1", OrderStatus.PROCESSED),
    ]])
    journal = InMemoryOrderJournal()
    engine = LiveExecutionEngine(provider, journal, sleeper=no_sleep)
    first = asyncio.run(engine.execute(intent(), policy(polls=2)))
    assert first.status is OrderStatus.PENDING
    provider.active["order-1"].append(OrderSnapshot("order-1", OrderStatus.CONFIRMED))
    second = asyncio.run(engine.execute(intent(), policy(polls=2)))
    assert second.status is OrderStatus.CONFIRMED
    assert provider.submit_count == 1


def test_expired_terminal_order_may_use_next_configured_step() -> None:
    provider = FakeAtomicProvider([
        [OrderSnapshot("order-1", OrderStatus.EXPIRED, failure_kind=FailureKind.EXPIRED, error_code="TRANSACTION_EXPIRED")],
        [OrderSnapshot("order-2", OrderStatus.CONFIRMED)],
    ])
    steps = (
        ExecutionStep(0.01, 0.0001, 0.0001),
        ExecutionStep(0.02, 0.0002, 0.0002),
    )
    engine = LiveExecutionEngine(provider, InMemoryOrderJournal(), sleeper=no_sleep)
    result = asyncio.run(engine.execute(intent(), policy(*steps)))
    assert result.status is OrderStatus.CONFIRMED
    assert provider.submit_count == 2
    assert provider.steps == list(steps)


def test_submit_network_unknown_never_retries() -> None:
    provider = FakeAtomicProvider([[]])
    provider.submit_error = ConnectionError("ambiguous")
    journal = InMemoryOrderJournal()
    engine = LiveExecutionEngine(provider, journal, sleeper=no_sleep)
    try:
        asyncio.run(engine.execute(intent(), policy(
            ExecutionStep(0.01, 0.0001, 0.0001),
            ExecutionStep(0.02, 0.0002, 0.0002),
        )))
    except LiveTradeError as exc:
        assert exc.submission_unknown
    else:
        raise AssertionError("expected LiveTradeError")
    assert provider.submit_count == 1
    try:
        asyncio.run(engine.execute(intent(), policy()))
    except LiveTradeError as exc:
        assert exc.code == "SUBMISSION_UNKNOWN"
    else:
        raise AssertionError("expected reconciliation guard")
    assert provider.submit_count == 1


def test_swap_429_does_not_move_to_a_higher_fee_step() -> None:
    provider = FakeAtomicProvider([[]])
    provider.submit_error = LiveTradeError("limited", kind=FailureKind.RATE_LIMIT, reset_at=1_800_000_000)
    journal = InMemoryOrderJournal()
    engine = LiveExecutionEngine(provider, journal, sleeper=no_sleep)
    try:
        asyncio.run(engine.execute(intent(), policy(
            ExecutionStep(0.01, 0.0001, 0.0001),
            ExecutionStep(0.05, 0.001, 0.001),
        )))
    except LiveTradeError as exc:
        assert exc.kind is FailureKind.RATE_LIMIT
    else:
        raise AssertionError("expected 429")
    assert provider.submit_count == 1
    record = asyncio.run(journal.get("logical-1"))
    assert record is not None and record.state == "submission_unknown"
    try:
        asyncio.run(engine.execute(intent(), policy()))
    except LiveTradeError as exc:
        assert exc.code == "SUBMISSION_UNKNOWN"
    else:
        raise AssertionError("expected reconciliation guard after submit 429")
    assert provider.submit_count == 1
