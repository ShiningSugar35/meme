from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class FailureCategory(str, Enum):
    NETWORK = "network"
    API = "api"
    RATE_LIMIT = "rate_limit"
    NO_ROUTE = "no_route"
    CHAIN_REJECTED = "chain_rejected"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    RISK_REJECTED = "risk_rejected"


class ExitReason(str, Enum):
    TAKE_PROFIT = "take_profit_1_6x"
    STOP_LOSS = "stop_loss_0_9x"
    TIMEOUT = "timeout_2h"
    MANUAL = "manual"


@dataclass(frozen=True)
class EntrySignal:
    token_address: str
    observed_at: datetime
    reference_price: float
    liquidity_usd: float
    model_probability: float | None = None
    strategy_key: str = "model_1"


@dataclass(frozen=True)
class MarketCandle:
    token_address: str
    observed_at: datetime
    open: float
    high: float
    low: float
    close: float
    liquidity_usd: float


@dataclass(frozen=True)
class QuoteRequest:
    token_address: str
    side: Side
    amount_usd: float
    reference_price: float
    liquidity_usd: float
    requested_at: datetime
    position_id: str | None = None


@dataclass(frozen=True)
class ExecutionQuote:
    success: bool
    fill_price: float | None
    gross_usd: float
    fee_usd: float
    network_fee_sol: float
    slippage_bps: float
    latency_ms: int
    failure_category: FailureCategory | None = None
    message: str | None = None


class QuoteProvider(Protocol):
    def quote(self, request: QuoteRequest) -> ExecutionQuote: ...


@dataclass
class SimulatedPosition:
    position_id: str
    token_address: str
    opened_at: datetime
    deadline_at: datetime
    entry_reference_price: float
    entry_fill_price: float
    quantity: float
    invested_usd: float
    entry_fee_usd: float
    entry_network_fee_sol: float
    entry_network_fee_usd: float
    entry_sol_usd_price: float | None
    model_probability: float | None
    strategy_key: str
    closed_at: datetime | None = None
    exit_reason: ExitReason | None = None
    exit_fill_price: float | None = None
    exit_fee_usd: float = 0.0
    exit_network_fee_sol: float = 0.0
    exit_network_fee_usd: float = 0.0
    exit_sol_usd_price: float | None = None
    proceeds_usd: float = 0.0
    realized_pnl_usd: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


@dataclass(frozen=True)
class SimulationEvent:
    event_id: int
    occurred_at: datetime
    event_type: str
    token_address: str
    position_id: str | None
    success: bool
    side: Side | None = None
    exit_reason: ExitReason | None = None
    amount_usd: float = 0.0
    fee_usd: float = 0.0
    network_fee_sol: float = 0.0
    latency_ms: int = 0
    failure_category: FailureCategory | None = None
    message: str | None = None


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash_usd: float
    open_positions: int
    closed_positions: int
    realized_pnl_usd: float
    total_fees_usd: float
    total_network_fees_usd: float
    total_network_fees_sol: float


@dataclass(frozen=True)
class SimulationConfig:
    initial_cash_usd: float = 1_000.0
    max_open_positions: int = 10
    take_profit_multiple: float = 1.60
    stop_loss_multiple: float = 0.90
    max_holding_seconds: int = 2 * 60 * 60

