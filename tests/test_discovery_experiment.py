from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from backend.app.collector.discovery_experiment import DiscoveryExperimentManager
from backend.app.collector.filters import FilterDecision
from backend.app.collector.models import CollectedSample, Kline, TokenCandidate
from backend.app.collector.rate_limit import AsyncRateLimiter
from backend.app.database import Database


class FakeTrendingDiscovery:
    def __init__(self, rows): self.rows = rows
    async def discover_trending(self, order_by: str, *, interval: str): return self.rows[order_by]


class FakeEnrichment:
    def __init__(self, now_ts: int): self.now_ts, self.enrich_calls, self.trending_facts = now_ts, [], []
    def prefilter(self, candidate): return FilterDecision(True, ())
    async def enrich(self, candidate, *, trending=None, now_ts=None):
        self.enrich_calls.append(candidate.address)
        self.trending_facts.append(dict(trending or {}))
        sample = CollectedSample(address=candidate.address, token_type="trending", entry_time=int(now_ts or self.now_ts), entry_price=1.0, launchpad="Pump.fun", liquidity=10_000.0, features={"feature": 1.0}, age_minutes=10.0, holder_count=100.0, source=dict(candidate.raw))
        return SimpleNamespace(sample=sample, decision=FilterDecision(True, ()))


class FakeKlineProvider:
    def __init__(self): self.calls = []
    async def klines(self, address, from_ts, to_ts):
        self.calls.append(address)
        entry = to_ts - 90 * 60
        return [Kline(timestamp=entry, high=1.1, low=0.95, close=1.0), Kline(timestamp=entry + 60, high=1.9, low=0.95, close=1.8)]


def _candidate(address: str, source: str) -> TokenCandidate:
    return TokenCandidate(address, "trending", {"address": address, "name": source, "symbol": "MEME", "launchpad_platform": "Pump.fun", "price": 1.0, "liquidity": 10_000, "holder_count": 100, "market_cap": 20_000, "top_10_holder_rate": 0.2, "creation_timestamp": int(time.time()) - 600})


def test_trending_shadow_deduplicates_enrichment_and_never_writes_production_samples(tmp_path):
    db = Database(tmp_path / "experiment.db"); db.initialize()
    manager = DiscoveryExperimentManager(db, AsyncRateLimiter(1000.0)); manager.create_or_resume(max_shadow_enrich_per_cycle=8)
    now = int(time.time()); manager.begin_cycle("cycle-1", observed_at=now); shared = "shared-address"
    discovery = FakeTrendingDiscovery({"volume": [_candidate(shared,"volume")], "smart_degen_count": [_candidate(shared,"smart")], "change5m": [_candidate(shared,"change")]})
    enrichment = FakeEnrichment(now)
    result = asyncio.run(manager.run_trending_cycle(discovery, enrichment)); manager.finish_cycle()
    assert result["accepted_unique"] == 1 and enrichment.enrich_calls == [shared]
    assert db.fetch_one("SELECT COUNT(*) AS count FROM samples")["count"] == 0
    assert db.fetch_one("SELECT COUNT(*) AS count FROM discovery_experiment_samples")["count"] == 1
    for row in db.fetch_all("SELECT entry_time,raw_json FROM discovery_experiment_samples"):
        raw = json.loads(row["raw_json"])
        assert int(row["entry_time"]) >= int(raw["_experiment_source_observed_at"])
    assert [row["samples"] for row in manager.summary()["sources"] if row["source_key"].startswith("trending:")] == [1]


def test_trending_reuses_same_cycle_trenches_missing_facts_without_overriding_rank_price(tmp_path):
    db = Database(tmp_path / "same-cycle.db"); db.initialize()
    manager = DiscoveryExperimentManager(db); manager.create_or_resume(max_shadow_enrich_per_cycle=8)
    now = int(time.time()); manager.begin_cycle("same-cycle", observed_at=now); address = "shared-facts"
    manager.observe_control("candidate_discovered", {
        "source_key": "trenches:new_creation", "address": address, "source_rank": 1,
        "raw": {"address": address, "insider_ratio": 0.11, "price": 0.5},
    })
    discovery = FakeTrendingDiscovery({
        "volume": [TokenCandidate(address, "trending", {"address": address, "price": 1.0})],
        "smart_degen_count": [], "change5m": [],
    })
    enrichment = FakeEnrichment(now)
    result = asyncio.run(manager.run_trending_cycle(discovery, enrichment))
    manager.finish_cycle()
    assert result["accepted_unique"] == 1
    assert enrichment.trending_facts == [{"address": address, "insider_ratio": 0.11, "price": 0.5}]


