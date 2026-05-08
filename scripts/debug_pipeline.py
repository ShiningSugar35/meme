import asyncio
import sys


async def main():
    # ensure repo root on path
    sys.path.insert(0, '.')
    from backend.app.db.repositories import Repositories
    from backend.app.trading.executor import TradingPipeline
    from backend.app.providers.mock_data import MockData
    from backend.app.providers.gmgn import GMGNProvider
    from backend.app.providers.jupiter import JupiterProvider
    from backend.app.providers.jito import JitoProvider
    from backend.app.providers.rpc import MockRpcProvider

    repo = await Repositories.create()
    await repo.ensure_default_strategy_groups()
    mock = MockData()
    gmgn = GMGNProvider(repo, mock)
    jupiter = JupiterProvider(repo, scenario='success')
    jito = JitoProvider(repo)
    rpc = MockRpcProvider(repo)
    pipeline = TradingPipeline(repo, gmgn, jupiter, jito, rpc)

    groups = await repo.get_enabled_strategy_groups()
    live_groups = [g for g in groups if g['is_live']]
    token_mint = 'PASS1'
    print('Running pipeline for', token_mint, 'live_groups:', live_groups)
    await pipeline.handle_token_second_filter_result(token_mint, live_groups, None)

    events = await repo.list_recent_system_events(limit=200)
    print('\n=== System events ===')
    for ev in reversed(events):
        print(ev)

    positions = await repo.list_positions_by_token_and_is_live(token_mint, True)
    print('\n=== Live positions ===')
    for p in positions:
        print(p)

    tes = await repo.list_trade_events(100)
    print('\n=== Trade events ===')
    for t in tes:
        print(t)


if __name__ == '__main__':
    asyncio.run(main())
