import pytest
import asyncio
from pathlib import Path

from ..db.repositories import Repositories


@pytest.mark.asyncio
async def test_connection_open_close_many_times(tmp_path):
    db_file = tmp_path / "many.sqlite3"
    # create/close the connection repeatedly to ensure no resource leak
    for i in range(20):
        repo = await Repositories.create(str(db_file))
        # simple write
        await repo.append_system_event('INFO', 'TEST', f'cycle-{i}', '{}')
        # close cleanly
        await repo.close()


@pytest.mark.asyncio
async def test_isolated_db_per_test(tmp_path):
    db_a = tmp_path / "a.sqlite3"
    db_b = tmp_path / "b.sqlite3"

    repo_a = await Repositories.create(str(db_a))
    events_a = await repo_a.list_recent_system_events()
    assert len(events_a) == 0
    await repo_a.append_system_event('INFO', 'TEST', 'a1', '{}')
    await repo_a.close()

    repo_b = await Repositories.create(str(db_b))
    events_b = await repo_b.list_recent_system_events()
    # repo_b should be a fresh DB file
    assert len(events_b) == 0
    await repo_b.close()
