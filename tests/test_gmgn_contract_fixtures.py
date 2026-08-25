from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.collector.client import GMGNDataClient
from backend.app.collector.errors import CollectorAPIError, CollectorRateLimitError, CollectorValidationError
from backend.app.collector.models import ApiSlot, TransportResponse
from backend.app.trading.live.errors import LiveTradeError
from backend.app.trading.live.gmgn_cli import CliResult, GmgnCliProvider
from backend.app.trading.live.models import ExecutionStep, FailureKind, OrderStatus, SwapIntent, TradeSide


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "gmgn_contracts.json").read_text(encoding="utf-8")
)


def cli_result(name: str) -> CliResult:
    raw = FIXTURES[name]
    return CliResult(raw["return_code"], raw["data"], raw.get("error_message", ""))


class FixtureRunner:
    def __init__(self, *names: str) -> None:
        self.results = [cli_result(name) for name in names]
        self.arguments: list[tuple[str, ...]] = []

    async def run(self, arguments) -> CliResult:
        self.arguments.append(tuple(str(value) for value in arguments))
        return self.results.pop(0)


class FixtureTransport:
    def __init__(self, name: str) -> None:
        raw = FIXTURES[name]
        self.response = TransportResponse(raw["status_code"], raw["data"], raw.get("headers", {}))

    async def request(self, method, url, **kwargs):
        return self.response


class FixtureLimiter:
    def __init__(self) -> None:
        self.reset_at = None

    async def acquire(self, weight=1) -> None:
        return None

    def note_rate_limit(self, reset_at) -> None:
        self.reset_at = reset_at


def intent() -> SwapIntent:
    return SwapIntent(
        chain="sol",
        wallet_address="public-wallet",
        input_token="SOL",
        output_token="TOKEN",
        input_amount_raw="1000000",
        side=TradeSide.BUY,
        client_order_id="fixture-logical-order",
    )


def step() -> ExecutionStep:
    return ExecutionStep(0.05, 0.0002, 0.0003)


@pytest.mark.asyncio
async def test_cli_fixture_quote_swap_and_query_status_contract() -> None:
    runner = FixtureRunner(
        "cli_quote_success",
        "cli_swap_pending",
        "cli_query_processed",
        "cli_query_confirmed_alias",
    )
    provider = GmgnCliProvider(runner, allow_live_execution=True)

    quote = await provider.quote(intent(), step())
    submission = await provider.submit_swap(
        intent(), quote, step(), submission_client_order_id="fixture-logical-order:1"
    )
    processed = await provider.get_order(submission.order_id)
    confirmed = await provider.get_order(submission.order_id)

    assert quote.output_amount_raw == "500000000"
    assert quote.min_output_amount_raw == "475000000"
    assert submission.status is OrderStatus.PENDING
    assert submission.tx_hash == "tx-fixture-1"
    assert processed.status is OrderStatus.PROCESSED
    assert confirmed.status is OrderStatus.CONFIRMED
    assert confirmed.tx_hash == "tx-fixture-1"
    assert runner.arguments[0][:2] == ("order", "quote")
    assert runner.arguments[1][0] == "swap"
    assert runner.arguments[2][:2] == ("order", "get")


@pytest.mark.asyncio
async def test_cli_fixture_expired_and_slippage_failure_contract() -> None:
    expired_provider = GmgnCliProvider(FixtureRunner("cli_query_expired"))
    slippage_provider = GmgnCliProvider(FixtureRunner("cli_query_slippage_failed"))

    expired = await expired_provider.get_order("order-expired")
    slippage = await slippage_provider.get_order("order-slippage")

    assert expired.status is OrderStatus.EXPIRED
    assert expired.failure_kind is FailureKind.EXPIRED
    assert expired.error_code == "TRANSACTION_EXPIRED"
    assert slippage.status is OrderStatus.FAILED
    assert slippage.failure_kind is FailureKind.CHAIN
    assert slippage.error_code == "SLIPPAGE_EXCEEDED"


@pytest.mark.asyncio
async def test_cli_fixture_429_and_5xx_are_typed() -> None:
    rate_limited = GmgnCliProvider(FixtureRunner("cli_rate_limit"))
    server_error = GmgnCliProvider(FixtureRunner("cli_server_error"))

    with pytest.raises(LiveTradeError) as rate_exc:
        await rate_limited.get_order("order-rate")
    assert rate_exc.value.kind is FailureKind.RATE_LIMIT
    assert rate_exc.value.reset_at == 1_800_000_000

    with pytest.raises(LiveTradeError) as api_exc:
        await server_error.get_order("order-api")
    assert api_exc.value.kind is FailureKind.API
    assert api_exc.value.code == "500"


@pytest.mark.asyncio
async def test_collector_fixture_429_5xx_and_validation_contract() -> None:
    limiter = FixtureLimiter()
    placeholder = "[REDACTED]"
    rate_client = GMGNDataClient(
        base_url="https://example.invalid",
        transport=FixtureTransport("collector_rate_limit"),
        limiter=limiter,
    )
    with pytest.raises(CollectorRateLimitError) as rate_exc:
        await rate_client.request(ApiSlot(0, placeholder), "/v1/test")
    assert rate_exc.value.reset_at == 1_800_000_000
    assert limiter.reset_at is None
    assert rate_client.slot_rate_limit_remaining(ApiSlot(0, placeholder)) > 0
    assert placeholder not in str(rate_exc.value)

    server_client = GMGNDataClient(
        base_url="https://example.invalid",
        transport=FixtureTransport("collector_server_error"),
        limiter=FixtureLimiter(),
    )
    with pytest.raises(CollectorAPIError) as server_exc:
        await server_client.request(ApiSlot(1, placeholder), "/v1/test")
    assert server_exc.value.status_code == 503
    assert server_exc.value.code == "503"
    assert placeholder not in str(server_exc.value)

    validation_client = GMGNDataClient(
        base_url="https://example.invalid",
        transport=FixtureTransport("collector_validation_error"),
        limiter=FixtureLimiter(),
    )
    with pytest.raises(CollectorValidationError) as validation_exc:
        await validation_client.request(ApiSlot(2, placeholder), "/v1/test")
    assert "status=422" in str(validation_exc.value)
    assert placeholder not in str(validation_exc.value)
