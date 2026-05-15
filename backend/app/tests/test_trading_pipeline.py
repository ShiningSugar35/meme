import pytest
from datetime import datetime
from ..db.repositories import Repositories
from ..trading.executor import TradingPipeline
from ..providers.mock_data import MockData
from ..providers.gmgn_real import GMGNProvider
from ..providers.jupiter import JupiterProvider
from ..providers.jito import JitoProvider
from ..providers.rpc import MockRpcProvider


async def _ensure_live_group(repo, name="test_live"):
    """Create a live strategy group if none exists."""
    groups = await repo.list_strategy_groups()
    live = [g for g in groups if g['is_live']]
    if not live:
        await repo.create_strategy_group(name, 0.15, 2.25, 150, is_live=True, priority=10, raw_config_json='{}')
        groups = await repo.list_strategy_groups()
    return [g for g in groups if g['is_live']]


@pytest.mark.asyncio
async def test_only_one_live_position_per_token(repo, pipeline_factory):
    pipeline, mock = pipeline_factory()
    live_groups = await _ensure_live_group(repo, "test_live_pos")
    token_mint = 'PASS1'
    await pipeline.handle_token_second_filter_result(token_mint, live_groups, None)

    positions = await repo.list_positions_by_token_and_is_live(token_mint, True)
    assert len(positions) == 1, f"Expected 1 live position, got {len(positions)}"
    p = positions[0]
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
    assert len(buy_submitted) > 0, 'at least one BUY_SUBMITTED event'
    buy_confirmed = [t for t in tes if t['token_mint'] == token_mint and t['event_type'] == 'BUY_CONFIRMED' and t['is_live'] == 1]
    assert len(buy_confirmed) > 0, 'at least one BUY_CONFIRMED event'
    assert buy_confirmed[0].get('tx_signature') is not None
    assert buy_confirmed[0].get('bundle_id') is not None


@pytest.mark.asyncio
async def test_simulated_positions_for_losers(repo, pipeline_factory):
    pipeline, mock = pipeline_factory()
    groups = await repo.list_strategy_groups()
    sim_groups = [g for g in groups if not g['is_live']]
    if not sim_groups:
        await repo.create_strategy_group("test_sim", 0.18, 2.5, 300, is_live=False, priority=100, raw_config_json='{}')
        groups = await repo.list_strategy_groups()
        sim_groups = [g for g in groups if not g['is_live']]
    token_mint = 'PASS1'
    await pipeline.handle_token_second_filter_result(token_mint, sim_groups, None)
    positions = await repo.list_positions_by_token_and_is_live(token_mint, False)
    assert len(positions) >= 1, f"Expected at least 1 simulated position, got {len(positions)}"
    for p in positions:
        assert p.get('is_live') == 0
        assert p.get('status') in ['SIM_OPEN', 'CLOSED']


@pytest.mark.asyncio
async def test_jupiter_high_impact_blocks(repo, pipeline_factory):
    pipeline, mock = pipeline_factory('high_impact')
    live_groups = await _ensure_live_group(repo, "test_high_impact")
    token_mint = 'PASS1'
    result = await pipeline.handle_token_second_filter_result(token_mint, live_groups, None)
    tes = await repo.list_trade_events(100)
    buy_submitted = [t for t in tes if t['token_mint'] == token_mint and t['event_type'] == 'BUY_SUBMITTED']
    assert len(buy_submitted) == 0, 'high impact should prevent buy submission'


@pytest.mark.asyncio
async def test_duplicate_token_no_second_live(repo, pipeline_factory):
    pipeline, mock = pipeline_factory()
    live_groups = await _ensure_live_group(repo, "test_dup")
    await pipeline.handle_token_second_filter_result('PASS1', live_groups, None)
    tes1 = await repo.list_trade_events(100)
    first_buy = sum(1 for t in tes1 if t['side'] == 'BUY' and t['token_mint'] == 'PASS1')
    await pipeline.handle_token_second_filter_result('PASS1', live_groups, None)
    tes2 = await repo.list_trade_events(100)
    second_buy = sum(1 for t in tes2 if t['side'] == 'BUY' and t['token_mint'] == 'PASS1')
    assert second_buy <= first_buy + 0, 'should not create duplicate buy for same token'
