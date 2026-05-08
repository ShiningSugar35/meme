from fastapi import APIRouter, Request
from ..db.repositories import Repositories

router = APIRouter()

@router.get('/api/positions')
async def list_positions(request: Request):
    repo = request.app.state.repo
    return await repo.list_open_positions()

@router.get('/api/positions/{id}')
async def get_position(id: int, request: Request):
    repo = request.app.state.repo
    return await repo.get_position(id)
