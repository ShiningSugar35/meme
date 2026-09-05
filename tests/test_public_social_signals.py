from __future__ import annotations

import math

import pytest

from backend.app.services.public_social_signals import (
    PublicSignalEndpoint,
    PublicSocialSignalProvider,
)


ADDRESS = "token-address"
ENTRY = 1_800_000_000


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    async def get(self, url: str):
        self.calls.append(url)
        for suffix, payload in self.payloads.items():
            if url.endswith(suffix):
                return FakeResponse(payload)
        return FakeResponse({}, 404)


@pytest.mark.asyncio
async def test_public_social_snapshot_is_pit_exact_address_and_cached() -> None:
    endpoints = (
        PublicSignalEndpoint("exchange", "/exchange", "exchange"),
        PublicSignalEndpoint("fomo", "/fomo", "trade"),
        PublicSignalEndpoint("news", "/news", "news"),
    )
    client = FakeClient(
        {
            "/exchange": {
                "events": [
                    {
                        "createdAt": ENTRY - 60,
                        "tokenAddress": ADDRESS,
                        "handle": "alice",
                        "followers": 1_000,
                        "side": "buy",
                        "usd": 100,
                    },
                    {
                        "createdAt": ENTRY + 1,
                        "tokenAddress": ADDRESS,
                        "handle": "future",
                        "followers": 1_000_000,
                    },
                ]
            },
            "/fomo": [
                {
                    "ts": ENTRY - 400,
                    "content": f"watch {ADDRESS}",
                    "handle": "bob",
                    "followers": 500,
                    "side": "sell",
                    "usd": 40,
                }
            ],
            "/news": {
                "events": [
                    {"createdAt": ENTRY - 30, "content": "unrelated market news"},
                ]
            },
        }
    )
    provider = PublicSocialSignalProvider(
        base_url="https://example.invalid",
        endpoints=endpoints,
        cache_seconds=60,
        client=client,
    )
    first = await provider.snapshot(ADDRESS, entry_time=ENTRY)
    second = await provider.snapshot(ADDRESS, entry_time=ENTRY)

    assert first.matched_events == 2
    assert first.successful_sources == ("exchange", "fomo", "news")
    assert first.failed_sources == ()
    assert first.features["ln(monitor_mentions_5m+1)"] == pytest.approx(math.log(2.0))
    assert first.features["ln(monitor_mentions_15m+1)"] == pytest.approx(math.log(3.0))
    assert first.features["monitor_unique_sources_15m"] == 2.0
    assert first.features["ln(monitor_unique_authors_15m+1)"] == pytest.approx(math.log(3.0))
    assert first.features["ln(monitor_follower_reach_15m+1)"] == pytest.approx(math.log1p(1_500))
    assert first.features["monitor_mention_accel_5m_vs_15m"] == pytest.approx(0.1)
    assert first.features["ln(monitor_latest_mention_age_s+1)"] == pytest.approx(math.log1p(60))
    assert first.features["monitor_fomo_buy_ratio_15m"] == pytest.approx(0.0)
    assert first.features["monitor_fomo_usd_imbalance_15m"] == pytest.approx(-1.0)
    assert first.features["ln(monitor_fomo_usd_15m+1)"] == pytest.approx(math.log1p(40))
    assert first.features["monitor_exchange_hits_15m"] == 1.0
    assert first.features["monitor_news_hits_15m"] == 0.0
    assert first.features["monitor_source_coverage"] == 1.0
    assert first.features["ln(monitor_global_events_5m+1)"] == pytest.approx(math.log1p(2))
    assert first.features == second.features
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_failed_sources_are_missing_coverage_not_zero_signal() -> None:
    endpoints = (
        PublicSignalEndpoint("ok", "/ok", "social"),
        PublicSignalEndpoint("down", "/down", "social"),
    )
    client = FakeClient({"/ok": {"events": []}})
    provider = PublicSocialSignalProvider(
        base_url="https://example.invalid",
        endpoints=endpoints,
        client=client,
    )
    snapshot = await provider.snapshot(ADDRESS, entry_time=ENTRY)
    assert snapshot.successful_sources == ("ok",)
    assert snapshot.failed_sources == ("down",)
    assert snapshot.features["monitor_source_coverage"] == pytest.approx(0.5)
    assert snapshot.features["ln(monitor_mentions_5m+1)"] == 0.0


@pytest.mark.asyncio
async def test_all_sources_down_keeps_features_missing() -> None:
    endpoints = (PublicSignalEndpoint("down", "/down", "social"),)
    provider = PublicSocialSignalProvider(
        base_url="https://example.invalid",
        endpoints=endpoints,
        client=FakeClient({}),
    )
    snapshot = await provider.snapshot(ADDRESS, entry_time=ENTRY)
    assert snapshot.successful_sources == ()
    assert snapshot.failed_sources == ("down",)
    assert snapshot.features["monitor_source_coverage"] is None
    assert snapshot.features["ln(monitor_mentions_5m+1)"] is None
