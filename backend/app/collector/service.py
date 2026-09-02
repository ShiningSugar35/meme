"""Collector orchestration with repository protocols."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, Sequence

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
    prefilter_rejected: int = 0
    enrichment_rejected: int = 0
    rejection_reasons: Mapping[str, int] = field(default_factory=dict)
    type_stats: Mapping[str, Mapping[str, int]] = field(default_factory=dict)


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

    async def collect_once(
        self,
        *,
        limit: int = 80,
        now_ts: int | None = None,
        event_sink: Callable[[str, Mapping[str, object]], None] | None = None,
        observation_sink: Callable[[str, Mapping[str, object]], None] | None = None,
    ) -> CollectionReport:
        discovered = accepted = rejected = duplicates = 0
        prefilter_rejected = enrichment_rejected = 0
        rejection_reasons: Counter[str] = Counter()
        type_stats: dict[str, dict[str, int]] = {}

        def emit(action: str, details: Mapping[str, object]) -> None:
            if event_sink is not None:
                event_sink(action, details)

        def observe(action: str, details: Mapping[str, object]) -> None:
            if observation_sink is not None:
                observation_sink(action, details)

        for token_type in DISCOVERY_TYPES:
            emit("discovery_start", {"token_type": token_type, "requested_limit": limit})
            candidates = await self.discovery.discover(token_type, limit=limit)
            discovered += len(candidates)
            current = {
                "returned": len(candidates),
                "accepted": 0,
                "rejected": 0,
                "prefilter_rejected": 0,
                "enrichment_rejected": 0,
                "duplicates": 0,
            }
            type_stats[token_type] = current
            emit(
                "discovery_result",
                {"token_type": token_type, "returned": len(candidates), "requested_limit": limit},
            )
            for source_rank, candidate in enumerate(candidates, start=1):
                observe("candidate_discovered", {
                    "source_key": f"trenches:{token_type}",
                    "source_kind": "trenches",
                    "token_type": token_type,
                    "address": candidate.address,
                    "source_rank": source_rank,
                    "raw": dict(candidate.raw),
                })
                token_label = str(
                    candidate.raw.get("symbol")
                    or candidate.raw.get("name")
                    or candidate.address[:8]
                )[:32]
                if await self.sink.has_unfinished_address(candidate.address):
                    duplicates += 1
                    current["duplicates"] += 1
                    emit("candidate_duplicate", {"token_type": token_type, "token": token_label})
                    observe("candidate_duplicate", {"source_key": f"trenches:{token_type}", "address": candidate.address})
                    continue
                prefilter = self.enrichment.prefilter(candidate)
                if not prefilter.accepted:
                    rejected += 1
                    prefilter_rejected += 1
                    current["rejected"] += 1
                    current["prefilter_rejected"] += 1
                    reasons = tuple(prefilter.reasons or ("unspecified",))
                    rejection_reasons.update(reasons)
                    emit(
                        "candidate_rejected",
                        {
                            "token_type": token_type,
                            "token": token_label,
                            "stage": "trench_prefilter",
                            "reasons": list(reasons),
                        },
                    )
                    observe("candidate_prefilter_rejected", {
                        "source_key": f"trenches:{token_type}", "address": candidate.address, "reasons": list(reasons)
                    })
                    continue
                observe("candidate_prefilter_passed", {"source_key": f"trenches:{token_type}", "address": candidate.address})
                result = await self.enrichment.enrich(candidate, now_ts=now_ts)
                if result.sample is None:
                    rejected += 1
                    enrichment_rejected += 1
                    current["rejected"] += 1
                    current["enrichment_rejected"] += 1
                    reasons = tuple(result.decision.reasons or ("unspecified",))
                    rejection_reasons.update(reasons)
                    emit(
                        "candidate_rejected",
                        {
                            "token_type": token_type,
                            "token": token_label,
                            "stage": "enrichment",
                            "reasons": list(reasons),
                        },
                    )
                    observe("candidate_enrichment_rejected", {
                        "source_key": f"trenches:{token_type}", "address": candidate.address, "reasons": list(reasons)
                    })
                    continue
                await self.sink.add_sample(result.sample)
                accepted += 1
                current["accepted"] += 1
                observe("candidate_accepted", {
                    "source_key": f"trenches:{token_type}",
                    "address": candidate.address,
                    "sample": result.sample,
                })
                emit("candidate_accepted", {"token_type": token_type, "token": token_label})
            emit("discovery_type_complete", {"token_type": token_type, **current})
        return CollectionReport(
            discovered,
            accepted,
            rejected,
            duplicates,
            prefilter_rejected=prefilter_rejected,
            enrichment_rejected=enrichment_rejected,
            rejection_reasons=dict(rejection_reasons.most_common()),
            type_stats=type_stats,
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

