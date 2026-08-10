"""Transport-neutral collector models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .errors import CollectorValidationError


@dataclass(frozen=True, slots=True, repr=False)
class ApiSlot:
    index: int
    secret: str

    def __post_init__(self) -> None:
        if not self.secret.strip():
            raise CollectorValidationError(f"GMGN API slot {self.index} is empty")

    def __repr__(self) -> str:
        return f"ApiSlot(index={self.index}, secret=<redacted>)"


@dataclass(frozen=True, slots=True)
class ApiKeyRoles:
    """The 12-key allocation frozen by the source collector."""

    discovery: tuple[ApiSlot, ApiSlot, ApiSlot]
    discovery_fallback: ApiSlot
    realtime: tuple[ApiSlot, ApiSlot, ApiSlot, ApiSlot]
    realtime_fallback: tuple[ApiSlot, ApiSlot]
    kline: tuple[ApiSlot, ApiSlot]

    @classmethod
    def from_secrets(cls, secrets: Sequence[str]) -> "ApiKeyRoles":
        clean = [str(value).strip() for value in secrets if str(value).strip()]
        if len(clean) < 12:
            raise CollectorValidationError(
                "At least 12 GMGN API keys are required for the frozen role layout"
            )
        slots = tuple(ApiSlot(index, secret) for index, secret in enumerate(clean[:12]))
        return cls(
            discovery=(slots[0], slots[1], slots[2]),
            discovery_fallback=slots[3],
            realtime=(slots[4], slots[5], slots[6], slots[7]),
            realtime_fallback=(slots[8], slots[9]),
            kline=(slots[10], slots[11]),
        )

    @property
    def kline_fallback(self) -> tuple[ApiSlot, ApiSlot, ApiSlot]:
        return (self.discovery_fallback, *self.realtime_fallback)

    @property
    def all_slots(self) -> tuple[ApiSlot, ...]:
        return (
            *self.discovery,
            self.discovery_fallback,
            *self.realtime,
            *self.realtime_fallback,
            *self.kline,
        )


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    data: Any
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TokenCandidate:
    address: str
    token_type: str
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CollectedSample:
    address: str
    token_type: str
    entry_time: int
    entry_price: float
    launchpad: str
    liquidity: float
    features: Mapping[str, Any]
    source: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class Kline:
    timestamp: int
    high: float | None
    low: float | None
    close: float | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Kline":
        def first(*keys: str) -> Any:
            for key in keys:
                if value.get(key) not in (None, ""):
                    return value[key]
            return None

        def number(raw: Any) -> float | None:
            try:
                return float(raw) if raw not in (None, "") else None
            except (TypeError, ValueError):
                return None

        raw_time = first("open_time", "time", "timestamp", "t")
        try:
            timestamp = int(float(raw_time))
        except (TypeError, ValueError) as exc:
            raise CollectorValidationError("Kline has no valid timestamp") from exc
        if timestamp > 10_000_000_000:
            timestamp //= 1_000
        return cls(
            timestamp=timestamp,
            high=number(first("high", "h")),
            low=number(first("low", "l")),
            close=number(first("close", "c")),
        )

