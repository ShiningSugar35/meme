from __future__ import annotations

import asyncio

from backend.app.trading.live.errors import LiveTradeError
from backend.app.trading.live.gmgn_cli import CliResult, GmgnCliProvider
from backend.app.trading.live.models import ExecutionStep, SwapIntent, TradeSide


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.arguments = []

    async def run(self, arguments):
        self.arguments.append(tuple(arguments))
        return self.results.pop(0)


def intent() -> SwapIntent:
    return SwapIntent(
        "sol",
        "public-wallet",
        "SOL",
        "TOKEN",
        "1000",
        TradeSide.BUY,
        "logical-id",
    )


def test_cli_provider_never_adds_credentials_to_arguments() -> None:
    runner = FakeRunner([
        CliResult(0, {"data": {
            "input_token": "SOL",
            "output_token": "TOKEN",
            "input_amount": "1000",
            "output_amount": "500",
            "min_output_amount": "450",
            "slippage": 0.02,
        }}),
        CliResult(0, {"data": {"order_id": "order", "status": "pending"}}),
    ])
    provider = GmgnCliProvider(runner, allow_live_execution=True)
    step = ExecutionStep(0.02, 0.0002, 0.0003)

    async def run():
        quote = await provider.quote(intent(), step)
        return await provider.submit_swap(
            intent(), quote, step, submission_client_order_id="logical-id:1"
        )

    submission = asyncio.run(run())
    assert submission.order_id == "order"
    flattened = " ".join(value for call in runner.arguments for value in call).lower()
    assert "api-key" not in flattened
    assert "private-key" not in flattened
    assert "--slippage 2" in flattened
    assert "--priority-fee 0.0002" in flattened
    assert "--tip-fee 0.0003" in flattened


def test_cli_swap_requires_explicit_live_enablement() -> None:
    runner = FakeRunner([])
    provider = GmgnCliProvider(runner)
    quote_result = None

    async def run():
        from backend.app.trading.live.models import Quote

        quote = Quote("SOL", "TOKEN", "1000", "500", "450", 0.02)
        return await provider.submit_swap(
            intent(), quote, ExecutionStep(0.02, 0.0002, 0.0003),
            submission_client_order_id="logical-id:1",
        )

    try:
        quote_result = asyncio.run(run())
    except LiveTradeError as exc:
        assert exc.code == "LIVE_EXECUTION_NOT_CONFIRMED"
    else:
        raise AssertionError(f"expected enablement guard, got {quote_result}")
    assert runner.arguments == []

