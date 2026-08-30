from __future__ import annotations

import math
from pathlib import Path

import httpx
import pytest

from backend.app.collector.client import CollectorEndpoints
from backend.app.collector.constants import FEATURE_SCHEMA_VERSION
from backend.app.collector.dexscreener_fallback import DexScreenerFallbackProvider
from backend.app.collector.errors import CollectorAPIError
from backend.app.collector.event_features import build_gmgn_event_features
from backend.app.collector.market_regime import GMGNMarketRegimeProvider
from backend.app.collector.models import ApiKeyRoles, Kline
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.ml.family_audit import FeatureFamilyAuditService, LEGACY_BASELINE_FEATURES
from backend.app.ml.features import AVAILABLE_MODEL_FEATURES, DEFAULT_MODEL_TRAINING_FEATURES
from backend.app.services.adaptive_policy import AdaptivePolicyService, adaptive_threshold
from backend.app.services.crypto_market import CoinbasePublicMarketProvider
from backend.app.services.solana_rpc import RpcEndpoint, SolanaRpcPool, configured_rpc_endpoints
from backend.app.services.training import TrainingService


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "event-regime.db")
    database.initialize()
    return database


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "event-regime.db"),
        adaptive_policy_enabled=True,
        adaptive_min_regime_snapshots=1,
        adaptive_min_confidence=0.10,
        adaptive_exploration_rate=0.0,
    )
    values.update(overrides)
    return Settings(**values)


def test_event1m_features_use_only_completed_history() -> None:
    entry_time = 1_000
    klines = (
        Kline(timestamp=820, high=1.1, low=0.9, close=1.0, open=0.95, volume=10.0),
        Kline(timestamp=880, high=1.2, low=1.0, close=1.1, open=1.0, volume=20.0),
        Kline(timestamp=940, high=1.3, low=1.1, close=1.2, open=1.1, volume=30.0),
        # This bucket has started but cannot have closed at entry_time and must
        # not participate in the entry snapshot.
        Kline(timestamp=1_000, high=9.0, low=0.1, close=9.0, open=1.2, volume=9_999.0),
    )
    features = build_gmgn_event_features(
        {
            "volume_1m": 30,
            "swaps_1m": 6,
            "buys_1m": 4,
            "sells_1m": 2,
            "buy_volume_1m": 21,
            "sell_volume_1m": 9,
        },
        current_price=1.2,
        entry_time=entry_time,
        history_klines=klines,
    )

    assert features["price_change_1m"] == pytest.approx(1.2 / 1.1 - 1.0)
    assert "ln(volume_2m+1)" not in features
    assert "volume_acceleration_2m" not in features
    assert features["buy_count_imbalance_1m"] == pytest.approx(1 / 3)
    assert features["buy_volume_imbalance_1m"] == pytest.approx(0.4)


def test_secondary_shadow_features_preserve_unknowns_and_explicit_semantics() -> None:
    features = build_gmgn_event_features(
        {
            "dexscr_ad": "null",
            "creator_token_status": "hold",
        },
        current_price=1.0,
        entry_time=1_000,
        age_minutes=2.5,
        holder_count=40,
        marketcap=10_000,
    )
    assert features["dexscr_ad"] is None
    assert "creator_token_status" not in features
    assert features["holder_count/age"] == pytest.approx(16.0)
    assert features["ln(marketcap+1)"] == pytest.approx(math.log1p(10_000))


def test_new_feature_generation_keeps_event_candidates_shadow_only() -> None:
    assert "price_change_1m" in AVAILABLE_MODEL_FEATURES
    assert "volume_acceleration_2m" not in AVAILABLE_MODEL_FEATURES
    assert "holder_count/age" in AVAILABLE_MODEL_FEATURES
    assert "creator_token_status" not in AVAILABLE_MODEL_FEATURES
    assert "ln(swaps_1m+1)" not in AVAILABLE_MODEL_FEATURES
    assert "ln(volume_2m+1)" not in AVAILABLE_MODEL_FEATURES
    assert "ln(liquidity_usd)" not in AVAILABLE_MODEL_FEATURES
    assert len(AVAILABLE_MODEL_FEATURES) == 39
    assert "price_change_1m" not in DEFAULT_MODEL_TRAINING_FEATURES
    assert "volume_acceleration_2m" not in DEFAULT_MODEL_TRAINING_FEATURES
    assert "holder_count/age" not in DEFAULT_MODEL_TRAINING_FEATURES
    assert "creator_token_status" not in DEFAULT_MODEL_TRAINING_FEATURES
    assert "ln(liquidity_usd)" not in DEFAULT_MODEL_TRAINING_FEATURES
    assert len(DEFAULT_MODEL_TRAINING_FEATURES) == 29
    assert FEATURE_SCHEMA_VERSION in TrainingService.FEATURE_SELECTION_STATE_KEY


