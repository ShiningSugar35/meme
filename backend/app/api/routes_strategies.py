from fastapi import APIRouter, Depends, Request
from ..db.repositories import Repositories

router = APIRouter()

@router.get('/api/strategies')
async def list_strategies(request: Request):
    repo = request.app.state.repo
    return await repo.list_strategy_groups()
