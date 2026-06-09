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
        tokens = asyncio.run(repo.list_tokens(10))
        assert isinstance(tokens, list)
        # provider requests logged
        prs = asyncio.run(repo.list_provider_requests(100))
        assert any(p['provider'] == 'GMGN' for p in prs)
        # Strategy thresholds may reject all mock tokens; lifecycle success is
        # verified by provider requests and discovery completion events.
        trades = asyncio.run(repo.list_trade_events(100))
        all_positions = asyncio.run(repo.list_all_positions(100))
        events = asyncio.run(repo.list_recent_system_events(100))
        assert any(e.get("message") == "Discovery run complete" for e in events)
        assert isinstance(all_positions, list)
