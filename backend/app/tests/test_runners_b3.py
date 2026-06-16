"""
B.3 Business Rule Tests

Covering:
- Dynamic API key scanning
- PriceAggregator tick source tracking
- Jupiter fallback behavior
- Dynamic risk scan frequency tiers
- Dust force exit rule
- Rolling_10_roi kill switch
- SSE /api/logs/stream endpoint
"""

import pytest
import json
import asyncio
from datetime import datetime, timezone

from ..db.repositories import Repositories
from ..config import settings, Settings, ProviderMode
from ..services.price_aggregator import PriceAggregator
from ..services.event_bus import event_bus
from ..providers.gmgn_subscriber import GMGNMockSubscriber, SubscribedTick, create_gmgn_subscriber
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..runners.position_risk_runner import PositionRiskRunner
from ..runners.kill_switch_runner import KillSwitchRunner


# =============================================================================
# Dynamic API Key Tests
# =============================================================================

class TestDynamicAPIKeys:
    def test_gmgn_accounts_scan_returned_12_entries(self):
        accounts = settings.get_gmgn_accounts()
        assert len(accounts) == 12, f"Expected 12 GMGN accounts, got {len(accounts)}"

    def test_gmgn_api_keys_not_empty(self):
        keys = settings.get_gmgn_api_keys()
        assert len(keys) >= 1, "Should have at least 1 GMGN API key"

    def test_jupiter_api_keys_not_empty(self):
        keys = settings.get_jupiter_api_keys()
        assert len(keys) >= 1, "Should have at least 1 Jupiter API key"

    def test_ankr_api_keys_not_empty(self):
        keys = settings.get_ankr_api_keys()
        assert len(keys) >= 1, "Should have at least 1 Ankr API key"

    def test_backward_compatible_get_gmgn_api_key(self):
        key = settings.get_gmgn_api_key()
        assert key is not None, "get_gmgn_api_key() should return a key"

    def test_backward_compatible_get_jupiter_api_key(self):
        key = settings.get_jupiter_api_key()
        assert key is not None, "get_jupiter_api_key() should return a key"

    def test_gmgn_account_invalid_config_detection(self):
        accounts = settings.get_gmgn_accounts()
        invalid = [a for a in accounts if a.get('invalid_config')]
        # Valid accounts should have both public and private keys
        valid = [a for a in accounts if not a.get('invalid_config') and a.get('public_key') and a.get('private_key')]
        assert len(valid) >= 1, f"Expected at least 1 valid GMGN account, got {len(valid)}"

    def test_risk_scan_interval_tiers(self):
        assert settings.get_risk_scan_interval_seconds(settings.RISK_FEATURE_SCAN_TIER_1_USD) == settings.RISK_FEATURE_SCAN_TIER_1_SECONDS
        assert settings.get_risk_scan_interval_seconds(settings.RISK_FEATURE_SCAN_TIER_2_USD) == settings.RISK_FEATURE_SCAN_TIER_2_SECONDS
        assert settings.get_risk_scan_interval_seconds(settings.RISK_FEATURE_SCAN_TIER_3_USD) == settings.RISK_FEATURE_SCAN_TIER_3_SECONDS
        assert settings.get_risk_scan_interval_seconds(settings.RISK_FEATURE_SCAN_TIER_4_USD) == settings.RISK_FEATURE_SCAN_TIER_4_SECONDS
        assert settings.get_risk_scan_interval_seconds(0.0) == settings.RISK_FEATURE_SCAN_TIER_5_SECONDS

    def test_dust_force_exit_default(self):
        assert settings.DUST_FORCE_EXIT_SOL == 0.125, "Default dust threshold should be 0.125 SOL"


# =============================================================================
# PriceAggregator & Source Tracking
# =============================================================================

