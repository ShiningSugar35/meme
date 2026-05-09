import json
import asyncio
from fastapi import APIRouter, Request
from ..db.repositories import Repositories
from ..services.event_bus import event_bus

router = APIRouter()


@router.get('/api/logs/recent')
async def recent_logs(request: Request, limit: int = 100):
    repo = request.app.state.repo
    return await repo.list_recent_system_events(limit)


@router.get('/api/logs/stream')
async def logs_stream(request: Request):
    queue = await event_bus.subscribe('system')

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"event: system_event\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield f"event: ping\ndata: {json.dumps({'type': 'ping'})}\n\n"
        finally:
            await event_bus.unsubscribe('system', queue)

    from starlette.responses import StreamingResponse
    return StreamingResponse(
        event_generator(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )
