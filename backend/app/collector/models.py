"""Transport-neutral collector models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .constants import FEATURE_SCHEMA_VERSION
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

        # With a sufficiently large pool, reserve three credentials for the
        # production-critical discovery path. Realtime/Kline/PositionMonitor
        # traffic must not cool those keys before the next Trenches request.
        # Small deployments retain the historical shared-pool behavior.
        auxiliary = slots[3:] if len(slots) >= 6 else slots
        return cls(
            discovery=(pick(0), pick(1)),
            position_monitor=auxiliary,
            discovery_fallback=pick(2),
            realtime=auxiliary,
            realtime_fallback=auxiliary,
            kline=auxiliary,
        )

    @property
    def kline_fallback(self) -> tuple[ApiSlot, ...]:
        # Never borrow the reserved discovery fallback into the Kline pool when
        # role isolation is active. For small shared pools realtime_fallback
        # already contains the same credentials.
        ordered = (*self.kline, *self.realtime_fallback)
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
    age_minutes: float | None = None
    holder_count: float | None = None
    feature_schema_version: str = FEATURE_SCHEMA_VERSION
    feature_snapshot_at: int | None = None
    source: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class Kline:
    # Keep the original positional field order for backward compatibility with
    # existing tests/helpers. New OHLCV facts are keyword-friendly extensions.
    timestamp: int
    high: float | None
    low: float | None
    close: float | None
    open: float | None = None
    volume: float | None = None

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
            open=number(first("open", "o")),
            volume=number(first("volume", "v", "volume_usd", "quote_volume")),
        )
