from __future__ import annotations

import asyncio

from backend.app.trading.live.gmgn import GMGNAtomicProvider, TradeTransportResponse
from backend.app.trading.live.models import ExecutionStep, OrderStatus, SwapIntent, TradeSide


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.responses.pop(0)


def trade() -> SwapIntent:
    return SwapIntent(
        chain="sol",
        wallet_address="wallet",
        input_token="SOL",
        output_token="TOKEN",
        input_amount_raw="1000",
        side=TradeSide.BUY,
        client_order_id="logical",
    )


def test_gmgn_quote_swap_poll_uses_configured_execution_values() -> None:
    transport = FakeTransport([
        TradeTransportResponse(200, {"data": {
            "input_token": "SOL",
            "output_token": "TOKEN",
            "input_amount": "1000",
            "output_amount": "500",
            "min_output_amount": "450",
            "slippage": 0.03,
        }}),
        TradeTransportResponse(200, {"data": {
            "order_id": "order-1",
            "status": "pending",
        }}),
        TradeTransportResponse(200, {"data": {
            "order_id": "order-1",
            "status": "processed",
        }}),
        TradeTransportResponse(200, {"data": {
            "order_id": "order-1",
            "status": "confirmed",
            "hash": "tx",
        }}),
    ])
    provider = GMGNAtomicProvider(transport)
    step = ExecutionStep(0.03, 0.0007, 0.0009)

    async def run():
        quote = await provider.quote(trade(), step)
        submission = await provider.submit_swap(
            trade(), quote, step, submission_client_order_id="logical:1"
        )
        processed = await provider.get_order(submission.order_id)
        confirmed = await provider.get_order(submission.order_id)
        return submission, processed, confirmed

    submission, processed, confirmed = asyncio.run(run())
    assert submission.status is OrderStatus.PENDING
    assert processed.status is OrderStatus.PROCESSED
    assert confirmed.status is OrderStatus.CONFIRMED
    swap_json = transport.calls[1][2]["json_body"]
    assert swap_json["slippage"] == 0.03
    assert swap_json["priority_fee"] == 0.0007
    assert swap_json["tip_fee"] == 0.0009
    assert swap_json["client_order_id"] == "logical:1"

