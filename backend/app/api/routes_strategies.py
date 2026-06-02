from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..config import settings

router = APIRouter(prefix="/api/config", tags=["config"])


class StrategyCreate(BaseModel):
    name: str
    is_live: bool = False
    x: Optional[float] = None
    priority: int = 100
    raw_config_json: str = "{}"


class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    is_live: Optional[bool] = None
    x: Optional[float] = None
    priority: Optional[int] = None
    raw_config_json: Optional[str] = None


@router.get("/strategies")
async def list_strategies(request: Request):
    repo = request.app.state.repo
    return await repo.list_strategy_groups()


@router.post("/strategies")
async def create_strategy(body: StrategyCreate, request: Request):
    repo = request.app.state.repo
    x_val = body.x if body.x is not None else settings.STRATEGY_DEFAULT_X
    if body.is_live:
        live = await repo.get_live_strategy_groups()
        if live:
            raise HTTPException(400, "实盘策略只能保留一条。请先禁用现有实盘策略后再创建新的。")
    sid = await repo.create_strategy_group(
        name=body.name, x=x_val,
        is_live=body.is_live, priority=body.priority, raw_config_json=body.raw_config_json
    )
    return {"id": sid, "status": "created"}


@router.put("/strategies/{strategy_id}")
async def update_strategy(strategy_id: int, body: StrategyUpdate, request: Request):
    repo = request.app.state.repo
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")
    turning_live = body.is_live is True
    if turning_live:
        live = await repo.get_live_strategy_groups()
        other_live = [g for g in live if int(g.get("id", 0)) != strategy_id]
        if other_live:
            raise HTTPException(400, "实盘策略只能保留一条。请先禁用现有实盘策略后再启用新的。")
    await repo.update_strategy_group(strategy_id, updates)
    return {"status": "updated"}


@router.post("/apply")
async def apply_config(request: Request):
    repo = request.app.state.repo
    await repo.append_system_event("INFO", "CONFIG", "Configuration applied", None)
    return {"status": "applied"}


@router.post("/pause-new-entries")
async def pause_new_entries(request: Request):
    repo = request.app.state.repo
    await repo.append_system_event("WARN", "CONFIG", "New entries PAUSED", None)
    request.app.state.pause_new_entries = True
    return {"status": "paused"}


@router.post("/resume-new-entries")
async def resume_new_entries(request: Request):
    repo = request.app.state.repo
    await repo.append_system_event("INFO", "CONFIG", "New entries RESUMED", None)
    request.app.state.pause_new_entries = False
    return {"status": "resumed"}
