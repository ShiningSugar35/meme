"""Service-oriented GMGN data collection primitives.

Secrets and persistence deliberately live outside this package.  Callers inject
API credentials, transports and repositories, which keeps collection testable
without making a network request.
"""

from .client import CollectorEndpoints, GMGNDataClient, HttpxTransport
from .constants import DISCOVERY_TYPES, LAUNCHPADS, FilterThresholds, LabelPolicy
from .discovery import DiscoveryService
from .enrichment import EnrichmentService, GMGNEnrichmentProvider
from .errors import (
    CollectorAPIError,
    CollectorError,
    CollectorNetworkError,
    CollectorRateLimitError,
    CollectorValidationError,
)
from .filters import FilterDecision, SafetyFilter
from .labels import LabelFinalizer, PriceWindowResult
from .models import ApiKeyRoles, ApiSlot, CollectedSample, Kline
from .rate_limit import AsyncRateLimiter
from .service import CollectorService, CollectionReport

__all__ = [
    "ApiKeyRoles",
    "ApiSlot",
    "AsyncRateLimiter",
    "CollectedSample",
    "CollectionReport",
    "CollectorAPIError",
    "CollectorEndpoints",
    "CollectorError",
    "CollectorNetworkError",
    "CollectorRateLimitError",
    "CollectorService",
    "CollectorValidationError",
    "DISCOVERY_TYPES",
    "DiscoveryService",
    "EnrichmentService",
    "FilterDecision",
    "FilterThresholds",
    "GMGNDataClient",
    "GMGNEnrichmentProvider",
    "HttpxTransport",
    "Kline",
    "LAUNCHPADS",
    "LabelFinalizer",
    "LabelPolicy",
    "PriceWindowResult",
    "SafetyFilter",
]
