import pytest


@pytest.mark.asyncio
async def test_wal_and_system_events(repo):
    # repo fixture provides an isolated DB and will be closed by fixture
    await repo.append_system_event("INFO", "TEST", "repo init")
    events = await repo.list_recent_system_events(limit=5)
    assert any(e["category"] == "TEST" for e in events)


@pytest.mark.asyncio
async def test_first_seen_upsert_and_no_overwrite(repo):
    await repo.upsert_token_first_seen("MINT1", symbol="TKN")
    t1 = await repo.get_token("MINT1")
    first = t1["first_seen_at"]
    # upsert again with different data
    await repo.upsert_token_first_seen("MINT1", symbol="TKN2")
    t2 = await repo.get_token("MINT1")
    assert t2["first_seen_at"] == first


@pytest.mark.asyncio
async def test_trade_event_idempotency(repo):
    te = await repo.append_trade_event("IDEMP1", token_mint="MINTX", side="BUY", event_type="BUY", status="PENDING")
    te2 = await repo.append_trade_event("IDEMP1", token_mint="MINTX", side="BUY", event_type="BUY", status="PENDING")
    assert te["id"] == te2["id"]


@pytest.mark.asyncio
async def test_provider_requests_and_matches_and_bandit_and_positions(repo):
    # provider requests
    await repo.append_provider_request('GMGN', '/trenches', 'GET', 200, 10, True, None, None, '{"q":1}', '{"r":1}')
    prs = await repo.list_provider_requests(10)
    assert any(p['provider'] == 'GMGN' for p in prs)

    # token strategy matches
    await repo.insert_strategy_match('MINTX', 1, 1, None, 'initial', True, '{}', '{}')
    matches = await repo.list_token_strategy_matches('MINTX')
    assert len(matches) >= 1

    # bandit observations
    await repo.insert_bandit_observation('MINTX', 1, False, '{}', '{}')
    # fetch last observation id via listing
    async with repo.db.execute("SELECT id FROM bandit_observations ORDER BY id DESC LIMIT 1") as cur:
        row = await cur.fetchone()
    if row:
        obs_id = row['id']
        await repo.finalize_bandit_observation(obs_id, '{}', 0.0, 'TEST')

    # positions create/update/close
    pid = await repo.create_position('MINTX', False, '{}', 'OPEN', 1.0, 1.0, 100, 100, 100)
    p = await repo.get_position(pid)
    assert p is not None
    await repo.update_position_remaining(pid, 50, 50)
    await repo.close_position(pid, close_reason='TEST_CLOSE', total_return_sol=0.5)
    closed = await repo.get_position(pid)
    assert closed['status'] == 'CLOSED'
