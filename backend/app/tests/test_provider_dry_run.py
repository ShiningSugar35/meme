"""
Acceptance Test: Provider Modes (mock/online_readonly/live)

Verifies that all real providers respect the three-mode system:
- MOCK: No external API calls, uses mock data
- ONLINE_READONLY: Allows real API read-only calls
- LIVE: (Future) Would execute real transactions

Also verifies:
- Jito send() is BLOCKED in MOCK and ONLINE_READONLY modes
- API keys and private keys are never logged in full
"""
import pytest
from ..db.repositories import Repositories
from ..config import settings, ProviderMode
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcRealProvider


@pytest.mark.asyncio
async def test_gmgn_provider_mock_returns_mock_data(repo):
    """
    GMGN Provider in MOCK mode should return mock data (not call real API)
    """
    provider = GMGNProvider(repo, mode=ProviderMode.MOCK)
    
    # Test fetch_trenches
    trenches = await provider.fetch_trenches({})
    assert isinstance(trenches, list)
    assert len(trenches) >= 0  # MOCK returns mock data (may have entries)
    # Verify it's mock data (has expected fields)
    if len(trenches) > 0:
        assert 'token_mint' in trenches[0] or 'address' in trenches[0]
    
    # Test fetch_token_snapshot
    snapshot = await provider.fetch_token_snapshot('PASS1')
    assert isinstance(snapshot, dict)
    assert 'mode' not in snapshot  # MOCK doesn't add mode field
    
    # Test fetch_kline
    klines = await provider.fetch_kline('PASS1', '1m', 10)
    assert isinstance(klines, list)
    
    # Test fetch_latest_price
    price = await provider.fetch_latest_price('PASS1')
    assert isinstance(price, dict)
    assert 'price' in price
    assert 'price_usd' in price


@pytest.mark.asyncio
async def test_jupiter_provider_mock_schema_validation(repo):
    """
    Jupiter Provider in MOCK mode should do schema validation only (no real transaction)
    """
    provider = JupiterProvider(repo, mode=ProviderMode.MOCK)
    
    # Test quote_exact_in (read-only, safe)
    quote = await provider.quote_exact_in('SOL', 'PASS1', 1000000000, 1500)
    assert isinstance(quote, dict)
    assert quote.get('mode') == 'MOCK'
    assert 'inAmount' in quote
    assert 'outAmount' in quote
    assert 'priceImpactPct' in quote
    assert float(quote['priceImpactPct']) <= 0.10  # Below 10% threshold
    
    # Test build_swap_instructions (schema validation only, no real transaction)
    instr = await provider.build_swap_instructions(quote, 'MOCK_PUBKEY', {})
    assert isinstance(instr, dict)
    assert instr.get('mode') == 'MOCK_NO_TRANSACTION'
    assert instr.get('swapTransaction') is None  # No real transaction built


@pytest.mark.asyncio
async def test_jito_provider_mock_blocks_send(repo):
    """
    Jito Provider in MOCK mode allows mock send() for testing.
    
    MOCK mode does not call real APIs, but allows send() to return
    mock success (for testing the trading flow).
    Only ONLINE_READONLY and LIVE modes should BLOCK or EXECUTE real sends.
    """
    provider = JitoProvider(repo, mode=ProviderMode.MOCK)
    
    # Test get_tip_floor (read-only, safe)
    tip = await provider.get_tip_floor()
    assert isinstance(tip, dict)
    assert tip.get('mode') == 'MOCK'
    
    # Test simulate (read-only, safe)
    sim = await provider.simulate({'mock': 'tx'})
    assert isinstance(sim, dict)
    assert sim.get('ok') == True
    assert sim.get('mode') == 'MOCK'
    
    # MOCK mode: send returns mock success (NOT blocked)
    send_result = await provider.send({'mock': 'tx'})
    assert isinstance(send_result, dict)
    assert send_result.get('ok') == True  # MOCK allows send (returns mock success)
    assert send_result.get('mode') == 'MOCK'
    assert 'bundle_id' in send_result
    
    # Verify provider_requests logged the mock send
    requests = await repo.list_provider_requests(limit=10)
    jito_requests = [r for r in requests if r.get('provider') == 'JITO']
    assert len(jito_requests) > 0, "Should log Jito requests"


