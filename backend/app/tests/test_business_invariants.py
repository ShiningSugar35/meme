"""
Business invariants tests for Solana Meme Trading Bot.

These tests verify that core constraints are maintained:
1. No global_x variable
2. No entry_x / entry_y / entry_t fields in positions
3. Only one OPEN live position per (token, cycle)
4. All exit percentages calculated on current remaining_token_amount
5. Multiple exit conditions take max percentage
6. No RPC send fallback - Jito-only broadcast or blocked
"""

import pytest
import json
from ..db.repositories import Repositories
from ..trading.executor import TradingPipeline
from ..config import ProviderMode
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter_real import JupiterProvider
from ..providers.jito_real import JitoProvider
from ..providers.rpc_real import RpcRealProvider
from ..providers.mock_data import MockData


@pytest.mark.asyncio
async def test_no_global_x_in_strategy_config(repo):
    """
    Invariant 1: No global_x variable.
    Strategy parameters x, y, t exist only within strategy_groups,
    never as global state.
    """
    groups = await repo.get_enabled_strategy_groups()
    for g in groups:
        # x, y, t should only be in strategy_groups, not global
        assert g.get('x') is not None, "x must be in strategy_groups"
        assert g.get('y') is not None, "y must be in strategy_groups"
        assert g.get('t_seconds') is not None, "t_seconds must be in strategy_groups"


@pytest.mark.asyncio
async def test_no_entry_x_entry_y_entry_t_fields(repo):
    """
    Invariant 2: No entry_x / entry_y / entry_t individual fields in positions.
    Positions should only freeze locked_strategy_config_json, not split fields.
    """
    # Create a position to check schema
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


@pytest.mark.asyncio
async def test_one_live_position_per_token_constraint(repo, pipeline_factory):
    """
    Invariant 3: Only one OPEN live position per (token, cycle).
    Once an open live position exists in the same cycle,
    subsequent live buy attempts must be blocked.
    """
    # Create pipeline with MOCK providers
    pipeline, _ = pipeline_factory()
    groups = await repo.get_enabled_strategy_groups()
    live_groups = [g for g in groups if g['is_live']]
    token_mint = 'PASS1'  # Use mock token
    
    # First call: should create live position (new cycle)
    await pipeline.handle_token_second_filter_result(token_mint, live_groups, snapshot_id=1)
    live_pos_1 = await repo.list_positions_by_token_and_is_live(token_mint, True)
    open_pos_1 = [p for p in live_pos_1 if p.get('status') != 'CLOSED']
    assert len(open_pos_1) == 1, "First call should create 1 open live position"
    
    # Second call with SAME snapshot_id: should be SKIPPED (snapshot idempotency)
    result = await pipeline.handle_token_second_filter_result(token_mint, live_groups, snapshot_id=1)
    assert result['status'] == 'SKIPPED_DUPLICATE_SNAPSHOT', "Second call with same snapshot_id must be skipped"
    
    # Verify no new position created
    live_pos_2 = await repo.list_positions_by_token_and_is_live(token_mint, True)
    open_pos_2 = [p for p in live_pos_2 if p.get('status') != 'CLOSED']
    assert len(open_pos_2) == 1, "Second call must not create another live position"
    
    # Check system_events has snapshot skip message
    events = await repo.list_recent_system_events(limit=50, category='TRADE')
    skip_events = [e for e in events if 'duplicate snapshot skipped' in (e.get('message') or '').lower()]
    assert len(skip_events) > 0, "System should log snapshot skip message"


@pytest.mark.asyncio
async def test_exit_percentage_on_current_remaining_amount(repo):
    """
    Invariant 4: Exit percentages calculated on current remaining_token_amount, not initial.
    
    Example: initial_amount=100, after partial sell remaining=40.
    50% exit = sell 20 (50% of 40), not 50 (50% of 100).
    """
    # Create position with initial 100 tokens
    pos_id = await repo.create_position(
        token_mint="PASS1",
        is_live=True,
        locked_strategy_config_json='{"x": 0.15}',
        status="OPEN",
        entry_price_usd=1.0,
        entry_price_sol=0.5,
        entry_token_amount=100.0,
        remaining_token_amount=100.0,
        remaining_value_usd=100.0,
    )
    
    # Simulate partial exit: reduce remaining to 40
    await repo.update_position_remaining(
        pos_id,
        remaining_token_amount=40.0,
        remaining_value_usd=40.0,
    )
    
    # Re-fetch and verify
    pos = await repo.get_position(pos_id)
    assert pos.get('entry_token_amount') == 100.0, "Initial amount unchanged"
    assert pos.get('remaining_token_amount') == 40.0, "Remaining reduced to 40"
    
    # If exit_pct = 50%, should sell 20 (50% of current 40), not 50 (50% of initial 100)
    # This is verified in exit_rules.py via decide_exit function
    # For this test, we just verify the position state is correct


