"""Local high-fidelity paper execution; no network or secret access."""

from .engine import TradingSimulator
from .quote import QuoteModelConfig, SimulatedQuoteProvider
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

__all__ = [
    "EntrySignal",
    "ExecutionQuote",
    "ExitReason",
    "FailureCategory",
    "MarketCandle",
    "PortfolioSnapshot",
    "QuoteModelConfig",
    "QuoteProvider",
    "QuoteRequest",
    "Side",
    "SimulatedPosition",
    "SimulatedQuoteProvider",
    "SimulationConfig",
    "SimulationEvent",
    "TradingSimulator",
]

