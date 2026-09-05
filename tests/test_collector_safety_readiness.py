from __future__ import annotations

from copy import deepcopy

import pytest

from backend.app.collector.enrichment import EnrichmentService
from backend.app.collector.models import TokenCandidate


def complete_facts(address: str = "TokenSafety111111111111111111111111111111111") -> dict[str, object]:
    return {
        "token_address": address,
        "launchpad_platform": "Pump.fun",
        "symbol": "SAFE",
        "quote_symbol": "SOL",
        "price": 0.00001,
        "rug_ratio": 0.1,
        "insider_ratio": 0.1,
        "bundler_rate": 0.1,
        "liquidity": 10_000.0,
        "top_10_holder_rate": 0.2,
        "fresh_wallet_rate": 0.1,
        "burn_status": "burn",
        "renounced_mint": 1,
        "renounced_freeze_account": 1,
        "is_wash_trading": False,
        "rat_trader_amount_rate": 0.1,
        "holder_count": 100,
        "market_cap": 20_000.0,
        "sell_tax": 0.01,
        "buy_tax": 0.01,
        "sniper_count": 5,
        "swaps_1h": 20,
        "volume_1h": 10_000.0,
        "volume": 10_000.0,
        "smart_degen_count": 1,
        "renowned_count": 0,
        "age": 10.0,
    }


class ReadinessProvider:
    def __init__(self, bundles: list[dict[str, object]], holders: list[list[dict[str, object]]] | None = None) -> None:
        self.bundles = list(bundles)
        self.holders = list(holders or [[{"addr_type": 0, "rate": 0.04}]])
        self.bundle_calls = 0
        self.holder_calls = 0

    async def token_bundle(self, address: str):
        self.bundle_calls += 1
        index = min(self.bundle_calls - 1, len(self.bundles) - 1)
        return {"token_info": {"data": deepcopy(self.bundles[index])}}

    async def top_holders(self, address: str, limit: int = 20):
        self.holder_calls += 1
        index = min(self.holder_calls - 1, len(self.holders) - 1)
        return deepcopy(self.holders[index])

    async def created_tokens(self, creator: str):
        return {}

    async def klines(self, address: str, from_ts: int, to_ts: int):
        return []


@pytest.mark.asyncio
async def test_transient_missing_security_fact_is_retried_then_can_pass() -> None:
    address = "TokenRetry1111111111111111111111111111111111"
    incomplete = complete_facts(address)
    incomplete.pop("rug_ratio")
    provider = ReadinessProvider([incomplete, incomplete, complete_facts(address)])
    service = EnrichmentService(provider, readiness_attempts=3, readiness_retry_seconds=0)

    result = await service.enrich(TokenCandidate(address, "new_creation", {}), now_ts=1_800_000_000)

    assert result.sample is not None
    assert provider.bundle_calls == 3
    assert provider.holder_calls == 1


@pytest.mark.asyncio
async def test_top10_feature_uses_same_normalized_value_as_safety_filter() -> None:
    address = "TokenTop10Authority11111111111111111111111111111"

    class ConflictingStatProvider(ReadinessProvider):
        async def token_bundle(self, address: str):
            self.bundle_calls += 1
            index = min(self.bundle_calls - 1, len(self.bundles) - 1)
            return {
                "token_info": {"data": deepcopy(self.bundles[index])},
                "stat": {"top_10_holder_rate": 0.06},
            }

    provider = ConflictingStatProvider([complete_facts(address)])
    service = EnrichmentService(provider, readiness_attempts=1, readiness_retry_seconds=0)
    candidate = TokenCandidate(address, "new_creation", {"top_10_holder_rate": 0.24})

    result = await service.enrich(candidate, now_ts=1_800_000_000)

    assert result.sample is not None
    assert result.sample.features["top_10_holder_rate"] == pytest.approx(0.24)


@pytest.mark.asyncio
async def test_persistently_missing_security_fact_is_rejected_after_retry_budget() -> None:
    address = "TokenMissing111111111111111111111111111111111"
    incomplete = complete_facts(address)
    incomplete.pop("rug_ratio")
    provider = ReadinessProvider([incomplete])
    service = EnrichmentService(provider, readiness_attempts=3, readiness_retry_seconds=0)

    result = await service.enrich(TokenCandidate(address, "new_creation", {}), now_ts=1_800_000_000)

    assert result.sample is None
    assert provider.bundle_calls == 3
    assert provider.holder_calls == 0
    assert "missing_or_invalid:rug_ratio" in result.decision.reasons


@pytest.mark.asyncio
async def test_missing_top_holder_fact_is_retried_and_never_defaulted_to_safe() -> None:
    address = "TokenHolder1111111111111111111111111111111111"
    provider = ReadinessProvider(
        [complete_facts(address)],
        holders=[[], [{"addr_type": "bad", "rate": 0.04}], [{"addr_type": 0, "rate": 0.04}]],
    )
    service = EnrichmentService(provider, readiness_attempts=3, readiness_retry_seconds=0)

    result = await service.enrich(TokenCandidate(address, "new_creation", {}), now_ts=1_800_000_000)

    assert result.sample is not None
    assert provider.holder_calls == 3


