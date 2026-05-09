from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.get("")
async def list_positions(request: Request, status: str = "all"):
    repo = request.app.state.repo
    if status == "all":
        return await repo.list_all_positions(100) or []
    return await repo.list_open_positions() or []


@router.get("/{position_id}")
async def get_position(position_id: int, request: Request):
    repo = request.app.state.repo
    p = await repo.get_position(position_id)
    return p or {}


@router.post("/{position_id}/manual-close")
async def manual_close(position_id: int, request: Request):
    repo = request.app.state.repo
    await repo.close_position(position_id, close_reason="MANUAL_CLOSE")
    await repo.append_system_event("WARN", "POSITION", "Manual close", str({"position_id": position_id}))
    return {"status": "closed", "position_id": position_id}
