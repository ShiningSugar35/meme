"""Transport-neutral live trading domain models."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING = "pending"
    PROCESSED = "processed"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    EXPIRED = "expired"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in {self.CONFIRMED, self.FAILED, self.EXPIRED}


class FailureKind(str, Enum):
    NETWORK = "network"
    RATE_LIMIT = "429"
    API = "api"
    VALIDATION = "validation"
    BALANCE = "balance"
    NO_ROUTE = "no_route"
    PENDING = "pending"
    EXPIRED = "expired"
    CHAIN = "chain"


@dataclass(frozen=True, slots=True)
class ExecutionStep:
    """One fully configured execution attempt; values are never inferred."""

    slippage: float
    priority_fee_sol: float
    tip_fee_sol: float

    def __post_init__(self) -> None:
        if not 0 < self.slippage <= 1:
            raise ValueError("slippage must be expressed as a fraction in (0, 1]")
        if self.priority_fee_sol < 0 or self.tip_fee_sol < 0:
            raise ValueError("priority and tip fees cannot be negative")


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """All slippage/fee ladders are application configuration, not constants."""

    buy_steps: tuple[ExecutionStep, ...]
    sell_steps: tuple[ExecutionStep, ...]
    poll_interval_seconds: float = 2.0
    max_polls_per_attempt: int = 30
    read_retry_count: int = 2
    read_retry_backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.buy_steps or not self.sell_steps:
            raise ValueError("buy_steps and sell_steps must each contain at least one configured step")
        if self.max_polls_per_attempt < 1 or self.read_retry_count < 1:
            raise ValueError("poll and read retry counts must be positive")
        if self.poll_interval_seconds < 0 or self.read_retry_backoff_seconds < 0:
            raise ValueError("retry delays cannot be negative")

    def steps_for(self, side: TradeSide) -> tuple[ExecutionStep, ...]:
        return self.buy_steps if side is TradeSide.BUY else self.sell_steps


@dataclass(frozen=True, slots=True)
class SwapIntent:
    chain: str
    wallet_address: str
    input_token: str
    output_token: str
    input_amount_raw: str
    side: TradeSide
    client_order_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.chain.lower() != "sol":
            raise ValueError("This system currently permits Solana live trading only")
        for name, value in (
            ("wallet_address", self.wallet_address),
            ("input_token", self.input_token),
            ("output_token", self.output_token),
            ("client_order_id", self.client_order_id),
        ):
            if not str(value).strip():
                raise ValueError(f"{name} is required")
        try:
            amount = int(self.input_amount_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("input_amount_raw must be an integer in smallest units") from exc
        if amount <= 0:
            raise ValueError("input_amount_raw must be positive")

    def fingerprint(self) -> str:
        payload = {
            "chain": self.chain.lower(),
            "wallet": self.wallet_address,
            "input": self.input_token,
            "output": self.output_token,
            "amount": self.input_amount_raw,
            "side": self.side.value,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class Quote:
    input_token: str
    output_token: str
    input_amount_raw: str
    output_amount_raw: str
    min_output_amount_raw: str
    slippage: float
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class PreparedTransaction:
    payload: bytes | str
    context: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class SignedTransaction:
    payload: bytes | str

    def __repr__(self) -> str:
        return "SignedTransaction(payload=<redacted>)"


@dataclass(frozen=True, slots=True)
class Submission:
    order_id: str
    status: OrderStatus
    tx_hash: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    order_id: str
    status: OrderStatus
    tx_hash: str | None = None
    failure_kind: FailureKind | None = None
    error_code: str | None = None
    error_message: str | None = None
    report: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    client_order_id: str
    order_id: str | None
    status: OrderStatus
    attempts: int
    tx_hash: str | None = None
    failure_kind: FailureKind | None = None
    error_code: str | None = None
    report: Mapping[str, Any] = field(default_factory=dict, repr=False)

