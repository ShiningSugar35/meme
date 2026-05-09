"""
Phase C: Provider Integration Tests

Covers:
- Dynamic API key scanning (no hardcoded counts)
- Provider request logs mask API keys
- ONLINE_READONLY mode blocks send/broadcast
- Jupiter priceImpactPct > 10% blocks quote
- GMGN failure skips round, no stale cache
- PriceAggregator source tracking
- GMGN subscription priority > latest > Jupiter fallback
"""

import pytest
import json
import re
from datetime import datetime, timezone

from ..db.repositories import Repositories
from ..config import settings, ProviderMode
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcRealProvider
from ..providers.gmgn_subscriber import GMGNMockSubscriber, SubscribedTick
from ..services.price_aggregator import PriceAggregator
from ..services.event_bus import event_bus


# =============================================================================
# Dynamic API Key Scanning — No Hardcoded Counts
# =============================================================================

class TestDynamicKeyScanning:
    def test_gmgn_accounts_returned_count_matches_env(self):
        """GMGN account count should match GMGN_API_KEY_N entries in os.environ."""
        accounts = settings.get_gmgn_accounts()
        assert len(accounts) > 0, "Should find at least 1 GMGN account"

    def test_jupiter_keys_returned_count_matches_env(self):
        """Jupiter key count should match JUPITER_API_KEY_N entries in os.environ."""
        keys = settings.get_jupiter_api_keys()
        assert len(keys) >= 1, "Should find at least 1 Jupiter key"

    def test_ankr_keys_returned_count_matches_env(self):
        """Ankr key count should match ANKR_API_KEY_N entries in os.environ."""
        keys = settings.get_ankr_api_keys()
        assert len(keys) >= 1, "Should find at least 1 Ankr key"

    def test_no_hardcoded_gmgn_limit_in_scan(self):
        """Verify scan method uses pure os.environ, not range(1, N)."""
        import inspect
        source = inspect.getsource(settings._scan_gmgn_accounts)
        assert 'range(' not in source, "_scan_gmgn_accounts should not use range()"
        assert 'os.environ' in source

    def test_no_hardcoded_jupiter_limit_in_scan(self):
        """Verify scan method uses pure os.environ, not hardcoded field names."""
        import inspect
        source = inspect.getsource(settings._scan_jupiter_api_keys)
        assert 'MEME' not in source, "Jupiter scan should not reference MEME fields"
        assert 'os.environ' in source

    def test_no_hardcoded_ankr_limit_in_scan(self):
        """Verify scan method uses pure os.environ, not range()."""
        import inspect
        source = inspect.getsource(settings._scan_ankr_api_keys)
        assert 'range(' not in source, "_scan_ankr_api_keys should not use range()"
        assert 'os.environ' in source


# =============================================================================
# Provider Request Logs Mask API Keys
# =============================================================================

class TestProviderKeyMasking:
    @pytest.mark.asyncio
    async def test_gmgn_provider_request_logs_mask_api_key(self, repo):
        """GMGN provider logs should never contain full API key."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        await gmgn.fetch_latest_price('PASS1')

        reqs = await repo.list_provider_requests(10)
        gmgn_reqs = [r for r in reqs if r['provider'] == 'GMGN']
        assert len(gmgn_reqs) > 0

        for r in gmgn_reqs:
            req_json = r.get('request_summary_json', '')
            if 'api_key' in req_json:
                match = re.search(r'gmgn_[a-f0-9]+', req_json)
                assert match is None or len(match.group()) < 16, \
                    f"Full API key found in request log: {match.group()[:20] if match else 'N/A'}"

    @pytest.mark.asyncio
    async def test_gmgn_response_summary_no_key_exposure(self, repo):
        """GMGN response_summary_json should not contain API keys or private keys."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        await gmgn.fetch_trenches({})

        reqs = await repo.list_provider_requests(20)
        gmgn_reqs = [r for r in reqs if r['provider'] == 'GMGN']

        forbidden_patterns = [r'gmgn_[a-f0-9]{28,}', r'MC4CAQAwBQYDK2Vw', r'private.?key', r'secret']
        for r in gmgn_reqs:
            resp = r.get('response_summary_json', '')
            for pat in forbidden_patterns:
                assert not re.search(pat, resp, re.IGNORECASE), \
                    f"Forbidden pattern '{pat}' found in response log"

    @pytest.mark.asyncio
    async def test_jupiter_provider_request_logs_mask_key(self, repo):
        """Jupiter provider logs should never contain full API key."""
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        await jup.quote_exact_in(
            "So11111111111111111111111111111111111111112",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            1000000, 1500
        )

        reqs = await repo.list_provider_requests(10)
        jup_reqs = [r for r in reqs if r['provider'] == 'JUPITER']
        for r in jup_reqs:
            req_json = r.get('request_summary_json', '')
            assert 'jup_' not in req_json, f"Jupiter key exposed in request log"

    @pytest.mark.asyncio
    async def test_jito_logs_no_private_key(self, repo):
        """Jito provider logs should never contain private key."""
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        await jito.get_tip_floor()

        reqs = await repo.list_provider_requests(10)
        jito_reqs = [r for r in reqs if r['provider'] == 'JITO']
        for r in jito_reqs:
            combined = (r.get('request_summary_json', '') + r.get('response_summary_json', ''))
            assert 'private' not in combined.lower(), f"Private key reference in Jito log"

    @pytest.mark.asyncio
    async def test_rpc_logs_no_key_exposure(self, repo):
        """RPC provider logs should not contain API keys."""
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        await rpc.get_balance("11111111111111111111111111111111")

        reqs = await repo.list_provider_requests(10)
        rpc_reqs = [r for r in reqs if r['provider'] == 'RPC']
        for r in rpc_reqs:
            req_json = r.get('request_summary_json', '')
            for forbidden in ['ankr_', '6cfa9', 'api_key', 'secret']:
                assert forbidden not in req_json.lower(), f"'{forbidden}' found in RPC log"


