from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get("")
async def list_trades(request: Request, limit: int = 100):
    repo = request.app.state.repo
    return await repo.list_trade_events(limit) or []


@router.get("/provider-requests")
async def list_provider_requests(request: Request, limit: int = 100):
    repo = request.app.state.repo
    return await repo.list_provider_requests(limit) or []
