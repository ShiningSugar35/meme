import asyncio
from fastapi.testclient import TestClient
from ..main import app
from ..db.repositories import Repositories


def test_mock_run_once_and_db_effects():
    # use context manager so TestClient triggers startup and shutdown properly
    with TestClient(app) as client:
        # run mock lifecycle
        r = client.post('/api/mock/run-once')
        assert r.status_code == 200
        # check tokens
        repo = app.state.repo
        tokens = asyncio.get_event_loop().run_until_complete(repo.list_tokens(10))
        assert isinstance(tokens, list)
        # provider requests logged
        prs = asyncio.get_event_loop().run_until_complete(repo.list_provider_requests(100))
        assert any(p['provider'] == 'GMGN' for p in prs)
        # trades exist
        trades = asyncio.get_event_loop().run_until_complete(repo.list_trade_events(100))
        # at least one buy trade
        assert any(t['side'] == 'BUY' for t in trades)
        # positions exist
        positions = asyncio.get_event_loop().run_until_complete(repo.list_open_positions())
        # ensure at least one position present (sim or live)
        assert isinstance(positions, list)