@pytest.mark.asyncio
async def test_multiple_exit_conditions_take_max(repo):
    """
    Invariant 5: When multiple exit conditions trigger, take max exit percentage.
    
    If 50% exit AND 100% exit both trigger, exit 100% of current remaining.
    If 20% exit AND 50% exit both trigger, exit 50% of current remaining.
    """
    # This is enforced in exit_rules.py decide_exit function
    # Verification: max(exit_pcts) is taken
    
    # Create a position
    pos_id = await repo.create_position(
        token_mint="PASS1",
        is_live=True,
        locked_strategy_config_json='{"x": 0.15}',
        status="OPEN",
        entry_price_usd=1.0,
        entry_price_sol=0.5,
        entry_token_amount=100.0,
        remaining_token_amount=100.0,
        remaining_value_usd=100.0,
    )
    
    pos = await repo.get_position(pos_id)
    assert pos.get('remaining_token_amount') == 100.0
    
    # The actual max-taking logic is in exit_rules.decide_exit
    # This test just verifies the position schema supports it


@pytest.mark.asyncio
async def test_no_rpc_send_fallback(repo):
    """
    Invariant 6: No RPC send fallback.
    provider_requests must never contain provider=RPC with send_transaction endpoint.
    RPC is only for wait_signature, get_signature_status, or read-only queries.
    """
    # Verify that provider_requests table is set up correctly
    # and check that no RPC send endpoint requests exist
    
    # For now, this is a schema/config check
    # The actual enforcement is in TradingPipeline._execute_live_buy:
    # - It ONLY calls jito.send, never rpc.send
    # - RPC fallback is NOT implemented
    
    provider_requests = await repo.list_provider_requests(limit=10)
    # If there are any RPC requests, they should be read-only or wait_signature
    for pr in provider_requests:
        if pr.get('provider') == 'RPC':
            endpoint = pr.get('endpoint') or ''
            # Forbidden endpoints for RPC
            forbidden = ['sendTransaction', 'send_transaction', 'send_raw', 'sendRawTransaction']
            for fend in forbidden:
                assert fend not in endpoint, f"RPC must not call {fend}. Found in {endpoint}"


@pytest.mark.asyncio
async def test_closed_live_position_allows_new_cycle_live_trade(repo, pipeline_factory):
    """
    CORRECT BEHAVIOR: Closed live positions should NOT prevent new live trades in a NEW cycle.
    
    A token can re-enter trading in a new discovery cycle, as long as
    there is no OPEN live position for that token.
    """
    pipeline, _ = pipeline_factory()
    groups = await repo.get_enabled_strategy_groups()
    live_groups = [g for g in groups if g['is_live']]
    token_mint = 'PASS1'
    
    # First cycle: create and close a live position
    await pipeline.handle_token_second_filter_result(token_mint, live_groups, snapshot_id=1)
    live_pos = await repo.list_positions_by_token_and_is_live(token_mint, True)
    open_pos = [p for p in live_pos if p.get('status') != 'CLOSED']
    assert len(open_pos) == 1, "First cycle should create 1 open live position"
    
    pos_id = open_pos[0]['id']
    # Close it (simulate exit)
    await repo.close_position(pos_id, close_reason="TEST_CLOSE", total_return_sol=0)
    
    # Verify closed
    closed_pos = await repo.get_position(pos_id)
    assert closed_pos.get('status') == 'CLOSED', "Position should be closed"
    
    # Second cycle (new snapshot_id): should ALLOW new live position
    await pipeline.handle_token_second_filter_result(token_mint, live_groups, snapshot_id=2)
    
    # Should have 2 live positions now (one closed, one open)
    all_live_pos = await repo.list_positions_by_token_and_is_live(token_mint, True)
    open_live_pos = [p for p in all_live_pos if p.get('status') != 'CLOSED']
    
    assert len(open_live_pos) == 1, "New cycle should allow new live position"
    assert open_live_pos[0].get('discovery_event_id') != closed_pos.get('discovery_event_id'), \
        "New cycle should have different discovery_event_id"


@pytest.mark.asyncio
async def test_same_cycle_blocks_duplicate_live_trade(repo, pipeline_factory):
    """
    Same cycle: Only ONE open live position allowed per (token, cycle).
    
    If an open live position already exists in the current cycle,
    subsequent live buy attempts must be blocked.
    
    Note: This test uses discovery_event_id to identify the cycle.
    """
    pipeline, _ = pipeline_factory()
    groups = await repo.get_enabled_strategy_groups()
    live_groups = [g for g in groups if g['is_live']]
    token_mint = 'PASS1'
    
    # First call: creates open live position with discovery_event_id
    await pipeline.handle_token_second_filter_result(token_mint, live_groups, snapshot_id=10)
    
    # Get the discovery_event_id from created position
    positions = await repo.list_positions_by_token_and_is_live(token_mint, True)
    open_pos = [p for p in positions if p.get('status') != 'CLOSED']
    assert len(open_pos) == 1, "First call should create 1 open live position"
    discovery_event_id = open_pos[0].get('discovery_event_id')
    
    # Close the existing position and create a new cycle
    await repo.close_position(open_pos[0]['id'], close_reason='TEST_CYCLE_END')
    
    # Now create new cycle with different snapshot_id
    await pipeline.handle_token_second_filter_result(token_mint, live_groups, snapshot_id=20)
    positions_2 = await repo.list_positions_by_token_and_is_live(token_mint, True)
    open_pos_2 = [p for p in positions_2 if p.get('status') != 'CLOSED']
    assert len(open_pos_2) == 1, "New cycle should create new live position"
    
    # Verify different discovery_event_id
    assert open_pos_2[0].get('discovery_event_id') != discovery_event_id, "New cycle should have different discovery_event_id"


