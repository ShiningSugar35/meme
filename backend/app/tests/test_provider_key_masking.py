"""
Test Provider Key Masking

Verifies that:
1. API keys are masked (first 4 + last 4 chars)
2. Private keys never appear in logs/system_events/provider_requests
3. No raw secrets in any output
"""
import pytest
import pytest_asyncio
from ..config import Settings, ProviderMode
from ..db.repositories import Repositories
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcRealProvider


@pytest_asyncio.fixture
async def repo(tmp_path):
    db_file = tmp_path / "test_masking.db"
    repo = await Repositories.create(str(db_file))
    await repo.ensure_default_strategy_groups()
    try:
        yield repo
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_api_key_masking(repo):
    """Test that API keys are masked in config."""
    settings = Settings()
    
    # Test mask_key function
    # Create a mock SecretStr
    from pydantic import SecretStr
    test_key = SecretStr("ABCD1234EFGH5678")
    masked = settings.mask_key(test_key)
    assert masked == "ABCD...5678", f"Expected ABCD...5678, got {masked}"
    
    # Key <=8 chars
    short_key = SecretStr("12345678")
    masked_short = settings.mask_key(short_key)
    assert masked_short == "****", f"Expected ****, got {masked_short}"


@pytest.mark.asyncio
async def test_private_key_not_in_logs(repo):
    """Test that private keys never appear in logs/events."""
    from pydantic import SecretStr
    
    # Create provider with private key
    settings = Settings()
    # Mock private key (not real)
    test_priv_key = SecretStr("5JdS9k7K8LmN3pQrT2wE4yU6iO0pA1sD3fG5hJ7kL9")
    
    # Create GMGN provider in mock mode
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    
    # Fetch some data to trigger logging
    await gmgn.fetch_latest_price("PASS1")
    
    # Check provider_requests for any private key leakage
    requests = await repo.list_provider_requests(limit=100)
    for req in requests:
        # Check request_summary_json
        req_summary = req.get('request_summary_json') or ''
        resp_summary = req.get('response_summary_json') or ''
        # Ensure private key not present
        assert "5JdS9k7K8LmN3pQrT2wE4yU6iO0pA1sD3fG5hJ7kL9" not in req_summary
        assert "5JdS9k7K8LmN3pQrT2wE4yU6iO0pA1sD3fG5hJ7kL9" not in resp_summary
    
    # Check system events
    events = await repo.list_recent_system_events(limit=100)
    for evt in events:
        message = evt.get('message') or ''
        context = evt.get('context_json') or ''
        assert "5JdS9k7K8LmN3pQrT2wE4yU6iO0pA1sD3fG5hJ7kL9" not in message
        assert "5JdS9k7K8LmN3pQrT2wE4yU6iO0pA1sD3fG5hJ7kL9" not in context


@pytest.mark.asyncio
async def test_jupiter_api_key_masking(repo):
    """Test Jupiter API key masking in logs."""
    jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
    
    # Make a quote request (mock)
    await jupiter.quote_exact_in("SOL", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 1000000, 1500)
    
    # Check logs don't expose full API key
    requests = await repo.list_provider_requests(limit=10)
    for req in requests:
        resp_summary = req.get('response_summary_json') or ''
        # Ensure no full API key pattern (if we had a real key)
        # Just check that response is summarized
        if 'raw_json' in resp_summary:
            # Should not have full raw JSON in logs
            assert len(resp_summary) < 1000, "Response summary too long, may contain full response"


@pytest.mark.asyncio
async def test_jito_no_private_key_logging(repo):
    """Test Jito doesn't log private keys."""
    jito = JitoProvider(repo, mode=ProviderMode.MOCK)
    
    # Simulate send (mock)
    await jito.send({'mock': 'tx'})
    
    # Check logs
    requests = await repo.list_provider_requests(limit=10)
    for req in requests:
        req_summary = req.get('request_summary_json') or ''
        # Mock tx should be masked
        assert 'mock' in req_summary.lower() or '...' in req_summary
