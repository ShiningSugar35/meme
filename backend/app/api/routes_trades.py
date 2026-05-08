from fastapi import APIRouter, Request
from ..db.repositories import Repositories

router = APIRouter()

@router.get('/api/trades')
async def list_trades(request: Request):
    repo = request.app.state.repo
    return await repo.list_trade_events(100)
