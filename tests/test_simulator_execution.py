from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.trading.simulator import (
    EntrySignal,
    ExecutionQuote,
    ExitReason,
    FailureCategory,
    MarketCandle,
    QuoteRequest,
    Side,
    SimulationConfig,
    TradingSimulator,
)


class _FixedQuoteProvider:
    def __init__(self, *, fee_rate: float = 0.0, network_fee_sol: float = 0.0):
        self.fee_rate = fee_rate
        self.network_fee_sol = network_fee_sol
        self.requests: list[QuoteRequest] = []

    def quote(self, request: QuoteRequest) -> ExecutionQuote:
        self.requests.append(request)
        return ExecutionQuote(
            success=True,
            fill_price=request.reference_price,
            gross_usd=request.amount_usd,
            fee_usd=request.amount_usd * self.fee_rate,
            network_fee_sol=self.network_fee_sol,
            slippage_bps=0.0,
            latency_ms=250,
        )


class _FailSecondQuoteProvider(_FixedQuoteProvider):
    def quote(self, request: QuoteRequest) -> ExecutionQuote:
        if self.requests:
            self.requests.append(request)
            return ExecutionQuote(
                success=False,
                fill_price=None,
                gross_usd=0.0,
                fee_usd=0.0,
                network_fee_sol=0.0,
                slippage_bps=0.0,
                latency_ms=500,
                failure_category=FailureCategory.NETWORK,
                message="timeout",
            )
        return super().quote(request)


def _signal(token: str, at: datetime, liquidity: float = 4_000.0) -> EntrySignal:
    return EntrySignal(
        token_address=token,
        observed_at=at,
        reference_price=1.0,
        liquidity_usd=liquidity,
        model_probability=0.4,
    )


def test_capital_formula_and_same_token_independent_batches() -> None:
    provider = _FixedQuoteProvider()
    simulator = TradingSimulator(provider)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    first = simulator.open_position(_signal("token-a", now, 4_000.0))
    second = simulator.open_position(_signal("token-a", now + timedelta(minutes=1), 20_000.0))

    assert first is not None and first.invested_usd == pytest.approx(40.0)
    assert second is not None and second.invested_usd == pytest.approx(50.0)
    assert first.position_id != second.position_id
    assert len(simulator.open_positions) == 2
    assert simulator.cash_usd == pytest.approx(910.0)


def test_same_candle_tp_sl_conflict_conservatively_stops_all_batches() -> None:
    provider = _FixedQuoteProvider()
    simulator = TradingSimulator(provider)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    simulator.open_position(_signal("token-a", now))
    simulator.open_position(_signal("token-a", now + timedelta(minutes=1)))

    attempted = simulator.process_candle(
        MarketCandle(
            token_address="token-a",
            observed_at=now + timedelta(minutes=10),
            open=1.0,
            high=1.7,
            low=0.8,
            close=1.2,
            liquidity_usd=4_000.0,
        )
    )

    assert len(attempted) == 2
    assert not simulator.open_positions
    assert all(
        position.exit_reason == ExitReason.STOP_LOSS
        for position in simulator.closed_positions
    )
    assert all(position.realized_pnl_usd == pytest.approx(-4.0) for position in simulator.closed_positions)


def test_two_hour_timeout_uses_close_and_accounts_for_fees() -> None:
    provider = _FixedQuoteProvider(fee_rate=0.01, network_fee_sol=0.001)
    simulator = TradingSimulator(provider, sol_usd_price_at=lambda _at: 180.0)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    position = simulator.open_position(_signal("token-b", now, 10_000.0))
    assert position is not None

    simulator.process_candle(
        MarketCandle(
            token_address="token-b",
            observed_at=now + timedelta(hours=2),
            open=1.1,
            high=1.2,
            low=1.0,
            close=1.1,
            liquidity_usd=10_000.0,
        )
    )
    closed = simulator.closed_positions[0]
    assert closed.exit_reason == ExitReason.TIMEOUT
    # $50 -> $55 gross, $0.50 entry fee, $0.55 exit fee, plus
    # 0.001 SOL network fee on each side at $180/SOL = $0.36 total.
    assert closed.realized_pnl_usd == pytest.approx(3.59)
    snapshot = simulator.snapshot()
    assert snapshot.total_fees_usd == pytest.approx(1.41)
    assert snapshot.total_network_fees_usd == pytest.approx(0.36)
    assert snapshot.total_network_fees_sol == pytest.approx(0.002)


def test_failed_exit_is_not_faked_and_position_remains_open() -> None:
    provider = _FailSecondQuoteProvider()
    simulator = TradingSimulator(provider)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    position = simulator.open_position(_signal("token-c", now))
    assert position is not None

    simulator.process_candle(
        MarketCandle(
            token_address="token-c",
            observed_at=now + timedelta(minutes=5),
            open=1.0,
            high=1.7,
            low=0.95,
            close=1.6,
            liquidity_usd=4_000.0,
        )
    )

    assert position.is_open
    assert simulator.events[-1].event_type == "exit_failed"
    assert simulator.events[-1].failure_category == FailureCategory.NETWORK
    assert simulator.cash_usd == pytest.approx(960.0)

