from fastapi import APIRouter, Depends, Request
from ..db.repositories import Repositories

router = APIRouter()

@router.get('/api/tokens')
async def list_tokens(request: Request):
    repo = request.app.state.repo
    return await repo.list_tokens(100)

@router.get('/api/tokens/{mint}')
async def get_token(mint: str, request: Request):
    repo = request.app.state.repo
    return await repo.get_token(mint)