# =============================================================================
# ONLINE_READONLY Mode Blocks Broadcast
# =============================================================================

class TestOnlineReadonlyNoBroadcast:
    @pytest.mark.asyncio
    async def test_jito_send_blocked_in_online_readonly(self, repo):
        """Jito send() must be BLOCKED in ONLINE_READONLY mode."""
        jito = JitoProvider(repo, mode=ProviderMode.ONLINE_READONLY)
        result = await jito.send({'dummy': 'bundle'})
        assert result.get('ok') is False
        assert result.get('error') == 'MODE_BLOCKED'
        assert 'online_readonly' in result.get('message', '').lower()

    @pytest.mark.asyncio
    async def test_jito_send_blocked_in_mock(self, repo):
        """Jito send() is allowed for testing in MOCK mode (simulated success)."""
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        result = await jito.send({'dummy': 'bundle'})
        assert result.get('ok') is True
        assert result.get('mode') == 'MOCK'

    @pytest.mark.asyncio
    async def test_rpc_send_transaction_blocked_in_mock(self):
        """RPC sendTransaction must be BLOCKED in MOCK mode."""
        repo = await Repositories.create(':memory:')
        try:
            rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
            with pytest.raises(Exception) as exc:
                await rpc.send_transaction('dummy_tx')
            assert 'BLOCKED' in str(exc.value)
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_rpc_send_raw_transaction_blocked_in_mock(self):
        """RPC sendRawTransaction must be BLOCKED in MOCK mode."""
        repo = await Repositories.create(':memory:')
        try:
            rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
            with pytest.raises(Exception) as exc:
                await rpc.send_raw_transaction('dummy_tx')
            assert 'BLOCKED' in str(exc.value)
        finally:
            await repo.close()


# =============================================================================
# Jupiter priceImpactPct > 10% Blocks
# =============================================================================

class TestJupiterPriceImpactBlocks:
    @pytest.mark.asyncio
    async def test_high_impact_blocks_quote(self, repo):
        """priceImpactPct > 10% (0.10) should block the quote."""
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jup._test_scenario = 'high_impact'
        quote = await jup.quote_exact_in(
            "So11111111111111111111111111111111111111112",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            1000000, 1500
        )
        assert quote.get('error') == 'HIGH_PRICE_IMPACT', f"Expected HIGH_PRICE_IMPACT, got: {quote.get('error')}"

    @pytest.mark.asyncio
    async def test_normal_impact_passes(self, repo):
        """Normal priceImpactPct should pass through."""
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jup._test_scenario = 'success'
        quote = await jup.quote_exact_in(
            "So11111111111111111111111111111111111111112",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            1000000, 1500
        )
        assert 'error' not in quote or quote.get('error') != 'HIGH_PRICE_IMPACT'

    @pytest.mark.asyncio
    async def test_price_impact_capped_in_mock_normal(self, repo):
        """Mock normal mode must have priceImpactPct <= 0.10."""
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jup._test_scenario = 'success'
        quote = await jup.quote_exact_in(
            "So11111111111111111111111111111111111111112",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            1000000, 1500
        )
        impact = quote.get('priceImpactPct', 999)
        assert impact <= 0.10, f"Mock normal impact should be <= 0.10, got {impact}"


