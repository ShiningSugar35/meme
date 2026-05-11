"""Runtime mode, workers, portfolio, and emergency API routes."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from ..config import settings, ProviderMode
from ..logging_config import logger

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


class ModeSwitchRequest(BaseModel):
    user_mode: str  # SIM_TEST or FORMAL_SIM_LIVE


# Runtime status
@router.get("/status")
async def runtime_status(request: Request):
    repo = request.app.state.repo
    settings_dict = await repo.get_all_runtime_settings()
    mode = settings.get_provider_mode()

    user_mode = settings_dict.get('user_mode', 'SIM_TEST')
    workers_enabled = settings_dict.get('workers_enabled', 'false') == 'true'
    live_entries_enabled = settings_dict.get('live_entries_enabled', 'false') == 'true'

    # Live safety check
    live_checks = _check_live_readiness()

    # Kill switch
    pause_new = getattr(request.app.state, 'pause_new_entries', False)

    return JSONResponse({
        'user_mode': user_mode,
        'workers_enabled': workers_enabled,
        'live_entries_enabled': live_entries_enabled,
        'provider_mode': mode.value,
        'pause_new_entries': pause_new,
        'live_readiness': live_checks,
        'can_live_trade': live_checks.get('ready', False),
    })


# Switch user mode
@router.post("/mode")
async def switch_mode(body: ModeSwitchRequest, request: Request):
    repo = request.app.state.repo
    new_mode = body.user_mode

    if new_mode not in ('SIM_TEST', 'FORMAL_SIM_LIVE'):
        return JSONResponse({'ok': False, 'error': f'Invalid mode: {new_mode}. Must be SIM_TEST or FORMAL_SIM_LIVE'}, status_code=400)

    if new_mode == 'FORMAL_SIM_LIVE':
        checks = _check_live_readiness()
        if not checks.get('ready'):
            return JSONResponse({
                'ok': False,
                'error': 'Live mode not ready',
                'missing': checks.get('missing', []),
                'checks': checks,
            }, status_code=400)

    await repo.set_runtime_setting('user_mode', new_mode, 'frontend')
    await repo.append_system_event('INFO', 'RUNTIME', f'User mode switched to {new_mode}', None, account_type='SIM')

    return JSONResponse({'ok': True, 'user_mode': new_mode})


# Workers control
@router.post("/workers/start")
async def start_workers(request: Request):
    repo = request.app.state.repo
    worker_mgr = request.app.state.worker_manager
    await worker_mgr.start_all()
    await repo.set_runtime_setting('workers_enabled', 'true', 'frontend')
    await repo.append_system_event('INFO', 'WORKER', 'All workers started', None, account_type='SIM')
    return JSONResponse({'ok': True, 'status': 'started'})


@router.post("/workers/stop")
async def stop_workers(request: Request):
    repo = request.app.state.repo
    worker_mgr = request.app.state.worker_manager
    await worker_mgr.stop_all()
    await repo.set_runtime_setting('workers_enabled', 'false', 'frontend')
    await repo.append_system_event('INFO', 'WORKER', 'All workers stopped', None, account_type='SIM')
    return JSONResponse({'ok': True, 'status': 'stopped'})


# Workers status
@router.get("/workers/status")
async def workers_status(request: Request):
    worker_mgr = getattr(request.app.state, 'worker_manager', None)
    if not worker_mgr:
        return JSONResponse({'error': 'Worker manager not initialized'}, status_code=503)
    return JSONResponse(worker_mgr.get_status())


# Portfolio table
@router.get("/portfolio/table")
async def portfolio_table(request: Request, account_type: str = 'LIVE'):
    repo = request.app.state.repo
    positions = await repo.list_positions_for_portfolio(account_type, 100)
    result = []
    for p in positions:
        mint = p.get('token_mint', '')
        result.append({
            'id': p['id'],
            'status': p.get('status', 'UNKNOWN'),
            'entry_usd': p.get('entry_price_usd', 0),
            'remaining': p.get('remaining_value_usd', 0),
            'price': p.get('entry_price_usd', 0),
            'liquidity': None,
            'pnl_pct': p.get('pnl_pct') or p.get('realized_pnl_pct'),
            'market_cap': None,
            'token_symbol': None,
            'mint_short': mint[:8] + '..' + mint[-4:] if len(mint) > 12 else mint,
            'mint': mint,
            'account_type': p.get('account_type', account_type),
            'updated_at': p.get('updated_at', p.get('opened_at', '')),
        })
    return JSONResponse(result)


# Positions summary
@router.get("/positions/summary")
async def positions_summary(request: Request):
    repo = request.app.state.repo
    summary = await repo.get_positions_summary()
    return JSONResponse(summary)


# Emergency endpoints
@router.post("/emergency/kill-switch")
async def toggle_kill_switch(request: Request, enable: bool = True):
    repo = request.app.state.repo
    request.app.state.pause_new_entries = enable
    await repo.append_system_event(
        'WARN' if enable else 'INFO', 'EMERGENCY',
        f'Kill switch {"ON" if enable else "OFF"}',
        None, account_type='SIM'
    )
    return JSONResponse({'ok': True, 'kill_switch_active': enable})


@router.post("/emergency/pause-new-live-entries")
async def pause_new_live_entries(request: Request):
    repo = request.app.state.repo
    request.app.state.pause_new_entries = True
    await repo.set_runtime_setting('live_entries_enabled', 'false', 'frontend')
    await repo.append_system_event('WARN', 'EMERGENCY', 'New live entries PAUSED', None, account_type='SIM')
    return JSONResponse({'ok': True, 'status': 'paused'})


@router.post("/emergency/resume-new-live-entries")
async def resume_new_live_entries(request: Request):
    repo = request.app.state.repo
    request.app.state.pause_new_entries = False
    await repo.set_runtime_setting('live_entries_enabled', 'true', 'frontend')
    await repo.append_system_event('INFO', 'EMERGENCY', 'New live entries RESUMED', None, account_type='SIM')
    return JSONResponse({'ok': True, 'status': 'resumed'})


@router.post("/emergency/backup-db")
async def backup_db(request: Request):
    import shutil
    from pathlib import Path
    from datetime import datetime, timezone

    src = Path(settings.SQLITE_PATH)
    if not src.exists():
        return JSONResponse({'ok': False, 'error': 'DB file not found'}, status_code=404)

    backup_dir = Path('data_backup')
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    dst = backup_dir / f"trading_bot_backup_{ts}.sqlite3"

    if src.exists():
        import aiosqlite
        src_db = await aiosqlite.connect(str(src))
        dst_db = await aiosqlite.connect(str(dst))
        await src_db.backup(dst_db)
        await src_db.close()
        await dst_db.close()

    return JSONResponse({'ok': True, 'backup_path': str(dst)})


@router.post("/emergency/repair-legacy-db")
async def repair_legacy_db(request: Request):
    repo = request.app.state.repo
    repaired = 0

    cursor = await repo.db.execute(
        "SELECT id, locked_strategy_config_json FROM positions WHERE legacy_config_status = 'LEGACY_INVALID_CONFIG'"
    )
    rows = await cursor.fetchall()
    for row in rows:
        pos_id = row[0]
        await repo.mark_position_legacy_config(pos_id, 'LEGACY_INVALID_CONFIG')
        repaired += 1

    await repo.append_system_event('INFO', 'EMERGENCY', f'Legacy DB repair complete',
        str({'positions_marked': repaired}), account_type='SIM')
    return JSONResponse({'ok': True, 'repaired_count': repaired})


def _check_live_readiness() -> dict:
    missing = []
    checks = {}

    checks['DRY_RUN'] = settings.DRY_RUN
    checks['LIVE_TRADING_ENABLED'] = settings.LIVE_TRADING_ENABLED
    checks['JITO_ENABLED'] = settings.JITO_ENABLED
    checks['WALLET_PUBLIC_KEY'] = bool(settings.WALLET_PUBLIC_KEY)
    checks['WALLET_PRIVATE_KEY_BASE58'] = bool(settings.WALLET_PRIVATE_KEY_BASE58)
    checks['GMGN_API_KEY'] = bool(settings.get_gmgn_api_key())
    checks['JUPITER_API_KEY'] = bool(settings.get_jupiter_api_key())
    checks['RPC_URL'] = bool(settings.get_rpc_http_url())

    if settings.DRY_RUN:
        missing.append('DRY_RUN=true - must be false for live trading')
    if not settings.LIVE_TRADING_ENABLED:
        missing.append('LIVE_TRADING_ENABLED=false')
    if not settings.JITO_ENABLED:
        missing.append('JITO_ENABLED=false')
    if not settings.WALLET_PUBLIC_KEY:
        missing.append('WALLET_PUBLIC_KEY missing')
    if not settings.WALLET_PRIVATE_KEY_BASE58:
        missing.append('WALLET_PRIVATE_KEY_BASE58 missing')
    if not settings.get_gmgn_api_key():
        missing.append('GMGN_API_KEY missing')
    if not settings.get_jupiter_api_key():
        missing.append('JUPITER_API_KEY missing')
    if not settings.get_rpc_http_url():
        missing.append('RPC_URL missing')

    checks['missing'] = missing
    checks['ready'] = len(missing) == 0
    return checks
