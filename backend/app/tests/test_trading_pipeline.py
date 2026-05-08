import pytest
from datetime import datetime
from ..db.repositories import Repositories
from ..trading.executor import TradingPipeline
from ..providers.mock_data import MockData
from ..providers.gmgn import GMGNProvider
from ..providers.jupiter import JupiterProvider
from ..providers.jito import JitoProvider
from ..providers.rpc import MockRpcProvider

@pytest.fixture(autouse=False)
def _noop():
    # placeholder to keep pytest imports happy if needed
    return None

@pytest.mark.asyncio
async def test_only_one_live_position_per_token(repo, pipeline_factory):
    pipeline, mock = pipeline_factory()
    groups = await repo.get_enabled_strategy_groups()
    live_groups = [g for g in groups if g['is_live']]
    token_mint = 'PASS1'
    # run pipeline
    await pipeline.handle_token_second_filter_result(token_mint, live_groups, None)

    # assertions on positions and trade events
    positions = await repo.list_positions_by_token_and_is_live(token_mint, True)
    assert len(positions) == 1, f"Expected 1 live position, got {len(positions)}"
    p = positions[0]
    # required fields
    assert p.get('live_strategy_id') is not None, 'live_strategy_id must be set'
    assert p.get('strategy_config_version') is not None, 'strategy_config_version must be set'
    assert p.get('total_cost_sol', 0) > 0, 'total_cost_sol must be > 0'
    assert p.get('entry_token_amount', 0) > 0, 'entry_token_amount must be > 0'
    assert p.get('remaining_token_amount', 0) == p.get('entry_token_amount', 0)
    assert p.get('open_trade_event_id') is not None, 'open_trade_event_id must be set'
    assert p.get('last_fill_at') is not None
    assert p.get('last_fill_price_usd') is not None

    tes = await repo.list_trade_events(100)
    buy_submitted = [t for t in tes if t['token_mint'] == token_mint and t['event_type'] == 'BUY_SUBMITTED' and t['is_live'] == 1]
    buy_confirmed = [t for t in tes if t['token_mint'] == token_mint and t['event_type'] == 'BUY_CONFIRMED' and t['is_live'] == 1]
    assert len(buy_submitted) == 1, f"Expected 1 BUY_SUBMITTED, got {len(buy_submitted)}"
    assert len(buy_confirmed) == 1, f"Expected 1 BUY_CONFIRMED, got {len(buy_confirmed)}"
    # position.open_trade_event_id should point to confirmed trade event
    assert p.get('open_trade_event_id') == buy_confirmed[0]['id']

@pytest.mark.asyncio
async def test_simulated_positions_for_losers(repo, pipeline_factory):
    pipeline, mock = pipeline_factory()
    groups = await repo.get_enabled_strategy_groups()
    live_groups = [g for g in groups if g['is_live']]
    sim_groups = [g for g in groups if not g['is_live']]
    all_passed = live_groups + sim_groups
    token_mint = 'PASS1'
    await pipeline.handle_token_second_filter_result(token_mint, all_passed, None)
    
    # Check live loser positions (all live groups except winner should have sim position)
    sim_positions = await repo.list_positions_by_token_and_is_live(token_mint, False)
    assert len(sim_positions) >= 1, "Expected at least one simulated position"
    
    # Check strategy matches: each passed_strategy must have a match
    matches = await repo.list_token_strategy_matches(token_mint)
    passed_strategy_ids = {s.get('id') for s in all_passed}
    matched_strategy_ids = {m.get('strategy_id') for m in matches if m.get('passed')}
    assert passed_strategy_ids == matched_strategy_ids, f"All passed strategies must have matches. passed={passed_strategy_ids}, matched={matched_strategy_ids}"
    
    # Check bandit observations: each passed strategy must have an observation
    observations = await repo.list_token_bandit_observations(token_mint)
    observed_strategy_ids = {o.get('strategy_id') for o in observations}
    assert passed_strategy_ids.issubset(observed_strategy_ids), f"All passed strategies must have observations. passed={passed_strategy_ids}, observed={observed_strategy_ids}"

@pytest.mark.asyncio
async def test_jupiter_high_impact_blocks(repo, pipeline_factory):
    pipeline, mock = pipeline_factory(scenario='high_impact')
    groups = await repo.get_enabled_strategy_groups()
    live_groups = [g for g in groups if g['is_live']]
    token_mint = 'PASS1'
    await pipeline.handle_token_second_filter_result(token_mint, live_groups, None)
    positions = await repo.list_positions_by_token_and_is_live(token_mint, True)
    assert len(positions) == 0, "Expected no live position when price impact too high"

@pytest.mark.asyncio
async def test_duplicate_token_no_second_live(repo, pipeline_factory):
    pipeline, mock = pipeline_factory()
    groups = await repo.get_enabled_strategy_groups()
    live_groups = [g for g in groups if g['is_live']]
    token_mint = 'PASS1'
    await pipeline.handle_token_second_filter_result(token_mint, live_groups, 1)
    positions = await repo.list_positions_by_token_and_is_live(token_mint, True)
    assert len(positions) == 1
    # second call with different snapshot
    await pipeline.handle_token_second_filter_result(token_mint, live_groups, 2)
    positions = await repo.list_positions_by_token_and_is_live(token_mint, True)
    assert len(positions) == 1, f"Expected still 1 live position, got {len(positions)}"
    # ensure system_events logged about duplicate/block
    events = await repo.list_recent_system_events(limit=50, category='TRADE')
    assert any('already' in (ev.get('message') or '').lower() or 'blocking duplicate' in (ev.get('message') or '').lower() for ev in events)
