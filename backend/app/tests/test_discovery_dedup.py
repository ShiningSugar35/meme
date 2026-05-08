"""
Tests for discovery event idempotency (Phase A)
Verifies that same snapshot_id does not create duplicate discovery events/positions.
"""
import pytest
import pytest_asyncio
from ..db.repositories import Repositories
from ..trading.executor import TradingPipeline
from ..config import ProviderMode
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcRealProvider


@pytest_asyncio.fixture
async def repo_and_pipeline(tmp_path):
    db_file = tmp_path / "test_discovery.db"
    repo = await Repositories.create(str(db_file))
    await repo.ensure_default_strategy_groups()
    gmgn = GMGNProvider(repo, mode=ProviderMode.MOCK)
    jupiter = JupiterProvider(repo, mode=ProviderMode.MOCK)
    jito = JitoProvider(repo, mode=ProviderMode.MOCK)
    rpc = RpcRealProvider(repo, mode=ProviderMode.MOCK)
    pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)
    try:
        yield repo, pipeline
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_same_snapshot_no_duplicate_discovery_event(repo_and_pipeline):
    """Same snapshot_id second call does not create second discovery event."""
    repo, pipeline = repo_and_pipeline
    groups = await repo.get_enabled_strategy_groups()
    live_groups = [g for g in groups if g['is_live']]
    
    # First call with snapshot_id=100 using PASS1 (existing mock token)
    await pipeline.handle_token_second_filter_result('PASS1', live_groups, snapshot_id=100)
    
    # Check discovery events
    events = await repo.list_discovery_events(token_mint='PASS1')
    assert len(events) == 1, "Should have 1 discovery event after first call"
    first_event_id = events[0]['id']
    
    # Second call with same snapshot_id=100
    await pipeline.handle_token_second_filter_result('PASS1', live_groups, snapshot_id=100)
    
    events_after = await repo.list_discovery_events(token_mint='PASS1')
    assert len(events_after) == 1, "Should still have 1 discovery event after second call"
    assert events_after[0]['id'] == first_event_id, "Discovery event ID should be same"


@pytest.mark.asyncio
async def test_same_snapshot_no_duplicate_live_position(repo_and_pipeline):
    """Same snapshot_id second call does not create second live position."""
    repo, pipeline = repo_and_pipeline
    groups = await repo.get_enabled_strategy_groups()
    live_groups = [g for g in groups if g['is_live']]
    
    # First call
    await pipeline.handle_token_second_filter_result('PASS1', live_groups, snapshot_id=200)
    positions = await repo.list_positions_by_token_and_is_live('PASS1', True)
    open_pos = [p for p in positions if p.get('status') != 'CLOSED']
    assert len(open_pos) == 1, "First call should create 1 live position"
    
    # Second call same snapshot
    await pipeline.handle_token_second_filter_result('PASS1', live_groups, snapshot_id=200)
    positions_after = await repo.list_positions_by_token_and_is_live('PASS1', True)
    open_pos_after = [p for p in positions_after if p.get('status') != 'CLOSED']
    assert len(open_pos_after) == 1, "Second call should not create new live position"


@pytest.mark.asyncio
async def test_same_snapshot_no_duplicate_simulated_positions(repo_and_pipeline):
    """Same snapshot_id second call does not create duplicate simulated positions."""
    repo, pipeline = repo_and_pipeline
    groups = await repo.get_enabled_strategy_groups()
    sim_groups = [g for g in groups if not g['is_live']]
    
    # First call
    await pipeline.handle_token_second_filter_result('PASS1', sim_groups, snapshot_id=300)
    sim_positions = await repo.list_positions_by_token_and_is_live('PASS1', False)
    assert len(sim_positions) >= 1, "First call should create simulated positions"
    
    # Second call same snapshot
    await pipeline.handle_token_second_filter_result('PASS1', sim_groups, snapshot_id=300)
    sim_positions_after = await repo.list_positions_by_token_and_is_live('PASS1', False)
    assert len(sim_positions_after) == len(sim_positions), "No new simulated positions for same snapshot"


@pytest.mark.asyncio
async def test_diff_snapshot_allows_new_cycle(repo_and_pipeline):
    """Different snapshot_id allows new cycle but respects open live position rule."""
    repo, pipeline = repo_and_pipeline
    groups = await repo.get_enabled_strategy_groups()
    live_groups = [g for g in groups if g['is_live']]
    
    # First cycle
    await pipeline.handle_token_second_filter_result('PASS1', live_groups, snapshot_id=400)
    positions = await repo.list_positions_by_token_and_is_live('PASS1', True)
    open_pos = [p for p in positions if p.get('status') != 'CLOSED']
    assert len(open_pos) == 1
    
    # Different snapshot but token has open live position - should block
    await pipeline.handle_token_second_filter_result('PASS1', live_groups, snapshot_id=401)
    positions_after = await repo.list_positions_by_token_and_is_live('PASS1', True)
    open_pos_after = [p for p in positions_after if p.get('status') != 'CLOSED']
    assert len(open_pos_after) == 1, "New snapshot should not create live position if open exists"


@pytest.mark.asyncio
async def test_discovery_event_id_in_strategy_matches(repo_and_pipeline):
    """token_strategy_matches should have discovery_event_id set."""
    repo, pipeline = repo_and_pipeline
    groups = await repo.get_enabled_strategy_groups()
    
    await pipeline.handle_token_second_filter_result('PASS1', groups, snapshot_id=500)
    
    matches = await repo.list_token_strategy_matches('PASS1')
    assert len(matches) > 0, "Should have strategy matches"
    for m in matches:
        assert m.get('discovery_event_id') is not None, "Strategy match must have discovery_event_id"


@pytest.mark.asyncio
async def test_discovery_event_id_in_positions(repo_and_pipeline):
    """positions should have discovery_event_id set."""
    repo, pipeline = repo_and_pipeline
    groups = await repo.get_enabled_strategy_groups()
    live_groups = [g for g in groups if g['is_live']]
    
    await pipeline.handle_token_second_filter_result('PASS1', live_groups, snapshot_id=600)
    
    positions = await repo.list_positions_by_token_and_is_live('PASS1', True)
    assert len(positions) > 0
    for p in positions:
        assert p.get('discovery_event_id') is not None, "Position must have discovery_event_id"


@pytest.mark.asyncio
async def test_discovery_event_id_in_bandit_observations(repo_and_pipeline):
    """bandit_observations should have discovery_event_id set."""
    repo, pipeline = repo_and_pipeline
    groups = await repo.get_enabled_strategy_groups()
    
    await pipeline.handle_token_second_filter_result('PASS1', groups, snapshot_id=700)
    
    observations = await repo.list_token_bandit_observations('PASS1')
    assert len(observations) > 0
    for o in observations:
        assert o.get('discovery_event_id') is not None, "Bandit observation must have discovery_event_id"
