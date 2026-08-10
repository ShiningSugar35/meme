"""Typed live execution errors; never carry credentials or private keys."""

from __future__ import annotations

from .models import FailureKind


class LiveTradeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: FailureKind,
        code: str | None = None,
        reset_at: int | None = None,
        submission_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.reset_at = reset_at
        self.submission_unknown = submission_unknown


def as_live_error(exc: BaseException, *, submission_phase: bool = False) -> LiveTradeError:
    if isinstance(exc, LiveTradeError):
        if submission_phase and exc.kind in {FailureKind.NETWORK, FailureKind.API}:
            exc.submission_unknown = True
        return exc
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return LiveTradeError(
            "Live trading network operation failed",
            kind=FailureKind.NETWORK,
            submission_unknown=submission_phase,
        )
    return LiveTradeError(
        "Live trading provider operation failed",
        kind=FailureKind.API,
        submission_unknown=submission_phase,
    )

