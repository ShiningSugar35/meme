"""Typed collector failures used by schedulers and audit logging."""

from __future__ import annotations

from typing import Any


class CollectorError(RuntimeError):
    category = "collector"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class CollectorNetworkError(CollectorError):
    category = "network"


class CollectorRateLimitError(CollectorError):
    category = "rate_limit"

    def __init__(self, message: str, *, reset_at: int | None = None):
        super().__init__(message, details={"reset_at": reset_at})
        self.reset_at = reset_at


class CollectorAPIError(CollectorError):
    category = "api"

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message, details={"status_code": status_code, "code": code})
        self.status_code = status_code
        self.code = code


class CollectorValidationError(CollectorError):
    category = "validation"

