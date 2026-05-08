from fastapi import APIRouter, Request
from ..db.repositories import Repositories

router = APIRouter()

@router.get('/api/logs/recent')
async def recent_logs(request: Request, limit: int = 100):
    repo = request.app.state.repo
    return await repo.list_recent_system_events(limit)
