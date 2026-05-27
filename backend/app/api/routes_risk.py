from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/risk", tags=["risk"])


@router.get("/kill-switch")
async def kill_switch_status(request: Request):
    pause = getattr(request.app.state, 'pause_new_entries', False)
    repo = request.app.state.repo
    try:
        closed = await repo.list_recent_closed_live_positions(10)
        if len(closed) >= 10:
            total_cost = sum(c.get('total_cost_usd', 0) or 0 for c in closed)
            total_return = sum(c.get('total_return_usd', 0) or 0 for c in closed)
            rolling_roi = (total_return / total_cost - 1) if total_cost > 0 else 0
        else:
            rolling_roi = 0
    except Exception:
        rolling_roi = 0
    return {"pause_new_entries": pause, "rolling_10_roi": rolling_roi}


@router.post("/kill-switch/reset")
async def reset_kill_switch(request: Request):
    request.app.state.pause_new_entries = False
    repo = request.app.state.repo
    await repo.append_system_event("INFO", "RISK", "Kill switch reset", None)
    return {"status": "reset"}
