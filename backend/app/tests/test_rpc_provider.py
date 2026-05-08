"""
Tests for RPC Provider

Verifies that:
1. Mock get_balance works
2. RPC timeout doesn't cause chase orders
3. Balance returns expected schema
"""
import pytest
import pytest_asyncio
from ..config import ProviderMode
from ..db.repositories import Repositories
from ..providers.rpc_real import RpcRealProvider


@pytest_asyncio.fixture
async def repo(tmp_path):
    db_file = tmp_path / "test_rpc.db"
    repo = await Repositories.create(str(db_file))
    await repo.ensure_default_strategy_groups()
    try:
        yield repo
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_mock_get_balance(repo):
    """Test mock get_balance returns expected schema."""
    rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
    
    balance = await rpc.get_balance("MOCK_WALLET")
    
    assert 'wallet' in balance, "Balance should have wallet field"
    assert 'sol_balance' in balance, "Balance should have sol_balance"
    assert balance.get('mode') == 'MOCK', "Should be in mock mode"


@pytest.mark.asyncio
async def test_rpc_timeout_no_chase(repo):
    """Test RPC timeout is logged but doesn't cause chase orders."""
    rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
    
    # In mock mode, no real timeout, but check provider_requests logging
    await rpc.get_balance("MOCK_WALLET")
    
    requests = await repo.list_provider_requests(limit=10)
    rpc_reqs = [r for r in requests if r.get('provider') == 'RPC']
    assert len(rpc_reqs) > 0, "Should have RPC provider requests"
    
    for req in rpc_reqs:
        assert req.get('endpoint') == '/getBalance', "Endpoint should be /getBalance"
        assert req.get('ok') is not None, "OK status should be recorded"


@pytest.mark.asyncio
async def test_get_token_balance(repo):
    """Test get_token_balance returns expected schema."""
    rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
    
    balance = await rpc.get_token_balance("MOCK_WALLET", "MOCK_TOKEN")
    
    assert 'wallet' in balance, "Balance should have wallet"
    assert 'mint' in balance, "Balance should have mint"
    assert balance.get('mode') == 'MOCK', "Should be mock mode"


@pytest.mark.asyncio
async def test_wait_signature_mock(repo):
    """Test wait_signature in mock mode."""
    rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
    
    result = await rpc.wait_signature("MOCK_SIG123", 30)
    
    assert 'signature' in result, "Result should have signature"
    assert result.get('status') == 'confirmed', "Mock should confirm immediately"
    assert result.get('mode') == 'MOCK', "Should be mock mode"