def test_rpc_endpoint_order_uses_four_alchemy_then_public(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "ALCHEMY_API_KEY_1=a1",
                "ALCHEMY_API_KEY_2=a2",
                "ALCHEMY_API_KEY_3=a3",
                "ALCHEMY_API_KEY_4=a4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    endpoints = configured_rpc_endpoints(env)
    assert [item.provider for item in endpoints] == [
        "alchemy",
        "alchemy",
        "alchemy",
        "alchemy",


        "solana_public",
    ]
    assert all(item.production_grade for item in endpoints[:-1])
    assert endpoints[-1].production_grade is False


def test_rpc_http_error_redaction_never_persists_embedded_credential() -> None:
    credential = "fixture_credential_value"
    endpoint = RpcEndpoint("alchemy", 1, f"https://solana-mainnet.g.alchemy.com/v2/{credential}")
    request = httpx.Request("POST", endpoint.url)
    response = httpx.Response(403, request=request)
    error = httpx.HTTPStatusError("forbidden", request=request, response=response)
    safe = SolanaRpcPool._safe_error(endpoint, error)
    assert safe == "alchemy:1:http_403"
    assert credential not in safe


@pytest.mark.asyncio
async def test_gmgn_regime_optional_failure_is_missing_not_zero_and_uses_official_post_bodies() -> None:
    calls: list[tuple[str, str, object, object]] = []

    class FakeClient:
        endpoints = CollectorEndpoints()

        async def request(self, slot, path, *, method="GET", params=None, json_body=None, timeout_seconds=None):
            calls.append((method, path, params, json_body))
            if path == self.endpoints.trending:
                return {"data": [{"address": "A", "volume_1m": 100, "buys_1m": 8, "sells_1m": 2}]}
            raise CollectorAPIError("optional market feed unavailable", status_code=503)

    provider = GMGNMarketRegimeProvider(FakeClient(), ApiKeyRoles.from_secrets(["fixture-value"]))  # type: ignore[arg-type]
    snapshot = await provider.snapshot(now_ts=10_000)

    assert snapshot["available"] is True
    assert snapshot["trending_count_1m"] == 1
    assert snapshot["hot_search_count_1m"] is None
    assert snapshot["signal_kol_buy_15m"] is None
    assert snapshot["signal_dex_boost_15m"] is None
    hot_calls = [item for item in calls if item[1] == "/v1/market/hot_searches"]
    signal_calls = [item for item in calls if item[1] == "/v1/market/token_signal"]
    assert hot_calls == [("POST", "/v1/market/hot_searches", None, {"params": [{"label": "hot-search", "chain": "sol", "interval": "1m", "limit": 100}]})]
    assert signal_calls == [("POST", "/v1/market/token_signal", None, {"chain": "sol", "groups": [{}]})]


@pytest.mark.asyncio
async def test_dexscreener_is_low_rate_solana_only_fallback() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/ads/latest/v1":
            return httpx.Response(
                200,
                json=[
                    {"chainId": "solana", "tokenAddress": "A", "impressions": 120},
                    {"chainId": "ethereum", "tokenAddress": "B", "impressions": 5000},
                ],
            )
        if request.url.path == "/token-boosts/latest/v1":
            return httpx.Response(
                200,
                json=[
                    {"chainId": "solana", "tokenAddress": "A", "amount": 10},
                    {"chainId": "solana", "tokenAddress": "C", "amount": 5},
                ],
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.dexscreener.com")
    provider = DexScreenerFallbackProvider(client=client)
    snapshot = await provider.snapshot()
    await client.aclose()

    assert sorted(calls) == ["/ads/latest/v1", "/token-boosts/latest/v1"]
    assert snapshot["available"] is True
    assert snapshot["dexscreener_sol_ads_latest_count"] == 1
    assert snapshot["dexscreener_sol_ad_impressions_latest"] == 120
    assert snapshot["dexscreener_sol_boosts_latest_count"] == 2
    assert snapshot["dexscreener_sol_boost_amount_latest"] == 15


@pytest.mark.asyncio
async def test_dexscreener_failure_is_missing_not_observed_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ads/latest/v1":
            return httpx.Response(429, json={"error": "rate"})
        if request.url.path == "/token-boosts/latest/v1":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.dexscreener.com"
    )
    provider = DexScreenerFallbackProvider(client=client)
    snapshot = await provider.snapshot(force=True)
    await client.aclose()

    assert snapshot["available"] is True
    assert snapshot["coverage"] == pytest.approx(0.5)
    assert snapshot["dexscreener_sol_ads_latest_count"] is None
    assert snapshot["dexscreener_sol_boosts_latest_count"] == 0


@pytest.mark.asyncio
async def test_coinbase_public_market_uses_completed_candles_and_cache() -> None:
    observed_at = 7_200
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        base = 100.0 if "BTC-USD" in request.url.path else 10.0
        rows = []
        for opened_at in range(3_000, observed_at + 1, 60):
            close = base * (1.0 + (opened_at - 3_000) / 100_000.0)
            if opened_at == observed_at:
                close = base * 99  # current forming bucket; must be ignored
            rows.append([opened_at, close, close, close, close, 1.0])
        return httpx.Response(200, json=rows)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.exchange.coinbase.com"
    )
    provider = CoinbasePublicMarketProvider(client=client, min_refresh_seconds=300)
    first = await provider.snapshot(observed_at=observed_at, force=True)
    second = await provider.snapshot(observed_at=observed_at + 60)
    await client.aclose()

    assert first.btc_available is True
    assert first.sol_available is True
    assert first.features["btc_return_15m"] is not None
    assert abs(float(first.features["btc_return_15m"])) < 0.10
    assert first.features["coinbase_sol_return_60m"] is not None
    assert len(calls) == 2
    assert second is first