@pytest.mark.asyncio
async def test_persistently_missing_top_holder_fact_is_rejected_after_retry_budget() -> None:
    address = "TokenNoHolder11111111111111111111111111111111"
    provider = ReadinessProvider([complete_facts(address)], holders=[[]])
    service = EnrichmentService(provider, readiness_attempts=3, readiness_retry_seconds=0)

    result = await service.enrich(TokenCandidate(address, "new_creation", {}), now_ts=1_800_000_000)

    assert result.sample is None
    assert provider.holder_calls == 3
    assert result.decision.reasons == ("missing_or_invalid:top1_addr_type0_rate",)


@pytest.mark.asyncio
async def test_complete_but_unsafe_fact_is_rejected_without_semantic_retry() -> None:
    address = "TokenUnsafe1111111111111111111111111111111111"
    unsafe = complete_facts(address)
    unsafe["rug_ratio"] = 0.3
    provider = ReadinessProvider([unsafe])
    service = EnrichmentService(provider, readiness_attempts=3, readiness_retry_seconds=0)

    result = await service.enrich(TokenCandidate(address, "new_creation", {}), now_ts=1_800_000_000)

    assert result.sample is None
    assert provider.bundle_calls == 1
    assert "rug_ratio<0.2" in result.decision.reasons


@pytest.mark.asyncio
async def test_server_qualified_missing_insider_passes_without_fabricating_value_or_retry() -> None:
    address = "TokenQualified11111111111111111111111111111111"
    facts = complete_facts(address)
    facts.pop("insider_ratio")

    class QualifiedProvider(ReadinessProvider):
        def __init__(self):
            super().__init__([facts])
            self.supplement_calls = 0

        async def supplement_missing_facts(self, address: str, missing_fields):
            self.supplement_calls += 1
            return {}

    provider = QualifiedProvider()
    service = EnrichmentService(provider, readiness_attempts=3, readiness_retry_seconds=0)
    candidate = TokenCandidate(
        address,
        "trending",
        {
            "_server_qualified_facts": {
                "insider_ratio": {
                    "source": "gmgn_market_rank",
                    "predicate": "lt",
                    "limit": 0.2,
                    "request_max_insider_rate": 0.19999999999999998,
                }
            }
        },
    )

    result = await service.enrich(candidate, now_ts=1_800_000_000)

    assert result.sample is not None
    assert provider.bundle_calls == 1
    assert provider.supplement_calls == 1
    assert result.sample.source.get("insider_ratio") is None
    assert "request_max_insider_rate" not in result.sample.source
    assert "insider_ratio" in result.sample.source["_server_qualified_facts"]


@pytest.mark.asyncio
async def test_real_supplemented_insider_is_preferred_over_server_qualification() -> None:
    address = "TokenRealInsider1111111111111111111111111111111"
    facts = complete_facts(address)
    facts.pop("insider_ratio")

    class RealSupplementProvider(ReadinessProvider):
        def __init__(self):
            super().__init__([facts])
            self.supplement_calls = 0

        async def supplement_missing_facts(self, address: str, missing_fields):
            self.supplement_calls += 1
            assert "insider_ratio" in missing_fields
            return {"security": {"data": {"suspected_insider_hold_rate": 0.1}}}

    provider = RealSupplementProvider()
    service = EnrichmentService(provider, readiness_attempts=3, readiness_retry_seconds=0)
    candidate = TokenCandidate(
        address,
        "trending",
        {
            "_server_qualified_facts": {
                "insider_ratio": {
                    "source": "gmgn_market_rank",
                    "predicate": "lt",
                    "limit": 0.2,
                    "request_max_insider_rate": 0.19999999999999998,
                }
            }
        },
    )

    result = await service.enrich(candidate, now_ts=1_800_000_000)

    assert result.sample is not None
    assert provider.bundle_calls == 1
    assert provider.supplement_calls == 1
    assert result.sample.source["suspected_insider_hold_rate"] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_trenches_cannot_use_server_qualification_to_bypass_missing_insider() -> None:
    address = "TokenNoQualTrench111111111111111111111111111111"
    facts = complete_facts(address)
    facts.pop("insider_ratio")
    provider = ReadinessProvider([facts])
    service = EnrichmentService(provider, readiness_attempts=1, readiness_retry_seconds=0)
    candidate = TokenCandidate(
        address,
        "new_creation",
        {
            "_server_qualified_facts": {
                "insider_ratio": {
                    "source": "gmgn_market_rank",
                    "predicate": "lt",
                    "limit": 0.2,
                    "request_max_insider_rate": 0.19999999999999998,
                }
            }
        },
    )

    result = await service.enrich(candidate, now_ts=1_800_000_000)

    assert result.sample is None
    assert "missing_or_invalid:insider_ratio" in result.decision.reasons


@pytest.mark.asyncio
async def test_trending_qualification_must_prove_strict_insider_bound() -> None:
    address = "TokenWeakQual111111111111111111111111111111111"
    facts = complete_facts(address)
    facts.pop("insider_ratio")
    provider = ReadinessProvider([facts])
    service = EnrichmentService(provider, readiness_attempts=1, readiness_retry_seconds=0)
    candidate = TokenCandidate(
        address,
        "trending",
        {
            "_server_qualified_facts": {
                "insider_ratio": {
                    "source": "gmgn_market_rank",
                    "predicate": "lt",
                    "limit": 0.2,
                    "request_max_insider_rate": 0.2,
                }
            }
        },
    )

    result = await service.enrich(candidate, now_ts=1_800_000_000)

    assert result.sample is None
    assert "missing_or_invalid:insider_ratio" in result.decision.reasons
