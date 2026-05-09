from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


@router.get("")
async def list_tokens(request: Request):
    repo = request.app.state.repo
    return await repo.list_tokens(100) or []


@router.get("/{mint}")
async def get_token(mint: str, request: Request):
    repo = request.app.state.repo
    t = await repo.get_token(mint)
    return t or {}


@router.get("/{mint}/snapshots")
async def token_snapshots(mint: str, request: Request):
    repo = request.app.state.repo
    return await repo.list_token_metric_snapshots(mint, 50) or []


@router.get("/{mint}/decisions")
async def token_decisions(mint: str, request: Request):
    repo = request.app.state.repo
    return await repo.list_strategy_matches_by_token(mint, 50) or []