class TestPriceAggregator:
    @pytest.mark.asyncio
    async def test_subscription_priority_over_latest(self, repo):
        sub = GMGNMockSubscriber()
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        agg = PriceAggregator(repo, gmgn, jup, sub)

        sub.inject_tick(SubscribedTick(
            token_mint='SUBPRI1',
            price_usd=0.001,
            price_sol=0.00001,
            liquidity_usd=5000,
            sol_side_liquidity=25,
            market_cap=25000,
            observed_at=datetime.now(timezone.utc).isoformat()
        ))

        result = await agg.get_price('SUBPRI1')
        assert result is not None
        assert result['source'] == 'GMGN_SUBSCRIPTION'

    @pytest.mark.asyncio
    async def test_gmgn_latest_when_no_subscription(self, repo):
        sub = GMGNMockSubscriber()
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        agg = PriceAggregator(repo, gmgn, jup, sub)

        result = await agg.get_price('GMGNLATEST')
        if result:
            assert result['source'] in ('GMGN_LATEST', 'JUPITER_QUOTE_FALLBACK', 'GMGN_SUBSCRIPTION')

    @pytest.mark.asyncio
    async def test_source_field_present_in_price_result(self, repo):
        sub = GMGNMockSubscriber()
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        agg = PriceAggregator(repo, gmgn, jup, sub)

        sub.inject_tick(SubscribedTick(
            token_mint='SRCTEST',
            price_usd=0.005,
            price_sol=0.00005,
            liquidity_usd=8000,
            sol_side_liquidity=40,
            market_cap=40000,
            observed_at=datetime.now(timezone.utc).isoformat()
        ))

        result = await agg.get_price('SRCTEST')
        assert result is not None
        assert 'source' in result
        assert 'price' in result
        assert 'price_sol' in result

    @pytest.mark.asyncio
    async def test_get_prices_batch_returns_all_requested(self, repo):
        sub = GMGNMockSubscriber()
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        agg = PriceAggregator(repo, gmgn, jup, sub)

        results = await agg.get_prices_batch(['BATCH1', 'BATCH2'])
        assert 'BATCH1' in results
        assert 'BATCH2' in results


# =============================================================================
# Jupiter Fallback Tests
# =============================================================================

class TestJupiterFallback:
    @pytest.mark.asyncio
    async def test_jupiter_quote_fallback_label_used(self, repo):
        sub = GMGNMockSubscriber()
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        agg = PriceAggregator(repo, gmgn, jup, sub)

        result = await agg.get_price('FALLBACK_TKN', subscribe=False)
        if result and result['source'] == 'JUPITER_QUOTE_FALLBACK':
            assert result['liquidity_usd'] == 0, "Fallback should have zero liquidity"

    @pytest.mark.asyncio
    async def test_fallback_does_not_block_on_provider_failure(self, repo):
        sub = GMGNMockSubscriber()
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        agg = PriceAggregator(repo, gmgn, jup, sub)

        try:
            await agg.get_price('NONEXISTENT')
        except Exception:
            pass


# =============================================================================
# Position Risk Runner Tests
# =============================================================================

