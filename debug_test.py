import asyncio
import os
from backend.app.db.repositories import Repositories
from backend.app.db.database import init_db

async def test():
    # Create fresh DB
    db_path = 'test_debug.sqlite3'
    if os.path.exists(db_path):
        os.remove(db_path)
    
    repo = await Repositories.create(db_path)
    print('DB created')
    
    # Check tables
    cursor = await repo.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = await cursor.fetchall()
    table_names = [t[0] for t in tables]
    print(f'Tables: {table_names}')
    
    # Try to create discovery event
    try:
        event_id = await repo.create_discovery_event(
            token_mint='TEST',
            source_snapshot_id=1
        )
        print(f'Created discovery event: {event_id}')
        
        # Check if it exists
        event = await repo.get_discovery_event(event_id)
        print(f'Got event: {event}')
        
        # Check latest event for token
        latest = await repo.get_latest_discovery_event_for_token('TEST')
        print(f'Latest event: {latest}')
        
    except Exception as e:
        print(f'Error creating discovery event: {e}')
    
    await repo.close()
    if os.path.exists(db_path):
        os.remove(db_path)

asyncio.run(test())
