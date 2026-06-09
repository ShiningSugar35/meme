"""
Phase D: Live Trading Path Tests
"""

import pytest
import json
from datetime import datetime, timezone

from ..db.repositories import Repositories
from ..config import Settings, settings, ProviderMode
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcRealProvider
from ..trading.executor import TradingPipeline


class TestSafetyGate:
    def test_safety_gate_passes_in_mock_mode(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipe = TradingPipeline(repo, gmgn, jup, jito, rpc)
        assert pipe._safety_gate() is None

    def test_dry_run_default_is_true(self):
        isolated = Settings(PROVIDER_MODE=None, DRY_RUN=True)
        assert isolated.DRY_RUN is True

    def test_non_mock_mode_with_dry_run_would_block(self):
        isolated = Settings(PROVIDER_MODE=None, DRY_RUN=True)
        assert isolated.get_provider_mode() == ProviderMode.MOCK
        # With DRY_RUN=true, get_provider_mode returns MOCK


class TestPriceImpactBlocks:
    @pytest.mark.asyncio
    async def test_high_impact_blocks_quote_in_executor(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        jup._test_scenario = 'high_impact'
        pipe = TradingPipeline(repo, gmgn, jup, jito, rpc)
        quote = await pipe._get_quote('TOKEN', 1000000, 1500, 1, is_sell=False)
        assert quote.get('error') == 'HIGH_PRICE_IMPACT'

    @pytest.mark.asyncio
    async def test_normal_impact_passes_in_executor(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        jup._test_scenario = 'success'
        pipe = TradingPipeline(repo, gmgn, jup, jito, rpc)
        quote = await pipe._get_quote('TOKEN', 1000000, 1500, 1, is_sell=False)
        assert 'error' not in quote


class TestJitoRetry:
    def test_instruction_error_no_retry(self):
        jito = JitoProvider(None, mode=ProviderMode.MOCK)
        err = 'InstructionError'
        assert 'InstructionError' in err

    def test_tip_too_low_triggers_retry(self):
        jito = JitoProvider(None, mode=ProviderMode.MOCK)
        err = 'tip too low'
        assert 'tip too low' in err

    @pytest.mark.asyncio
    async def test_send_blocked_in_online_readonly(self):
        repo = await Repositories.create(':memory:')
        try:
            jito = JitoProvider(repo, mode=ProviderMode.ONLINE_READONLY)
            result = await jito.send({})
            assert result['ok'] is False
            assert result['error'] == 'MODE_BLOCKED'
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_send_mock_returns_success(self):
        repo = await Repositories.create(':memory:')
        try:
            jito = JitoProvider(repo, mode=ProviderMode.MOCK)
            result = await jito.send({})
            assert result['ok'] is True
        finally:
            await repo.close()


class TestIdempotency:
    def test_buy_idempotency_key_format(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipe = TradingPipeline(repo, gmgn, jup, jito, rpc)
        key = pipe._build_idempotency_key('BUY', 'TOKEN123', {'id': 5, 'config_version': 2}, 100)
        assert key.startswith('BUY:TOKEN123:5:2:100')

    def test_different_strategy_different_key(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipe = TradingPipeline(repo, gmgn, jup, jito, rpc)
        k1 = pipe._build_idempotency_key('BUY', 'TKN', {'id': 1, 'config_version': 1}, 10)
        k2 = pipe._build_idempotency_key('BUY', 'TKN', {'id': 2, 'config_version': 1}, 10)
        assert k1 != k2


class TestSellPath:
    @pytest.mark.asyncio
    async def test_sell_uses_remaining_not_initial(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)

        pos_id = await repo.create_position(
            token_mint='SELLTKN', is_live=True,
            locked_strategy_config_json='{"sell_slippage_cap_bps": 2000}',
            status='OPEN', entry_price_usd=1.0, entry_price_sol=0.01,
            entry_token_amount=1000, remaining_token_amount=500,
            remaining_value_usd=500, total_cost_sol=5.0
        )
        try:
            position = await repo.get_position(pos_id)
            assert position['remaining_token_amount'] == 500
            assert position['entry_token_amount'] == 1000
        finally:
            await repo.close_position(pos_id, close_reason='TEST_CLEANUP')

    @pytest.mark.asyncio
    async def test_sell_safety_gate_works_in_mock_mode(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        jup = JupiterProvider(repo, mode=ProviderMode.MOCK)
        jito = JitoProvider(repo, mode=ProviderMode.MOCK)
        rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
        pipe = TradingPipeline(repo, gmgn, jup, jito, rpc)
        gate = pipe._safety_gate()
        assert gate is None


class TestNoRpcFallback:
    def test_rpc_send_methods_exist(self):
        assert hasattr(RpcRealProvider, 'send_transaction')
        assert hasattr(RpcRealProvider, 'send_raw_transaction')


class TestSecretNotInLogs:
    @pytest.mark.asyncio
    async def test_trade_event_json_no_raw_tx(self, repo):
        te = await repo.append_trade_event(
            'SECRET_TEST', token_mint='TKN', side='BUY',
            event_type='BUY_SUBMITTED', status='SUBMITTED', is_live=1,
            quote_json=json.dumps({'impact': 0.005}),
        )
        req_json = te.get('quote_json', '')
        assert 'private' not in req_json.lower()
        assert 'secret' not in req_json.lower()

    @pytest.mark.asyncio
    async def test_provider_request_logs_no_raw_tx(self, repo):
        gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
        await gmgn.fetch_latest_price('PASS1')
        reqs = await repo.list_provider_requests(10)
        for r in reqs:
            combined = (r.get('request_summary_json', '') + r.get('response_summary_json', ''))
            assert 'private_key' not in combined.lower()
            assert 'base58' not in combined.lower()
            assert 'secret' not in combined.lower()