def _insert_hot_regime(database: Database, observed_at: int) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO market_regime_snapshots(
                observed_at,regime_score,regime_label,confidence,features_json,
                source_health_json,reasons_json,policy_version,recorded_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (observed_at, 0.8, "hot", 0.9, "{}", "{}", "[]", "test", "2026-08-16T00:00:00+00:00"),
        )


def test_adaptive_policy_is_shadow_only_until_evidence_gate(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    settings = make_settings(tmp_path)
    _insert_hot_regime(database, 1_000)
    service = AdaptivePolicyService(database, settings)

    shadow = service.decision(now_ts=1_800)
    assert shadow.action == "NEUTRAL"
    assert shadow.delta_logit == 0
    assert shadow.reason.startswith("shadow_only:EXPANSIVE:")
    assert service.effective_threshold(0.20, shadow) == pytest.approx(0.20)

    database.set_runtime_state("adaptive_policy_ready", True)
    applied = service.decision(now_ts=2_700)
    assert applied.action == "EXPANSIVE"
    assert applied.delta_logit < 0
    assert service.effective_threshold(0.20, applied) < 0.20


def test_adaptive_evidence_starts_closed_on_fresh_generation(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    service = AdaptivePolicyService(database, make_settings(tmp_path))
    evidence = service.evidence_status()
    assert evidence.ready is False
    assert evidence.unique_sample_clusters == 0
    assert evidence.reason.startswith("feedback_warmup:")
    assert database.get_runtime_state("adaptive_policy_ready", False) is False


def test_feature_family_audit_reports_insufficient_data_without_backfill(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    report = FeatureFamilyAuditService(database).run()
    assert report["status"] == "INSUFFICIENT_DATA"
    assert report["generation"] == FEATURE_SCHEMA_VERSION
    assert report["readiness"]["mature_samples"] == 0
    assert report["families"]["local_event"]["status"] == "INSUFFICIENT_DATA"
    assert report["families"]["attention_entry"]["status"] == "INSUFFICIENT_DATA"
    assert report["families"]["attention"]["status"] == "INSUFFICIENT_DATA"
    assert len(LEGACY_BASELINE_FEATURES) == 28
    assert "price_change_1m" not in LEGACY_BASELINE_FEATURES
    assert "baseline" not in report


def test_logit_threshold_adjustment_is_monotone_and_bounded() -> None:
    base = 0.20
    defensive = adaptive_threshold(base, 0.35)
    expansive = adaptive_threshold(base, -0.20)
    assert 0 < expansive < base < defensive < 1