def test_volume_only_trending_has_no_cross_source_budget_censoring(tmp_path):
    db = Database(tmp_path / "budget.db"); db.initialize(); manager = DiscoveryExperimentManager(db); manager.create_or_resume(max_shadow_enrich_per_cycle=1)
    now=int(time.time()); manager.begin_cycle("cycle-budget", observed_at=now)
    discovery=FakeTrendingDiscovery({"volume":[_candidate("volume-only","volume")],"smart_degen_count":[_candidate("smart-only","smart")],"change5m":[_candidate("change-only","change")]})
    result=asyncio.run(manager.run_trending_cycle(discovery, FakeEnrichment(now))); manager.finish_cycle()
    assert result["deep_enrich_selected"] == 1 and result["budget_censored_unique"] == 0
    assert db.fetch_one("SELECT COUNT(*) AS count FROM discovery_experiment_observations WHERE outcome='budget_censored'")["count"] == 0


def test_control_observer_is_shadow_only(tmp_path):
    db=Database(tmp_path/"control.db"); db.initialize(); manager=DiscoveryExperimentManager(db); manager.create_or_resume(); now=int(time.time()); manager.begin_cycle("control-cycle", observed_at=now)
    sample=CollectedSample(address="control-address",token_type="new_creation",entry_time=now,entry_price=1.0,launchpad="Pump.fun",liquidity=10_000.0,features={"x":1.0},source={"name":"control"})
    manager.observe_control("candidate_discovered",{"source_key":"trenches:new_creation","address":sample.address,"source_rank":1,"raw":dict(sample.source)})
    manager.observe_control("candidate_accepted",{"source_key":"trenches:new_creation","address":sample.address,"sample":sample}); manager.finish_cycle()
    assert db.fetch_one("SELECT COUNT(*) AS count FROM samples")["count"] == 0
    copied=db.fetch_one("SELECT * FROM discovery_experiment_samples"); assert copied["production_sample"] == 1


def test_shadow_labels_group_same_address_into_one_kline_request(tmp_path):
    db=Database(tmp_path/"labels.db"); db.initialize(); now=int(time.time())-90*60-10; manager=DiscoveryExperimentManager(db, clock=lambda: now); manager.create_or_resume(max_shadow_enrich_per_cycle=8)
    manager.begin_cycle("label-cycle",observed_at=now); shared="label-shared"
    discovery=FakeTrendingDiscovery({"volume":[_candidate(shared,"volume")],"smart_degen_count":[_candidate(shared,"smart")],"change5m":[]})
    asyncio.run(manager.run_trending_cycle(discovery,FakeEnrichment(now))); manager.finish_cycle(); provider=FakeKlineProvider()
    finalized=asyncio.run(manager.finalize_due(provider,now_ts=now+90*60+10))
    assert finalized == 1 and provider.calls == [shared]
    rows=db.fetch_all("SELECT label_status,tag FROM discovery_experiment_samples ORDER BY source_key"); assert [(r["label_status"],r["tag"]) for r in rows] == [("mature",1)]


def test_shared_limiter_delta_and_route_metrics_are_persisted(tmp_path):
    db=Database(tmp_path/"api.db"); db.initialize(); limiter=AsyncRateLimiter(1000.0); manager=DiscoveryExperimentManager(db,limiter); manager.create_or_resume(); manager.begin_cycle("api-cycle")
    asyncio.run(limiter.acquire(3)); manager.record_api_event({"path":"/v1/trenches","route_weight":3,"latency_ms":12,"outcome":"success"}); manager.record_api_event({"path":"/v1/market/rank","route_weight":1,"latency_ms":8,"outcome":"rate_limited","rate_limited":True}); manager.finish_cycle()
    metrics={r["route_key"]:r for r in db.fetch_all("SELECT * FROM discovery_experiment_api_metrics")}
    assert metrics["/v1/trenches"]["weighted_units"] == 3 and metrics["/v1/market/rank"]["rate_limited_count"] == 1 and metrics["__shared_global__"]["weighted_units"] == 3
