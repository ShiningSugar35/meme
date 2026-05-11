import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from ..services.event_bus import event_bus
from ..config import settings

router = APIRouter()


@router.get('/api/logs/recent')
async def recent_logs(request: Request, limit: int = 100, level: str = '', category: str = ''):
    repo = request.app.state.repo
    lvl = level if level else None
    cat = category if category else None
    return await repo.list_recent_system_events(limit, level=lvl, category=cat)


@router.get('/api/logs/stream')
async def logs_stream(request: Request):
    queue = await event_bus.subscribe('system')

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"event: system_event\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield f"event: ping\ndata: {json.dumps({'type': 'ping'})}\n\n"
        finally:
            await event_bus.unsubscribe('system', queue)

    from starlette.responses import StreamingResponse
    return StreamingResponse(
        event_generator(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )


@router.post('/api/logs/export-diagnostic')
async def export_diagnostic(request: Request):
    """Generate a diagnostic markdown file for AI debugging."""
    repo = request.app.state.repo
    worker_mgr = getattr(request.app.state, 'worker_manager', None)
    now = datetime.now(timezone.utc)
    ts = now.strftime('%Y%m%d_%H%M')

    logs_dir = Path('logs')
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_path = logs_dir / f'diagnostic_{ts}.md'

    lines = []
    def w(s=''):
        lines.append(s)

    w(f'# Diagnostic Report — {now.isoformat()[:19]}')
    w()
    w('## 1. Runtime')
    rsettings = await repo.get_all_runtime_settings()
    w(f'- user_mode: {rsettings.get("user_mode", "?")}')
    w(f'- workers_enabled: {rsettings.get("workers_enabled", "?")}')
    w(f'- provider_mode: {settings.get_provider_mode().value}')
    w(f'- backend: 127.0.0.1:8000  frontend: localhost:5173')
    try:
        import sys
        w(f'- python: {sys.version.split()[0]}')
    except Exception:
        pass

    w()
    w('## 2. Config Summary')
    w(f'- DRY_RUN: {settings.DRY_RUN}')
    w(f'- JITO_ENABLED: {settings.JITO_ENABLED}')
    w(f'- WALLET_PUBLIC_KEY: {"SET" if settings.WALLET_PUBLIC_KEY else "MISSING"}')
    w(f'- WALLET_PRIVATE_KEY: {"SET" if settings.WALLET_PRIVATE_KEY_BASE58 else "MISSING"}')
    w(f'- GMGN_API_KEY: {"SET" if settings.get_gmgn_api_key() else "MISSING"}')
    w(f'- JUPITER_API_KEY: {"SET" if settings.get_jupiter_api_key() else "MISSING"}')
    w(f'- RPC_URL: {"SET" if settings.get_rpc_http_url() else "MISSING"}')

    w()
    w('## 3. Worker Summary')
    if worker_mgr:
        status = worker_mgr.get_status()
        for name, s in status.items():
            w(f'- {name}: running={s["running"]} processed={s["processed_count"]} last_error={s.get("last_error") or "none"}')
    else:
        w('- Worker manager not available')

    w()
    w('## 4. DB Health')
    w(f'- path: {settings.SQLITE_PATH}')
    async with repo.db.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    w(f'- journal_mode: {row[0] if row else "?"}')
    async with repo.db.execute("PRAGMA busy_timeout") as cur:
        row = await cur.fetchone()
    w(f'- busy_timeout: {row[0] if row else "?"}')
    for table in ['positions', 'trade_events', 'system_events', 'provider_requests', 'tick_snapshots']:
        async with repo.db.execute(f"SELECT COUNT(*) as c FROM {table}") as cur:
            row = await cur.fetchone()
        w(f'- {table}: {row[0] if row else 0} rows')

    w()
    w('## 5. Trading Summary')
    async with repo.db.execute("SELECT COUNT(*) FROM positions WHERE account_type='LIVE' AND status NOT IN ('CLOSED')") as cur:
        row = await cur.fetchone()
    live_open = row[0] if row else 0
    async with repo.db.execute("SELECT COUNT(*) FROM positions WHERE account_type='LIVE' AND status='CLOSED'") as cur:
        row = await cur.fetchone()
    live_closed = row[0] if row else 0
    async with repo.db.execute("SELECT COUNT(*) FROM positions WHERE account_type='SIM' AND status NOT IN ('CLOSED')") as cur:
        row = await cur.fetchone()
    sim_open = row[0] if row else 0
    async with repo.db.execute("SELECT COUNT(*) FROM positions WHERE account_type='SIM' AND status='CLOSED'") as cur:
        row = await cur.fetchone()
    sim_closed = row[0] if row else 0
    w(f'- LIVE open={live_open} closed={live_closed}')
    w(f'- SIM  open={sim_open} closed={sim_closed}')

    # Anomaly check: SELL without BUY
    async with repo.db.execute(
        "SELECT COUNT(*) FROM trade_events WHERE side='SELL' AND token_mint NOT IN (SELECT token_mint FROM trade_events WHERE side='BUY')"
    ) as cur:
        row = await cur.fetchone()
    sell_no_buy = row[0] if row else 0
    w(f'- SELL without BUY: {sell_no_buy} (anomaly if >0)')

    w()
    w('## 6. Recent Trade Events (last 20)')
    async with repo.db.execute("SELECT * FROM trade_events ORDER BY id DESC LIMIT 20") as cur:
        rows = await cur.fetchall()
    for r in rows:
        d = dict(r)
        sig = (d.get('tx_signature') or '')[:12]
        w(f'  - id={d["id"]} {d["side"]} {d["event_type"]} {d["status"]} acct={d["account_type"]} sig={sig}')

    w()
    w('## 7. Error Aggregation (last 1h)')
    async with repo.db.execute(
        "SELECT level, category, message, COUNT(*) as cnt, MIN(created_at) as first_seen, MAX(created_at) as last_seen "
        "FROM system_events WHERE (level='ERROR' OR level='WARN') AND created_at > datetime('now','-1 hour') "
        "GROUP BY level, category, message ORDER BY cnt DESC LIMIT 20"
    ) as cur:
        rows = await cur.fetchall()
    for r in rows:
        d = dict(r)
        w(f'  - [{d["level"]}] {d["category"]}: {d["message"][:120]}  (x{d["cnt"]}, {d["first_seen"][:19]}..{d["last_seen"][:19]})')

    w()
    w('## 8. Provider Requests (last 20 errors)')
    async with repo.db.execute(
        "SELECT * FROM provider_requests WHERE ok=0 ORDER BY id DESC LIMIT 20"
    ) as cur:
        rows = await cur.fetchall()
    for r in rows:
        d = dict(r)
        w(f'  - {d["provider"]} {d["endpoint"][:60]} err={d.get("error_code") or d.get("error_summary","?")[:80]}')

    w()
    w('## 9. Recent Errors (last 30)')
    async with repo.db.execute(
        "SELECT level, category, message, created_at FROM system_events WHERE level IN ('ERROR','WARN') ORDER BY id DESC LIMIT 30"
    ) as cur:
        rows = await cur.fetchall()
    for r in rows:
        d = dict(r)
        w(f'  - [{d["level"]}] {d["category"]}: {d["message"][:150]}  ({d["created_at"][:19]})')

    content = '\n'.join(lines)
    out_path.write_text(content, encoding='utf-8')

    file_size = len(content.encode('utf-8'))
    return JSONResponse({
        'ok': True,
        'path': str(out_path.absolute()),
        'size_bytes': file_size,
    })
