from __future__ import annotations

import asyncio

from backend.app.collector.enrichment import EnrichmentResult
from backend.app.collector.filters import FilterDecision
from backend.app.collector.models import CollectedSample, TokenCandidate
from backend.app.collector.service import CollectorService


class FakeDiscovery:
    async def discover(self, token_type: str, *, limit: int = 80):
        if token_type != "new_creation":
            return []
        return [TokenCandidate("mint-1", token_type, {"address": "mint-1", "symbol": "MEME"})]


class FakeSink:
    def __init__(self) -> None:
        self.added = []

    async def has_unfinished_address(self, address: str) -> bool:
        return False

    async def add_sample(self, sample) -> None:
        self.added.append(sample)

    async def due_samples(self, now_ts: int):
        return []

    async def save_label(self, result) -> None:
        return None


class RejectAtPrefilter:
    def __init__(self) -> None:
        self.enrich_calls = 0

    def prefilter(self, candidate: TokenCandidate) -> FilterDecision:
        return FilterDecision(False, ("marketcap>5000",))

    async def enrich(self, candidate: TokenCandidate, *, now_ts=None) -> EnrichmentResult:
        self.enrich_calls += 1
        return EnrichmentResult(None, FilterDecision(False, ("unexpected",)))


class RejectAtEnrichment:
    def __init__(self) -> None:
        self.enrich_calls = 0

    def prefilter(self, candidate: TokenCandidate) -> FilterDecision:
        return FilterDecision(True, ())

    async def enrich(self, candidate: TokenCandidate, *, now_ts=None) -> EnrichmentResult:
        self.enrich_calls += 1
        return EnrichmentResult(None, FilterDecision(False, ("swaps_1h>19",)))


class AcceptIncompleteModelPayload:
    def prefilter(self, candidate: TokenCandidate) -> FilterDecision:
        return FilterDecision(True, ())

    async def enrich(self, candidate: TokenCandidate, *, now_ts=None) -> EnrichmentResult:
        sample = CollectedSample(
            address=candidate.address,
            token_type=candidate.token_type,
            entry_time=1900000000,
            entry_price=0.00001,
            launchpad="Pump.fun",
            liquidity=10000.0,
            features={},
            age_minutes=10.0,
            holder_count=100.0,
            feature_snapshot_at=1900000000,
            source={},
        )
        return EnrichmentResult(sample, FilterDecision(True, ()))


def test_prefilter_rejection_skips_enrichment_and_is_counted_separately() -> None:
    enrichment = RejectAtPrefilter()
    events: list[tuple[str, dict[str, object]]] = []
    service = CollectorService(FakeDiscovery(), enrichment, object(), FakeSink())

    report = asyncio.run(service.collect_once(limit=1, event_sink=lambda action, details: events.append((action, dict(details)))))

    assert report.discovered == 1
    assert report.rejected == 1
    assert report.prefilter_rejected == 1
    assert report.enrichment_rejected == 0
    assert enrichment.enrich_calls == 0
    rejected_event = next(details for action, details in events if action == "candidate_rejected")
    assert rejected_event["stage"] == "trench_prefilter"


def test_enrichment_rejection_is_counted_after_prefilter_passes() -> None:
    enrichment = RejectAtEnrichment()
    service = CollectorService(FakeDiscovery(), enrichment, object(), FakeSink())

    report = asyncio.run(service.collect_once(limit=1))

    assert report.discovered == 1
    assert report.rejected == 1
    assert report.prefilter_rejected == 0
    assert report.enrichment_rejected == 1
    assert enrichment.enrich_calls == 1


def test_model_feature_completeness_rejects_before_sink_write() -> None:
    sink = FakeSink()
    events: list[tuple[str, dict[str, object]]] = []
    service = CollectorService(FakeDiscovery(), AcceptIncompleteModelPayload(), object(), sink)

    report = asyncio.run(service.collect_once(limit=1, event_sink=lambda action, details: events.append((action, dict(details)))))

    assert report.discovered == 1
    assert report.accepted == 0
    assert report.rejected == 1
    assert report.enrichment_rejected == 1
    assert sink.added == []
    rejected = next(details for action, details in events if action == "candidate_rejected")
    assert rejected["stage"] == "model_feature_completeness"
    assert any(str(reason).startswith("missing_model_feature:") for reason in rejected["reasons"])