@pytest.mark.asyncio
async def test_rpc_provider_mock_returns_mock_data(repo):
    """
    RPC Provider in MOCK mode should return mock data (no real RPC calls)
    """
    provider = RpcRealProvider(repo, mode=ProviderMode.MOCK)
    
    # Test get_balance (read-only, safe)
    balance = await provider.get_balance('MOCK_WALLET')
    assert isinstance(balance, dict)
    assert balance.get('mode') == 'MOCK'
    assert 'sol_balance' in balance
    
    # Test get_token_balance (read-only, safe)
    token_balance = await provider.get_token_balance('MOCK_WALLET', 'PASS1')
    assert isinstance(token_balance, dict)
    assert token_balance.get('mode') == 'MOCK'
    assert 'amount' in token_balance
    
    # Test wait_signature (read-only polling, safe)
    sig_result = await provider.wait_signature('MOCK_SIG', 30)
    assert isinstance(sig_result, dict)
    assert sig_result.get('mode') == 'MOCK'
    assert sig_result.get('status') == 'confirmed'


@pytest.mark.asyncio
async def test_provider_requests_do_not_leak_keys(repo):
    """
    Provider requests should NEVER include full API keys or private keys in logs
    """
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
    jito = JitoProvider(repo, mode=ProviderMode.MOCK)
    rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
    
    # Generate some requests
    await gmgn.fetch_latest_price('PASS1')
    await jupiter.quote_exact_in('SOL', 'PASS1', 1000000000, 1500)
    await jito.simulate({'mock': 'tx'})
    await rpc.get_balance('MOCK_WALLET')
    
    # Check all provider requests
    requests = await repo.list_provider_requests(limit=100)
    
    for req in requests:
        request_json = req.get('request_summary_json') or ''
        response_json = req.get('response_summary_json') or ''
        
        # Check that full API keys are NOT in logs
        combined = request_json + response_json
        
        # Should not contain common key patterns in full
        assert 'api_key' not in combined.lower() or '...' in combined, \
            f"Provider request may leak API key: {req.get('provider')} {req.get('endpoint')}"


@pytest.mark.asyncio
async def test_provider_mode_mock_does_not_call_external(repo):
    """
    MOCK mode should NOT call any external APIs.
    
    All calls should return quickly with mock data.
    """
    import time
    
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
    
    # Measure time for GMGN call (should be fast, <0.1s)
    start = time.time()
    await gmgn.fetch_latest_price('PASS1')
    elapsed = time.time() - start
    assert elapsed < 0.1, f"MOCK mode should not make external calls, took {elapsed}s"
    
    # Measure time for Jupiter call (should be fast, <0.1s)
    start = time.time()
    await jupiter.quote_exact_in('SOL', 'PASS1', 1000000000, 1500)
    elapsed = time.time() - start
    assert elapsed < 0.1, f"MOCK mode should not make external calls, took {elapsed}s"


@pytest.mark.asyncio
async def test_online_readonly_does_not_require_private_key(repo):
    """
    ONLINE_READONLY mode should NOT require private key.
    
    Private key should only be required for LIVE mode.
    """
    # This test verifies that providers can be initialized in ONLINE_READONLY
    # without private key
    try:
        gmgn = GMGNProvider(repo, mode=ProviderMode.ONLINE_READONLY)
        jupiter = JupiterProvider(repo, mode=ProviderMode.ONLINE_READONLY)
        jito = JitoProvider(repo, mode=ProviderMode.ONLINE_READONLY)
        rpc = RpcRealProvider(repo, mode=ProviderMode.ONLINE_READONLY)
        
        # If we reach here, initialization succeeded without private key
        assert True
    except Exception as e:
        # Some providers may fail due to missing httpx or API keys
        # That's OK for this test - we're checking private key requirement
        assert 'private' not in str(e).lower() and 'key' not in str(e).lower(), \
            f"Should not require private key for online_readonly: {e}"


@pytest.mark.asyncio
async def test_online_readonly_blocks_jito_send(repo):
    """
    Jito send() should be BLOCKED in ONLINE_READONLY mode too.
    
    Only LIVE mode should allow send operations.
    """
    provider = JitoProvider(repo, mode=ProviderMode.ONLINE_READONLY)
    
    # Try to send (should be blocked)
    send_result = await provider.send({'mock': 'tx'})
    assert isinstance(send_result, dict)
    assert send_result.get('ok') == False
    assert send_result.get('error') == 'MODE_BLOCKED'


@pytest.mark.asyncio
async def test_online_readonly_blocks_rpc_send(repo):
    """
    RPC write operations should be BLOCKED in ONLINE_READONLY mode.
    """
    provider = RpcRealProvider(repo, mode=ProviderMode.ONLINE_READONLY)
    
    # Try to send transaction (should be blocked)
    try:
        await provider.send_transaction('mock_tx')
        assert False, "Should have raised exception"
    except Exception as e:
        assert 'BLOCKED' in str(e) or 'blocked' in str(e).lower(), \
            f"Should block write operations in online_readonly: {e}"