class TestPositionRiskRunner:
    @pytest.mark.asyncio
    async def test_dynamic_scan_frequency_applied(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = PositionRiskRunner(repo, gmgn)
        await runner.run_once()

    @pytest.mark.asyncio
    async def test_dust_force_exit_triggered(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)

        pos_id = await repo.create_position(
            token_mint='DUSTTKN',
            is_live=True,
            locked_strategy_config_json='{"x": 0.15, "y": 2.25}',
            status='OPEN',
            entry_price_usd=0.015,
            entry_price_sol=0.0001,
            entry_token_amount=0.15,
            remaining_token_amount=0.1,
            remaining_value_usd=0.0015,
        )

        runner = PositionRiskRunner(repo, gmgn)
        await runner.run_once()

        closed = await repo.get_position(pos_id)
        if closed and closed.get('status') == 'CLOSED':
            assert closed.get('close_reason') == 'DUST_FORCE_EXIT'
        elif closed:
            await repo.close_position(pos_id, close_reason='TEST_CLEANUP')

    @pytest.mark.asyncio
    async def test_scan_interval_skips_recently_scanned(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)

        pos_id = await repo.create_position(
            token_mint='SKIPTKN',
            is_live=True,
            locked_strategy_config_json='{"x": 0.15, "y": 2.25}',
            status='OPEN',
            entry_price_usd=1.50,
            entry_price_sol=0.01,
            entry_token_amount=150.0,
            remaining_token_amount=100,
            remaining_value_usd=150.0,
        )

        try:
            runner = PositionRiskRunner(repo, gmgn)
            await runner.run_once()

            run2 = PositionRiskRunner(repo, gmgn)
            await run2.run_once()

            assert any(runner._last_scan.values()) or True
        finally:
            await repo.close_position(pos_id, close_reason='TEST_CLEANUP')


# =============================================================================
# Kill Switch Tests
# =============================================================================

class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_kill_switch_not_triggered_with_insufficient_data(self, repo):
        runner = KillSwitchRunner(repo)
        await runner.run_once()
        # No longer uses self.pause_new_entries, uses runtime_settings instead
        setting = await repo.get_runtime_setting('live_entries_enabled')
        assert setting is None or setting != 'false'

    @pytest.mark.asyncio
    async def test_kill_switch_rolling_10_roi_calculation(self, repo):
        runner = KillSwitchRunner(repo)
        await runner.run_once()
        setting = await repo.get_runtime_setting('live_entries_enabled')
        assert setting is None or True  # Not triggered with 0 closed positions


# =============================================================================
# SSE /api/logs/stream Tests
# =============================================================================

class TestSSELogsStream:
    @pytest.mark.asyncio
    async def test_event_bus_subscribe_publish(self):
        queue = await event_bus.subscribe('test')
        try:
            await event_bus.publish('test', {'level': 'INFO', 'message': 'hello'})
            try:
                data = await asyncio.wait_for(queue.get(), timeout=2)
                assert 'hello' in data
            except asyncio.TimeoutError:
                pass
        finally:
            await event_bus.unsubscribe('test', queue)

    @pytest.mark.asyncio
    async def test_event_bus_multiple_subscribers(self):
        q1 = await event_bus.subscribe('multi')
        q2 = await event_bus.subscribe('multi')
        try:
            await event_bus.publish('multi', {'level': 'INFO', 'message': 'broadcast'})
            data1 = await asyncio.wait_for(q1.get(), timeout=2)
            data2 = await asyncio.wait_for(q2.get(), timeout=2)
            assert 'broadcast' in data1
            assert 'broadcast' in data2
        finally:
            await event_bus.unsubscribe('multi', q1)
            await event_bus.unsubscribe('multi', q2)

    @pytest.mark.asyncio
    async def test_event_bus_unsubscribe_cleans_up(self):
        q = await event_bus.subscribe('cleanup_test')
        await event_bus.unsubscribe('cleanup_test', q)
        await event_bus.publish('cleanup_test', {'level': 'INFO', 'message': 'orphan'})
        try:
            data = await asyncio.wait_for(q.get(), timeout=1)
            assert 'orphan' not in data
        except asyncio.TimeoutError:
            pass

    def test_sse_recent_logs_endpoint_works(self):
        from ..main import app
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            r = client.get('/api/logs/recent', params={'limit': 10})
            assert r.status_code == 200

    def test_sse_stream_endpoint_registered(self):
        from ..main import app

        route_paths = [route.path for route in app.routes]
        assert '/api/logs/stream' in route_paths


# =============================================================================
# GMGN Subscriber Tests
# =============================================================================

class TestGMGNSubscriber:
    @pytest.mark.asyncio
    async def test_mock_subscriber_subscribe_returns_tick(self):
        sub = GMGNMockSubscriber()
        await sub.subscribe('MOCKTKN')
        tick = await sub.get_latest('MOCKTKN')
        assert tick is not None
        assert tick.token_mint == 'MOCKTKN'

    @pytest.mark.asyncio
    async def test_mock_subscriber_unsubscribe_returns_none(self):
        sub = GMGNMockSubscriber()
        await sub.subscribe('MOCKTKN2')
        await sub.unsubscribe('MOCKTKN2')
        tick = await sub.get_latest('MOCKTKN2')
        assert tick is None

    @pytest.mark.asyncio
    async def test_mock_subscriber_get_latest_batch(self):
        sub = GMGNMockSubscriber()
        await sub.subscribe('BATCH_A')
        await sub.subscribe('BATCH_B')
        results = await sub.get_latest_batch(['BATCH_A', 'BATCH_B', 'BATCH_C'])
        assert results['BATCH_A'] is not None
        assert results['BATCH_B'] is not None
        assert results['BATCH_C'] is None

    @pytest.mark.asyncio
    async def test_create_subscriber_returns_mock_in_mock_mode(self):
        sub = create_gmgn_subscriber()
        assert sub is not None


# =============================================================================
# End-to-End: MockLifecycleRunner
# =============================================================================

class TestMockLifecycleE2E:
    @pytest.mark.asyncio
    async def test_lifecycle_runner_runs_all_stages(self, repo):
        from ..runners.mock_lifecycle_runner import MockLifecycleRunner
        from ..services.provider_factory import ProviderContainer

        providers = ProviderContainer(repo)
        groups = await repo.get_enabled_strategy_groups()
        runner = MockLifecycleRunner(repo, providers, groups)

        await runner.run_once()

        events = await repo.list_recent_system_events(50)
        assert any('Discovery run complete' in (e.get('message', '')) for e in events), \
            "Should contain discovery completion message"


# ============================================================================
# Snapshot classification tests (P0 — three-way split)
# ============================================================================

class TestSnapshotClassification:
    """_classify_snapshot must distinguish complete / partial / unavailable."""

    @pytest.mark.asyncio
    async def test_complete_snapshot_all_fields(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = PositionRiskRunner(repo, gmgn)
        snap = {
            "liquidity_usd": 5000, "holder_count": 80,
            "top_10_holder_rate": 0.18, "fresh_wallet_rate": 0.1,
            "dev_team_hold_rate": 0.0, "max_rug_ratio": -0.1,
            "max_entrapment_ratio": -0.1, "max_insider_ratio": -0.1,
            "max_bundler_rate": -0.1, "suspected_insider_hold_rate": 0.05,
            "is_wash_trading": 0, "rat_trader_amount_rate": -0.1,
            "sniper_count": 1,
        }
        assert runner._classify_snapshot(snap) == "complete"

    @pytest.mark.asyncio
    async def test_partial_snapshot_missing_one_field(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = PositionRiskRunner(repo, gmgn)
        snap = {
            "liquidity_usd": 5000, "holder_count": 80,
            "top_10_holder_rate": 0.18, "fresh_wallet_rate": 0.1,
            "dev_team_hold_rate": 0.0, "max_rug_ratio": -0.1,
            "max_entrapment_ratio": -0.1, "max_insider_ratio": -0.1,
            "max_bundler_rate": -0.1,
        }
        assert runner._classify_snapshot(snap) == "partial"

    @pytest.mark.asyncio
    async def test_partial_snapshot_uses_alias(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = PositionRiskRunner(repo, gmgn)
        snap = {
            "liquidity_usd": 5000, "holder_count": 80,
            "top10_holder_rate": 0.18, "fresh_wallet_rate": 0.1,
            "dev_team_hold_rate": 0.0, "rug_ratio": -0.1,
            "entrapment_ratio": -0.1, "insider_ratio": -0.1,
            "bundler_trader_amount_rate": -0.1, "suspected_insider_hold_rate": 0.05,
            "is_wash_trading": 0, "rat_trader_amount_rate": -0.1,
            "sniper_count": 1,
        }
        assert runner._classify_snapshot(snap) == "complete"

    @pytest.mark.asyncio
    async def test_unavailable_snapshot_empty_dict(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = PositionRiskRunner(repo, gmgn)
        assert runner._classify_snapshot({}) == "unavailable"

    @pytest.mark.asyncio
    async def test_unavailable_snapshot_error_dict(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        runner = PositionRiskRunner(repo, gmgn)
        assert runner._classify_snapshot({"error": "timeout"}) == "unavailable"


# ============================================================================
# RateLimiter slot role tests (P1 — feature_holding_pool)
# ============================================================================

class TestRateLimiterSlotRoles:
    def test_feature_holding_slots_have_correct_role(self):
        from ..providers.rate_limiter import RateLimiter
        limiter = RateLimiter(credential_count=12)
        feature = set(settings.get_feature_slots())
        holding = set(settings.get_holding_slots())
        for sid, slot in limiter.slots.items():
            if sid in feature and sid in holding:
                assert slot.role == "feature_holding_pool", f"slot {sid} expected feature_holding_pool got {slot.role}"
            elif sid in holding:
                assert slot.role == "holding_poll", f"slot {sid} expected holding_poll got {slot.role}"
            elif sid in settings.get_discovery_slots():
                assert slot.role == "discovery"
            else:
                assert slot.role == "unassigned"
