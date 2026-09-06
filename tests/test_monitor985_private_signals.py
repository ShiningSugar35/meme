from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from backend.app.services.browser_session import BrowserCredentialSnapshot
from backend.app.services.monitor985_private_signals import Monitor985PrivateSignalProvider


@dataclass
class FakeResponse:
    status_code: int
    payload: object

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, *, entry_time: int, address: str) -> None:
        self.entry_time = entry_time
        self.address = address
        self.posts: list[dict[str, object]] = []
        self.get_headers: list[dict[str, str]] = []

    async def post(self, url: str, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return FakeResponse(
            200,
            {
                "ok": True,
                "config": {"account": {"userId": "w"}},
                "session": {"token": "s", "clientId": "c", "expiresAt": (self.entry_time + 86_400) * 1000},
            },
        )

    async def get(self, url: str, **kwargs):
        self.get_headers.append(dict(kwargs.get("headers") or {}))
        ts = (self.entry_time - 60) * 1000
        if "pump-trade-events" in url:
            return FakeResponse(
                200,
                {
                    "events": [
                        {
                            "eventType": "PUMP_TRADE",
                            "ts": ts,
                            "content": {"pumpTrade": {"side": "buy", "chainId": 1399811149, "mint": self.address, "wallet": "wa", "amountUsd": 120}},
                        },
                        {
                            "eventType": "PUMP_TRADE",
                            "ts": (self.entry_time - 240) * 1000,
                            "content": {"pumpTrade": {"side": "sell", "chainSlug": "sol", "mint": self.address, "wallet": "wb", "amountUsd": 40}},
                        },
                        {
                            "eventType": "PUMP_TRADE",
                            "ts": ts,
                            "content": {"pumpTrade": {"side": "buy", "chainId": 56, "mint": self.address, "wallet": "wbsc", "amountUsd": 999}},
                        },
                    ]
                },
            )
        return FakeResponse(
            200,
            {
                "events": [
                    {"eventType": "FOMO_BUY", "ts": ts, "chainName": "Solana", "tokenAddress": self.address, "handle": "alpha", "usd": 90},
                    {
                        "eventType": "FOMO_SELL",
                        "createdAt": datetime.fromtimestamp(self.entry_time - 300, tz=timezone.utc).isoformat(),
                        "chainName": "sol",
                        "tokenAddress": self.address,
                        "handle": "beta",
                        "usd": 30,
                    },
                ]
            },
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_monitor985_account_session_is_readonly_and_features_are_pit(monkeypatch) -> None:
    entry_time = 1_800_000_000
    address = "SoLMintFixture111111111111111111111111111"
    client = FakeClient(entry_time=entry_time, address=address)

    monkeypatch.setattr(
        "backend.app.services.monitor985_private_signals.read_local_storage",
        lambda _origin, _keys: BrowserCredentialSnapshot(
            profile="chrome:Default",
            values={
                "xMonitorWalletAddress": "w",
                "xMonitorWalletToken": "t",
                "xMonitorFomoMutedV1": "[]",
                "xMonitorFomoPrefsV1": "{}",
                "xMonitorPumpMutedV1": "[]",
                "xMonitorPumpPrefsV1": "{}",
                "xMonitorPumpOnlyMineV1": "true",
            },
            source="fixture",
        ),
    )
    provider = Monitor985PrivateSignalProvider(client=client)
    snapshot = await provider.snapshot(address, entry_time=entry_time)

    assert snapshot.connected is True
    assert snapshot.matched_events == 4
    assert snapshot.features["monitor_private_fomo_buy_ratio_15m"] == pytest.approx(0.5)
    assert snapshot.features["monitor_private_pump_buy_ratio_15m"] == pytest.approx(0.5)
    assert snapshot.features["monitor_private_fomo_usd_imbalance_15m"] == pytest.approx(0.5)
    assert snapshot.features["monitor_private_pump_usd_imbalance_15m"] == pytest.approx(0.5)
    assert snapshot.features["monitor_private_source_coverage"] == 1.0
    assert len(client.posts) == 1
    assert str(client.posts[0]["url"]).endswith("/api/extension/session")
    assert client.posts[0]["headers"]["X-User-Token"] == "t"
    assert all(item.get("Authorization") == "Bearer s" for item in client.get_headers)
    assert "Bearer s" not in repr(snapshot)
    await provider.close()


@pytest.mark.asyncio
async def test_monitor985_login_missing_is_nonblocking_missing_signal(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.services.monitor985_private_signals.read_local_storage",
        lambda _origin, _keys: BrowserCredentialSnapshot(None, {}, "fixture"),
    )
    provider = Monitor985PrivateSignalProvider(client=FakeClient(entry_time=1_800_000_000, address="x"))
    snapshot = await provider.snapshot("x", entry_time=1_800_000_000)
    assert snapshot.connected is False
    assert snapshot.failed_sources == ("login_required",)
    assert all(value is None for value in snapshot.features.values())
    await provider.close()


@pytest.mark.asyncio
async def test_monitor985_future_event_is_never_used(monkeypatch) -> None:
    entry_time = 1_800_000_000
    address = "SoLFutureFixture11111111111111111111111111"

    class FutureClient(FakeClient):
        async def get(self, url: str, **kwargs):
            return FakeResponse(
                200,
                {"events": [{"eventType": "FOMO_BUY", "ts": (entry_time + 10) * 1000, "chainName": "Solana", "tokenAddress": address, "usd": 999}]},
            )

    monkeypatch.setattr(
        "backend.app.services.monitor985_private_signals.read_local_storage",
        lambda _origin, _keys: BrowserCredentialSnapshot(
            "chrome:Default", {"xMonitorWalletAddress": "w", "xMonitorWalletToken": "t"}, "fixture"
        ),
    )
    provider = Monitor985PrivateSignalProvider(client=FutureClient(entry_time=entry_time, address=address))
    snapshot = await provider.snapshot(address, entry_time=entry_time)
    assert snapshot.connected is True
    assert snapshot.matched_events == 0
    assert snapshot.features["ln(monitor_private_fomo_events_15m+1)"] == 0.0
    await provider.close()


@pytest.mark.asyncio
async def test_monitor985_transient_session_and_feed_failures_retry_once(monkeypatch) -> None:
    entry_time = 1_800_000_000
    address = "SoLRetryFixture111111111111111111111111111"

    class RetryClient(FakeClient):
        def __init__(self, *, entry_time: int, address: str) -> None:
            super().__init__(entry_time=entry_time, address=address)
            self.post_attempts = 0
            self.pump_attempts = 0

        async def post(self, url: str, **kwargs):
            self.post_attempts += 1
            if self.post_attempts == 1:
                return FakeResponse(503, {})
            return await super().post(url, **kwargs)

        async def get(self, url: str, **kwargs):
            if "pump-trade-events" in url:
                self.pump_attempts += 1
                if self.pump_attempts == 1:
                    return FakeResponse(503, {})
            return await super().get(url, **kwargs)

    monkeypatch.setattr(
        "backend.app.services.monitor985_private_signals.read_local_storage",
        lambda _origin, _keys: BrowserCredentialSnapshot(
            "chrome:Default", {"xMonitorWalletAddress": "w", "xMonitorWalletToken": "t"}, "fixture"
        ),
    )
    client = RetryClient(entry_time=entry_time, address=address)
    provider = Monitor985PrivateSignalProvider(client=client, retry_delay_seconds=0)
    snapshot = await provider.snapshot(address, entry_time=entry_time)
    assert client.post_attempts == 2
    assert client.pump_attempts == 2
    assert snapshot.connected is True
    assert snapshot.failed_sources == ()
    assert snapshot.matched_events == 4
    assert snapshot.features["monitor_private_source_coverage"] == 1.0
    await provider.close()


@pytest.mark.asyncio
async def test_monitor985_mixed_chain_full_page_stays_incomplete(monkeypatch) -> None:
    entry_time = 1_800_000_000
    address = "SoLTruncatedFixture111111111111111111111111"

    class TruncatedClient(FakeClient):
        async def get(self, url: str, **kwargs):
            events = []
            for index in range(150):
                ts = (entry_time - 30 - index) * 1000
                if "pump-trade-events" in url:
                    chain_id = 1399811149 if index < 10 else 56
                    events.append(
                        {
                            "eventType": "PUMP_TRADE",
                            "ts": ts,
                            "content": {
                                "pumpTrade": {
                                    "side": "buy",
                                    "chainId": chain_id,
                                    "mint": address,
                                    "wallet": f"w{index}",
                                    "amountUsd": 1,
                                }
                            },
                        }
                    )
                else:
                    events.append(
                        {
                            "eventType": "FOMO_BUY",
                            "ts": ts,
                            "chainName": "Solana" if index < 10 else "BSC",
                            "tokenAddress": address,
                            "handle": f"h{index}",
                            "usd": 1,
                        }
                    )
            return FakeResponse(200, {"events": events})

    monkeypatch.setattr(
        "backend.app.services.monitor985_private_signals.read_local_storage",
        lambda _origin, _keys: BrowserCredentialSnapshot(
            "chrome:Default", {"xMonitorWalletAddress": "w", "xMonitorWalletToken": "t"}, "fixture"
        ),
    )
    provider = Monitor985PrivateSignalProvider(client=TruncatedClient(entry_time=entry_time, address=address))
    snapshot = await provider.snapshot(address, entry_time=entry_time)
    assert snapshot.connected is True
    assert snapshot.incomplete_sources == ("private_fomo", "private_pump")
    assert snapshot.features["monitor_private_source_coverage"] == 0.0
    assert snapshot.features["ln(monitor_private_fomo_events_15m+1)"] is None
    assert snapshot.features["ln(monitor_private_pump_events_15m+1)"] is None
    await provider.close()
