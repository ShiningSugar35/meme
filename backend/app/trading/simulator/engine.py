from __future__ import annotations

from datetime import datetime, timedelta
import itertools
from typing import Callable, Iterable

from .types import (
    EntrySignal,
    ExecutionQuote,
    ExitReason,
    FailureCategory,
    MarketCandle,
    PortfolioSnapshot,
    QuoteProvider,
    QuoteRequest,
    Side,
    SimulatedPosition,
    SimulationConfig,
    SimulationEvent,
)


class TradingSimulator:
    """High-fidelity, dependency-injected paper execution state machine."""

    def __init__(
        self,
        quote_provider: QuoteProvider,
        config: SimulationConfig | None = None,
        *,
        sol_usd_price_at: Callable[[datetime], float | None] | None = None,
    ) -> None:
        self.quote_provider = quote_provider
        self.config = config or SimulationConfig()
        self.sol_usd_price_at = sol_usd_price_at
        self.cash_usd = float(self.config.initial_cash_usd)
        self.total_network_fees_sol = 0.0
        self.total_network_fees_usd = 0.0
        self.positions: dict[str, SimulatedPosition] = {}
        self.events: list[SimulationEvent] = []
        self._position_ids = itertools.count(1)
        self._event_ids = itertools.count(1)

    @property
    def open_positions(self) -> tuple[SimulatedPosition, ...]:
        return tuple(position for position in self.positions.values() if position.is_open)

    @property
    def closed_positions(self) -> tuple[SimulatedPosition, ...]:
        return tuple(position for position in self.positions.values() if not position.is_open)

    def open_position(self, signal: EntrySignal) -> SimulatedPosition | None:
        self._validate_signal(signal)
        if len(self.open_positions) >= self.config.max_open_positions:
            self._record_rejection(
                signal,
                FailureCategory.RISK_REJECTED,
                "maximum simultaneous simulated positions reached",
            )
            return None

        capital = min(0.01 * signal.liquidity_usd, 50.0)
        if self.cash_usd < capital:
            self._record_rejection(
                signal,
                FailureCategory.INSUFFICIENT_FUNDS,
                "insufficient simulated USD cash for fixed position capital",
            )
            return None

        position_id = f"sim-{next(self._position_ids):08d}"
        request = QuoteRequest(
            token_address=signal.token_address,
            side=Side.BUY,
            amount_usd=capital,
            reference_price=signal.reference_price,
            liquidity_usd=signal.liquidity_usd,
            requested_at=signal.observed_at,
            position_id=position_id,
        )
        quote = self.quote_provider.quote(request)
        network_fee = self._network_fee_usd(request, quote)
        if network_fee is None:
            self._record_rejection(
                signal,
                FailureCategory.API,
                "SOL/USD fee price unavailable at execution time",
            )
            return None
        network_fee_usd, sol_usd_price = network_fee
        if not self._can_apply_quote(
            quote, capital + max(quote.fee_usd, 0.0) + network_fee_usd
        ):
            self._charge_failed_network_fee(request, quote)
            self._record_failed_or_resource_quote(request, quote, "entry_failed")
            return None

        assert quote.fill_price is not None
        self.cash_usd -= capital + quote.fee_usd + network_fee_usd
        self.total_network_fees_sol += quote.network_fee_sol
        self.total_network_fees_usd += network_fee_usd
        position = SimulatedPosition(
            position_id=position_id,
            token_address=signal.token_address,
            opened_at=signal.observed_at,
            deadline_at=signal.observed_at
            + timedelta(seconds=self.config.max_holding_seconds),
            entry_reference_price=signal.reference_price,
            entry_fill_price=quote.fill_price,
            quantity=capital / quote.fill_price,
            invested_usd=capital,
            entry_fee_usd=quote.fee_usd,
            entry_network_fee_sol=quote.network_fee_sol,
            entry_network_fee_usd=network_fee_usd,
            entry_sol_usd_price=sol_usd_price,
            model_probability=signal.model_probability,
            strategy_key=signal.strategy_key,
        )
        self.positions[position_id] = position
        self._record_quote_event(request, quote, "entry_filled")
        return position

    def process_candle(self, candle: MarketCandle) -> tuple[str, ...]:
        self._validate_candle(candle)
        attempted: list[str] = []
        # Same-token batches are independent and all remain eligible.
        positions = [
            position
            for position in self.open_positions
            if position.token_address == candle.token_address
            and candle.observed_at >= position.opened_at
        ]
        for position in positions:
            decision = self._exit_decision(position, candle)
            if decision is None:
                continue
            reason, reference_price = decision
            attempted.append(position.position_id)
            self.close_position(
                position.position_id,
                reason=reason,
                reference_price=reference_price,
                liquidity_usd=candle.liquidity_usd,
                observed_at=candle.observed_at,
            )
        return tuple(attempted)

    def close_position(
        self,
        position_id: str,
        *,
        reason: ExitReason,
        reference_price: float,
        liquidity_usd: float,
        observed_at,
    ) -> bool:
        position = self.positions.get(position_id)
        if position is None:
            raise KeyError(f"unknown simulated position: {position_id}")
        if not position.is_open:
            return False
        amount_usd = position.quantity * reference_price
        request = QuoteRequest(
            token_address=position.token_address,
            side=Side.SELL,
            amount_usd=amount_usd,
            reference_price=reference_price,
            liquidity_usd=liquidity_usd,
            requested_at=observed_at,
            position_id=position_id,
        )
        quote = self.quote_provider.quote(request)
        network_fee = self._network_fee_usd(request, quote)
        if network_fee is None:
            self._record_failed_or_resource_quote(
                request,
                ExecutionQuote(
                    success=False,
                    fill_price=None,
                    gross_usd=0.0,
                    fee_usd=0.0,
                    network_fee_sol=0.0,
                    slippage_bps=quote.slippage_bps,
                    latency_ms=quote.latency_ms,
                    failure_category=FailureCategory.API,
                    message="SOL/USD fee price unavailable at execution time",
                ),
                "exit_failed",
                reason,
            )
            return False
        network_fee_usd, sol_usd_price = network_fee
        if not self._can_apply_quote(quote, 0.0):
            self._charge_failed_network_fee(request, quote)
            self._record_failed_or_resource_quote(request, quote, "exit_failed", reason)
            return False

        assert quote.fill_price is not None
        proceeds = quote.gross_usd - quote.fee_usd - network_fee_usd
        self.cash_usd += proceeds
        self.total_network_fees_sol += quote.network_fee_sol
        self.total_network_fees_usd += network_fee_usd
        position.closed_at = observed_at
        position.exit_reason = reason
        position.exit_fill_price = quote.fill_price
        position.exit_fee_usd = quote.fee_usd
        position.exit_network_fee_sol = quote.network_fee_sol
        position.exit_network_fee_usd = network_fee_usd
        position.exit_sol_usd_price = sol_usd_price
        position.proceeds_usd = proceeds
        position.realized_pnl_usd = (
            proceeds
            - position.invested_usd
            - position.entry_fee_usd
            - position.entry_network_fee_usd
        )
        self._record_quote_event(request, quote, "exit_filled", reason)
        return True

    def snapshot(self) -> PortfolioSnapshot:
        platform_fees = sum(
            position.entry_fee_usd + position.exit_fee_usd
            for position in self.positions.values()
        )
        return PortfolioSnapshot(
            cash_usd=self.cash_usd,
            open_positions=len(self.open_positions),
            closed_positions=len(self.closed_positions),
            realized_pnl_usd=sum(
                position.realized_pnl_usd for position in self.closed_positions
            ),
            total_fees_usd=platform_fees + self.total_network_fees_usd,
            total_network_fees_usd=self.total_network_fees_usd,
            total_network_fees_sol=self.total_network_fees_sol,
        )

    def _exit_decision(
        self, position: SimulatedPosition, candle: MarketCandle
    ) -> tuple[ExitReason, float] | None:
        stop = position.entry_reference_price * self.config.stop_loss_multiple
        take = position.entry_reference_price * self.config.take_profit_multiple
        # Conservative ordering exactly matches label generation for a candle
        # that touches both barriers.
        if candle.low <= stop:
            return ExitReason.STOP_LOSS, stop
        if candle.high >= take:
            return ExitReason.TAKE_PROFIT, take
        if candle.observed_at >= position.deadline_at:
            return ExitReason.TIMEOUT, candle.close
        return None

    def _can_apply_quote(self, quote: ExecutionQuote, required_cash: float) -> bool:
        return bool(
            quote.success
            and quote.fill_price is not None
            and quote.fill_price > 0
            and quote.gross_usd >= 0
            and quote.fee_usd >= 0
            and quote.network_fee_sol >= 0
            and self.cash_usd >= required_cash
        )

    def _network_fee_usd(
        self,
        request: QuoteRequest,
        quote: ExecutionQuote,
    ) -> tuple[float, float | None] | None:
        fee_sol = max(0.0, float(quote.network_fee_sol))
        if fee_sol == 0:
            return 0.0, None
        if self.sol_usd_price_at is None:
            return None
        occurred_at = request.requested_at + timedelta(
            milliseconds=max(quote.latency_ms, 0)
        )
        price = self.sol_usd_price_at(occurred_at)
        if price is None or float(price) <= 0:
            return None
        price_usd = float(price)
        return fee_sol * price_usd, price_usd

    def _charge_failed_network_fee(
        self,
        request: QuoteRequest,
        quote: ExecutionQuote,
    ) -> None:
        if quote.success or quote.network_fee_sol <= 0:
            return
        network_fee = self._network_fee_usd(request, quote)
        if network_fee is None:
            return
        network_fee_usd, _ = network_fee
        self.cash_usd -= network_fee_usd
        self.total_network_fees_sol += quote.network_fee_sol
        self.total_network_fees_usd += network_fee_usd

    def _record_quote_event(
        self,
        request: QuoteRequest,
        quote: ExecutionQuote,
        event_type: str,
        reason: ExitReason | None = None,
    ) -> None:
        self.events.append(
            SimulationEvent(
                event_id=next(self._event_ids),
                occurred_at=request.requested_at
                + timedelta(milliseconds=max(quote.latency_ms, 0)),
                event_type=event_type,
                token_address=request.token_address,
                position_id=request.position_id,
                success=quote.success,
                side=request.side,
                exit_reason=reason,
                amount_usd=request.amount_usd,
                fee_usd=quote.fee_usd,
                network_fee_sol=quote.network_fee_sol,
                latency_ms=quote.latency_ms,
                failure_category=quote.failure_category,
                message=quote.message,
            )
        )

    def _record_failed_or_resource_quote(
        self,
        request: QuoteRequest,
        quote: ExecutionQuote,
        event_type: str,
        reason: ExitReason | None = None,
    ) -> None:
        if not quote.success:
            self._record_quote_event(request, quote, event_type, reason)
            return
        category = FailureCategory.INSUFFICIENT_FUNDS
        message = "simulated account lacks USD cash for quoted execution"
        rejected = ExecutionQuote(
            success=False,
            fill_price=None,
            gross_usd=0.0,
            fee_usd=0.0,
            network_fee_sol=0.0,
            slippage_bps=quote.slippage_bps,
            latency_ms=quote.latency_ms,
            failure_category=category,
            message=message,
        )
        self._record_quote_event(request, rejected, event_type, reason)

    def _record_rejection(
        self,
        signal: EntrySignal,
        category: FailureCategory,
        message: str,
    ) -> None:
        self.events.append(
            SimulationEvent(
                event_id=next(self._event_ids),
                occurred_at=signal.observed_at,
                event_type="entry_rejected",
                token_address=signal.token_address,
                position_id=None,
                success=False,
                side=Side.BUY,
                failure_category=category,
                message=message,
            )
        )

    @staticmethod
    def _validate_signal(signal: EntrySignal) -> None:
        if not signal.token_address:
            raise ValueError("token address is required")
        if signal.reference_price <= 0:
            raise ValueError("entry reference price must be positive")
        if signal.liquidity_usd <= 0:
            raise ValueError("entry liquidity must be positive")

    @staticmethod
    def _validate_candle(candle: MarketCandle) -> None:
        if min(candle.open, candle.high, candle.low, candle.close) <= 0:
            raise ValueError("candle prices must be positive")
        if candle.low > min(candle.open, candle.close, candle.high):
            raise ValueError("candle low is inconsistent")
        if candle.high < max(candle.open, candle.close, candle.low):
            raise ValueError("candle high is inconsistent")
        if candle.liquidity_usd <= 0:
            raise ValueError("candle liquidity must be positive")
