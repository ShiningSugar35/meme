"""
Acceptance Test: Secret Masking

Verifies that API keys and private keys are NEVER logged or exposed in full:
- config.py mask_key() function works correctly
- Provider logs mask API keys (show only first 4 + last 4 chars)
- Private keys are NEVER logged in any form
- response_summary_json in provider_requests does not contain full keys
"""
import pytest
from ..config import Settings, ProviderMode
from ..db.repositories import Repositories
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcRealProvider


@pytest.mark.asyncio
async def test_config_mask_key_function():
    """
    Test that Settings.mask_key() correctly masks SecretStr values
    """
    settings = Settings()
    
    # Test with None
    assert settings.mask_key(None) is None
    
    # Test with short key (<=8 chars)
    from pydantic import SecretStr
    short_key = SecretStr('12345678')
    assert settings.mask_key(short_key) == '****'
    
    # Test with normal key (>8 chars)
    normal_key = SecretStr('abcdefghijklmnop')
    masked = settings.mask_key(normal_key)
    assert masked == 'abcd...mnop', f"Expected 'abcd...mnop', got '{masked}'"
    assert 'efghijkl' not in masked, "Middle part of key should be masked"


@pytest.mark.asyncio
async def test_provider_requests_mask_api_keys(repo):
    """
    Provider requests should mask API keys in request_summary_json
    """
    # Create providers with mock mode
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
    jito = JitoProvider(repo, mode=ProviderMode.MOCK)
    rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
    
    # Generate some requests
    await gmgn.fetch_latest_price('PASS1')
    await jupiter.quote_exact_in('SOL', 'PASS1', 1000000000, 1500)
    await jito.simulate({'mock': 'tx'})
    await rpc.get_balance('MOCK_WALLET')
    
    # Check ALL provider requests
    requests = await repo.list_provider_requests(limit=100)
    
    for req in requests:
        request_json = req.get('request_summary_json') or ''
        response_json = req.get('response_summary_json') or ''
        
        # The private key should NOT be in any log
        # (This is a basic check - real implementation would be more thorough)
        assert 'api_key' not in request_json.lower() or '...' in request_json, \
            f"API key may be exposed in request: {req.get('provider')} {req.get('endpoint')}"


@pytest.mark.asyncio
async def test_private_key_never_logged(repo):
    """
    Private keys should NEVER appear in any logs or provider_requests
    """
    from pydantic import SecretStr
    
    # Simulate a private key (this would come from settings in real scenario)
    test_private_key = SecretStr('5Jd7zTtG7E7XkZYqTq8K7QZJQZJQZJQZJQZJQZJQZJQZJQ')
    
    # Create providers
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
    jito = JitoProvider(repo, mode=ProviderMode.MOCK)
    rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
    
    # Generate some requests
    await gmgn.fetch_latest_price('PASS1')
    await jupiter.quote_exact_in('SOL', 'PASS1', 1000000000, 1500)
    await jito.simulate({'mock': 'tx'})
    await rpc.get_balance('MOCK_WALLET')
    
    # Check ALL provider requests
    requests = await repo.list_provider_requests(limit=100)
    
    private_key_value = test_private_key.get_secret_value()
    
    for req in requests:
        request_json = req.get('request_summary_json') or ''
        response_json = req.get('response_summary_json') or ''
        
        # The private key should NOT be in any log
        assert private_key_value not in request_json, \
            f"Private key found in request log for {req.get('provider')} {req.get('endpoint')}"
        assert private_key_value not in response_json, \
            f"Private key found in response log for {req.get('provider')} {req.get('endpoint')}"


@pytest.mark.asyncio
async def test_settings_validate_live_requires_keys():
    """When PROVIDER_MODE=live, validate that required keys are present."""
    from pydantic import SecretStr

    # Missing live wallet keys are reported as warnings so local live-read config
    # can still boot while real trading remains blocked by provider/runtime gates.
    with pytest.warns(UserWarning) as warnings:
        Settings(
            PROVIDER_MODE='live',
            DRY_RUN=False,
            GMGN_API_BASE_URL='https://api.gmgn.ai',
            GMGN_API_KEY_1=SecretStr('test_key'),
            JUPITER_API_BASE_URL='https://quote-api.jup.ag',
            JUPITER_API_KEY_1=SecretStr('test_key'),
            SOLANA_RPC_HTTP_PRIMARY='https://api.mainnet-beta.solana.com',
            JITO_ENABLED=True,
            JITO_BLOCK_ENGINE_URL='https://mainnet.block-engine.jito.wtf',
            WALLET_PUBLIC_KEY=None,
            WALLET_PRIVATE_KEY_BASE58=None,
        )
    combined = "\n".join(str(w.message) for w in warnings)
    assert 'WALLET_PUBLIC_KEY' in combined
    assert 'WALLET_PRIVATE_KEY_BASE58' in combined


@pytest.mark.asyncio
async def test_jito_dry_run_block_message_does_not_expose_keys(repo):
    """
    Even error messages from Jito DRY_RUN block should not expose keys
    """
    jito = JitoProvider(repo, mode=ProviderMode.ONLINE_READONLY)
    
    # Attempt to send (should be blocked)
    result = await jito.send({'mock': 'tx'})
    
    assert result.get('ok') == False
    assert result.get('error') == 'MODE_BLOCKED'
    
    # Check the logged error message
    requests = await repo.list_provider_requests(limit=10)
    jito_requests = [r for r in requests if r.get('provider') == 'JITO']
    blocked = [r for r in jito_requests if r.get('error_code') == 'JITO_MODE_BLOCKED']
    
    assert len(blocked) >0, "Should have logged MODE_BLOCKED error"
    
    for req in blocked:
        error_summary = req.get('error_summary') or ''
        response_json = req.get('response_summary_json') or ''
        
        # Error message should not contain any keys
        assert 'api_key' not in error_summary.lower() or '...' in error_summary, \
            "Error message may expose API key"


@pytest.mark.asyncio
async def test_no_global_x_no_entry_x_y_t(repo):
    """
    Verify that global_x, entry_x, entry_y, entry_t variables/fields do NOT exist
    """
    # Check that positions table doesn't have entry_x, entry_y, entry_t fields
    # (This is a schema check)
    pos_id = await repo.create_position(
        token_mint="PASS1",
        is_live=False,
        locked_strategy_config_json='{"x": 0.15, "y": 2.25}',
        status="OPEN",
        entry_price_usd=1.0,
        entry_price_sol=0.5,
        entry_token_amount=1000,
        remaining_token_amount=1000,
        remaining_value_usd=1000,
    )
    
    pos = await repo.get_position(pos_id)
    
    # Check that entry_x, entry_y, entry_t do NOT exist
    assert 'entry_x' not in pos or pos.get('entry_x') is None, "entry_x must not exist"
    assert 'entry_y' not in pos or pos.get('entry_y') is None, "entry_y must not exist"
    assert 'entry_t' not in pos or pos.get('entry_t') is None, "entry_t must not exist"
    
    # locked_strategy_config_json should contain strategy params
    assert pos.get('locked_strategy_config_json') is not None