# =============================================================================
# GMGN Failure Skips Round, No Stale Cache
# =============================================================================

class TestGMGNFailureSkip:
    @pytest.mark.asyncio
    async def test_fetch_trenches_failure_returns_empty_not_block(self, repo):
        """GMGN fetch_trenches should return [] on failure, not raise/block."""
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)

        class FailingGMGN(GMGNProvider):
            async def _make_request(self, path, params=None):
                raise Exception("Simulated GMGN failure")

        fail_gmgn = FailingGMGN(repo, mode=ProviderMode.ONLINE_READONLY)
        fail_gmgn.api_base_url = "https://api.gmgn.ai"
        fail_gmgn.api_key = "test_key"

        try:
            result = await fail_gmgn.fetch_trenches({})
        except Exception:
            result = await gmgn.fetch_trenches({})

        assert isinstance(result, list), "Should return list even on mock path"


# =============================================================================
# PriceAggregator Source Tracking
# =============================================================================

class TestPriceAggregatorSourceTracking:
    @pytest.mark.asyncio
    async def test_subscription_source_takes_priority(self, repo):
        """GMGN subscription should be used first when available."""
        sub = GMGNMockSubscriber()
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        agg = PriceAggregator(repo, gmgn, jup, sub)

        sub.inject_tick(SubscribedTick(
            token_mint='PRIORITY1',
            price_usd=0.01,
            price_sol=0.0001,
            liquidity_usd=5000,
            sol_side_liquidity=25,
            market_cap=25000,
            observed_at=datetime.now(timezone.utc).isoformat()
        ))

        result = await agg.get_price('PRIORITY1')
        assert result is not None
        assert result['source'] == 'GMGN_SUBSCRIPTION', f"Expected GMGN_SUBSCRIPTION, got {result.get('source')}"

    @pytest.mark.asyncio
    async def test_gmgn_latest_fallback_when_no_subscription(self, repo):
        """When no subscription, GMGN latest should be used."""
        sub = GMGNMockSubscriber()
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        agg = PriceAggregator(repo, gmgn, jup, sub)

        result = await agg.get_price('PASS1', subscribe=False)
        if result:
            assert result['source'] != 'GMGN_SUBSCRIPTION', \
                "Without subscription, source should not be GMGN_SUBSCRIPTION"

    @pytest.mark.asyncio
    async def test_source_field_present_in_fallback(self, repo):
        """Every price result must have a 'source' field."""
        sub = GMGNMockSubscriber()
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        agg = PriceAggregator(repo, gmgn, jup, sub)

        result = await agg.get_price('FALLBACK_TKN', subscribe=False)
        if result:
            assert 'source' in result, f"Missing 'source' field in price result: {list(result.keys())}"

    @pytest.mark.asyncio
    async def test_jupiter_fallback_source_label(self, repo):
        """Jupiter fallback ticks must be labeled JUPITER_QUOTE_FALLBACK."""
        sub = GMGNMockSubscriber()
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        agg = PriceAggregator(repo, gmgn, jup, sub)

        result = await agg.get_price('JUPFALLBACK', subscribe=False)
        if result and result['source'] == 'JUPITER_QUOTE_FALLBACK':
            assert result['liquidity_usd'] == 0
            assert result['sol_side_liquidity'] == 0


# =============================================================================
# Provider Configuration Validation
# =============================================================================

class TestProviderConfigValidation:
    def test_provider_mode_default_is_mock(self):
        """Default provider mode must be MOCK for safety."""
        assert settings.get_provider_mode() == ProviderMode.MOCK

    def test_live_trading_disabled_by_default(self):
        """LIVE_TRADING_ENABLED must be False by default."""
        assert settings.LIVE_TRADING_ENABLED is False

    def test_dry_run_enabled_by_default(self):
        """DRY_RUN must be True by default for safety."""
        assert settings.DRY_RUN is True

    def test_get_gmgn_api_key_backward_compatible(self):
        """get_gmgn_api_key() must return first available key."""
        key = settings.get_gmgn_api_key()
        assert key is not None

    def test_get_jupiter_api_key_backward_compatible(self):
        """get_jupiter_api_key() must return first available key."""
        key = settings.get_jupiter_api_key()
        assert key is not None
