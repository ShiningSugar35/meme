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
    """Dynamic GMGN key pools with deterministic rotation and fallback."""

    discovery: tuple[ApiSlot, ApiSlot]
    position_monitor: tuple[ApiSlot, ...]
    discovery_fallback: ApiSlot
    realtime: tuple[ApiSlot, ...]
    realtime_fallback: tuple[ApiSlot, ...]
    kline: tuple[ApiSlot, ...]

    @classmethod
    def from_secrets(cls, secrets: Sequence[str]) -> "ApiKeyRoles":
        clean = [str(value).strip() for value in secrets if str(value).strip()]
        if not clean:
            raise CollectorValidationError("At least one GMGN API key is required")
        slots = tuple(ApiSlot(index, value) for index, value in enumerate(clean))

        def pick(index: int) -> ApiSlot:
            return slots[index % len(slots)]

        # All pools may reuse the same underlying keys when only a few are
        # configured. A shared per-IP limiter still caps aggregate throughput;
        # the larger pool merely spreads auth/key-specific failures and hotspots.
        return cls(
            discovery=(pick(0), pick(1)),
            position_monitor=slots,
            discovery_fallback=pick(2),
            realtime=slots,
            realtime_fallback=slots,
            kline=slots,
        )

    @property
    def kline_fallback(self) -> tuple[ApiSlot, ...]:
        ordered = (self.discovery_fallback, *self.realtime_fallback)
        unique: dict[int, ApiSlot] = {}
        for slot in ordered:
            unique.setdefault(slot.index, slot)
        return tuple(unique.values())

    @property
    def all_slots(self) -> tuple[ApiSlot, ...]:
        ordered = (
            *self.discovery,
            *self.position_monitor,
            self.discovery_fallback,
            *self.realtime,
            *self.realtime_fallback,
            *self.kline,
        )
        unique: dict[int, ApiSlot] = {}
        for slot in ordered:
            unique.setdefault(slot.index, slot)
        return tuple(unique.values())


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