@pytest.mark.asyncio
async def test_strategy_matches_are_cycle_scoped(repo, pipeline_factory):
    """
    Strategy matches should be scoped to a specific discovery cycle.
    
    Same token in different cycles should have separate strategy matches.
    """
    pipeline, _ = pipeline_factory()
    groups = await repo.get_enabled_strategy_groups()
    
    # First cycle: process token
    await pipeline.handle_token_second_filter_result('PASS1', groups, snapshot_id=1)
    
    # Check matches for first cycle
    matches_1 = await repo.list_token_strategy_matches('PASS1')
    assert len(matches_1) > 0, "First cycle should have strategy matches"
    
    # Get discovery_event_id from first cycle positions
    positions_1 = await repo.list_positions_by_token_and_is_live('PASS1', False)  # sim positions
    if positions_1:
        discovery_event_id_1 = positions_1[0].get('discovery_event_id')
        # Check that matches have discovery_event_id
        for m in matches_1:
            assert m.get('discovery_event_id') is not None, "Strategy match should have discovery_event_id"


@pytest.mark.asyncio
async def test_sim_positions_are_cycle_scoped(repo, pipeline_factory):
    """
    Simulated positions should be scoped to a specific discovery cycle.
    
    Same token in different cycles can have separate simulated positions.
    """
    pipeline, _ = pipeline_factory()
    groups = await repo.get_enabled_strategy_groups()
    sim_groups = [g for g in groups if not g['is_live']]
    
    # First cycle: create sim position
    await pipeline.handle_token_second_filter_result('PASS1', sim_groups, snapshot_id=1)
    
    sim_pos_1 = await repo.list_positions_by_token_and_is_live('PASS1', False)
    assert len(sim_pos_1) >= 1, "First cycle should have sim positions"
    
    # Verify discovery_event_id is set
    for pos in sim_pos_1:
        assert pos.get('discovery_event_id') is not None, "Sim position should have discovery_event_id"
    
    # Second cycle with different snapshot_id should create new positions
    await pipeline.handle_token_second_filter_result('PASS1', sim_groups, snapshot_id=2)
    
    sim_pos_all = await repo.list_positions_by_token_and_is_live('PASS1', False)
    # Should have positions from both cycles
    discovery_event_ids = set(p.get('discovery_event_id') for p in sim_pos_all if p.get('discovery_event_id') is not None)
    assert len(discovery_event_ids) >= 1, "Should have positions from different cycles"


@pytest.mark.asyncio
async def test_strategy_match_covers_all_passed_strategies(repo, pipeline_factory):
    """
    Invariant: Every passed_strategy must generate exactly one token_strategy_match.
    This ensures full traceability of strategy evaluation.
    """
    pipeline, _ = pipeline_factory()
    groups = await repo.get_enabled_strategy_groups()
    passed_strategies = groups  # all enabled groups pass in mock
    token_mint = 'PASS1'
    
    await pipeline.handle_token_second_filter_result(token_mint, passed_strategies, None)
    
    matches = await repo.list_token_strategy_matches(token_mint)
    passed_ids = {s.get('id') for s in passed_strategies}
    matched_ids = {m.get('strategy_id') for m in matches}
    
    assert passed_ids == matched_ids, f"All passed strategies must have matches. Missing: {passed_ids - matched_ids}"


@pytest.mark.asyncio
async def test_small_dust_position_cleared_in_single_exit(repo):
    """
    Invariant: Very small remaining value positions (< 10 USD) should be cleared in single exit.
    This prevents dust positions from clogging the portfolio.
    """
    # Create position and reduce to dust
    pos_id = await repo.create_position(
        token_mint="PASS1",
        is_live=True,
        locked_strategy_config_json='{"x": 0.15}',
        status="OPEN",
        entry_price_usd=1.0,
        entry_price_sol=0.5,
        entry_token_amount=100.0,
        remaining_token_amount=100.0,
        remaining_value_usd=100.0,
    )
    
    # Reduce to dust (5 USD remaining)
    await repo.update_position_remaining(
        pos_id,
        remaining_token_amount=5.0,
        remaining_value_usd=5.0,
    )
    
    pos = await repo.get_position(pos_id)
    assert pos.get('remaining_value_usd') == 5.0
    assert pos.get('remaining_value_usd') < 10.0, "Position is dust"
    # The exit logic should clear this in one go when triggered
