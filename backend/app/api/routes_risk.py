from fastapi import APIRouter, Request
from ..db.repositories import Repositories

router = APIRouter()

@router.get('/api/risk/kill-switch')
async def kill_switch_status(request: Request):
    repo = request.app.state.repo
    # return pause state from KillSwitchRunner? For now return static.
    return {'pause_new_entries': False}
