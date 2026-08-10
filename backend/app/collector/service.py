"""Collector orchestration with repository protocols."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence

from .constants import DISCOVERY_TYPES
from .discovery import DiscoveryService
from .enrichment import EnrichmentService, EnrichmentProvider
from .labels import LabelFinalizer, PriceWindowResult
from .models import CollectedSample


class SampleSink(Protocol):
    async def has_unfinished_address(self, address: str) -> bool: ...

    async def add_sample(self, sample: CollectedSample) -> None: ...

    async def due_samples(self, now_ts: int) -> Sequence[CollectedSample]: ...

    async def save_label(self, result: PriceWindowResult) -> None: ...


@dataclass(frozen=True, slots=True)
class CollectionReport:
    discovered: int
    accepted: int
    rejected: int
    unfinished_duplicates: int
    finalized: int = 0
    rejection_reasons: Mapping[str, int] = field(default_factory=dict)


class CollectorService:
    def __init__(
        self,
        discovery: DiscoveryService,
        enrichment: EnrichmentService,
        provider: EnrichmentProvider,
        sink: SampleSink,
        finalizer: LabelFinalizer | None = None,
    ) -> None:
        self.discovery = discovery
        self.enrichment = enrichment
        self.provider = provider
        self.sink = sink
        self.finalizer = finalizer or LabelFinalizer()

    async def collect_once(self, *, limit: int = 80, now_ts: int | None = None) -> CollectionReport:
        discovered = accepted = rejected = duplicates = 0
        rejection_reasons: Counter[str] = Counter()
        for token_type in DISCOVERY_TYPES:
            candidates = await self.discovery.discover(token_type, limit=limit)
            discovered += len(candidates)
            for candidate in candidates:
                if await self.sink.has_unfinished_address(candidate.address):
                    duplicates += 1
                    continue
                result = await self.enrichment.enrich(candidate, now_ts=now_ts)
                if result.sample is None:
                    rejected += 1
                    rejection_reasons.update(result.decision.reasons or ("unspecified",))
                    continue
                await self.sink.add_sample(result.sample)
                accepted += 1
        return CollectionReport(
            discovered,
            accepted,
            rejected,
            duplicates,
            rejection_reasons=dict(rejection_reasons.most_common()),
        )

    async def finalize_due(self, *, now_ts: int | None = None) -> CollectionReport:
        current = int(now_ts or time.time())
        finalized = 0
        for sample in await self.sink.due_samples(current):
            if not self.finalizer.is_due(sample, current):
                continue
            policy = self.finalizer.policy
            klines = await self.provider.klines(
                sample.address,
                sample.entry_time - policy.history_seconds,
                sample.entry_time + policy.window_seconds,
            )
            result = self.finalizer.finalize(sample, klines)
            await self.sink.save_label(result)
            finalized += 1
        return CollectionReport(0, 0, 0, 0, finalized=finalized)

