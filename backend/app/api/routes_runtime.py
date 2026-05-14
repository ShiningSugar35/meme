"""Runtime mode, portfolio, trading-parameter, and emergency API routes.

This module is intentionally self-contained for runtime-facing UI endpoints.  It
uses direct SQL for Strategy Groups and exports so older repository wrappers do
not break when their signatures diverge from the frontend contract.
"""
from __future__ import annotations

import json
import math
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import ProviderMode, settings
from ..logging_config import logger

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    dt = dt or _utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _json_response(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=jsonable_encoder(content), status_code=status_code)


def _safe_json_loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _row_to_dict(row: Any) -> Dict[str, Any]:
    return dict(row) if row is not None else {}


async def _fetch_all(db, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    async with db.execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(row) for row in rows]


async def _fetch_one(db, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
    async with db.execute(sql, tuple(params)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row is not None else None


async def _table_columns(db, table: str) -> List[str]:
    rows = await _fetch_all(db, f"PRAGMA table_info({table})")
    return [str(r.get("name")) for r in rows]


# ---------------------------------------------------------------------------
# Runtime mode
# ---------------------------------------------------------------------------

class ModeSwitchRequest(BaseModel):
    user_mode: str  # SIM_TEST / FORMAL_SIM_LIVE / IDLE


@router.get("/status")
async def runtime_status(request: Request):
    repo = request.app.state.repo
    settings_dict = await repo.get_all_runtime_settings()
    mode = settings.get_provider_mode()

    user_mode = settings_dict.get("user_mode", "IDLE")
    workers_enabled = settings_dict.get("workers_enabled", "false") == "true"
    live_entries_enabled = settings_dict.get("live_entries_enabled", "false") == "true"
    live_checks = _check_live_readiness()
    live_open_count = await _count_open_live_positions(repo.db)

    return _json_response({
        "user_mode": user_mode,
        "workers_enabled": workers_enabled,
        "live_entries_enabled": live_entries_enabled,
        "provider_mode": mode.value,
        "pause_new_entries": getattr(request.app.state, "pause_new_entries", False),
        "session_started_at": getattr(request.app.state, "session_started_at", None),
        "live_open_count": live_open_count,
        "has_live_positions": live_open_count > 0,
        "live_readiness": live_checks,
        "can_live_trade": live_checks.get("ready", False),
    })


@router.post("/mode")
async def switch_mode(body: ModeSwitchRequest, request: Request):
    return await _switch_user_mode(request, body.user_mode, updated_by="frontend")


async def _switch_user_mode(request: Request, new_mode: str, *, updated_by: str) -> JSONResponse:
    repo = request.app.state.repo
    worker_mgr = getattr(request.app.state, "worker_manager", None)

    if new_mode not in ("SIM_TEST", "FORMAL_SIM_LIVE", "IDLE"):
        return _json_response({"ok": False, "error": f"Invalid mode: {new_mode}."}, status_code=400)

    if new_mode == "FORMAL_SIM_LIVE":
        checks = _check_live_readiness()
        if not checks.get("ready"):
            return _json_response({
                "ok": False,
                "error": "Live mode not ready",
                "missing": checks.get("missing", []),
                "checks": checks,
            }, status_code=400)

    await repo.set_runtime_setting("user_mode", new_mode, updated_by)

    if new_mode == "IDLE":
        request.app.state.pause_new_entries = True
        await repo.set_runtime_setting("workers_enabled", "false", updated_by)
        await repo.set_runtime_setting("live_entries_enabled", "false", updated_by)
        if worker_mgr:
            await worker_mgr.stop_all()
    elif new_mode == "SIM_TEST":
        # SIM mode must still allow paper entries; only live entries are disabled.
        request.app.state.pause_new_entries = False
        await repo.set_runtime_setting("live_entries_enabled", "false", updated_by)
        if worker_mgr:
            await worker_mgr.start_all()
        await repo.set_runtime_setting("workers_enabled", "true", updated_by)
    elif new_mode == "FORMAL_SIM_LIVE":
        request.app.state.pause_new_entries = False
        await repo.set_runtime_setting("live_entries_enabled", "true", updated_by)
        if worker_mgr:
            await worker_mgr.start_all()
        await repo.set_runtime_setting("workers_enabled", "true", updated_by)

    await repo.append_system_event("INFO", "RUNTIME", f"User mode switched to {new_mode}", None, account_type="SIM")
    return _json_response({"ok": True, "user_mode": new_mode})


@router.get("/workers/status")
async def workers_status(request: Request):
    worker_mgr = getattr(request.app.state, "worker_manager", None)
    if not worker_mgr:
        return _json_response({"error": "Worker manager not initialized"}, status_code=503)
    return _json_response(worker_mgr.get_status())


# The worker APIs are retained for backwards compatibility, but the current UI no
# longer exposes manual worker controls.
@router.post("/workers/start")
async def start_workers(request: Request):
    worker_mgr = getattr(request.app.state, "worker_manager", None)
    if not worker_mgr:
        return _json_response({"ok": False, "error": "Worker manager not initialized"}, status_code=503)
    await worker_mgr.start_all()
    await request.app.state.repo.set_runtime_setting("workers_enabled", "true", "runtime_api")
    return _json_response({"ok": True})


@router.post("/workers/stop")
async def stop_workers(request: Request):
    worker_mgr = getattr(request.app.state, "worker_manager", None)
    if not worker_mgr:
        return _json_response({"ok": False, "error": "Worker manager not initialized"}, status_code=503)
    await worker_mgr.stop_all()
    await request.app.state.repo.set_runtime_setting("workers_enabled", "false", "runtime_api")
    return _json_response({"ok": True})


# ---------------------------------------------------------------------------
# Trading parameters: runtime editable config formerly held in .env
# ---------------------------------------------------------------------------

class TradingParamSpec(BaseModel):
    key: str
    label: str
    description: str
    value_type: str = Field(pattern="^(int|float)$")
    default: float | int
    min_value: Optional[float] = None


TRADING_PARAM_SPECS: List[TradingParamSpec] = [
    TradingParamSpec(key="POLL_INTERVAL_SECONDS", label="池子轮询间隔", description="每隔多少秒拉取一次 trenches 新池列表。", value_type="int", default=60, min_value=1),
    TradingParamSpec(key="ACTIVE_POSITION_PRICE_POLL_SECONDS", label="持仓价格轮询", description="持仓价格面刷新间隔，越小越灵敏但资源消耗越高。", value_type="int", default=1, min_value=1),
    TradingParamSpec(key="TIP_FLOOR_REFRESH_SECONDS", label="Jito 小费刷新", description="刷新 Jito tip floor 的时间间隔。", value_type="int", default=3, min_value=1),
    TradingParamSpec(key="BUY_SLIPPAGE_CAP_BPS", label="买入滑点上限", description="买入报价允许的最大滑点，单位 bps。", value_type="int", default=1500, min_value=0),
    TradingParamSpec(key="SELL_SLIPPAGE_CAP_BPS", label="卖出滑点上限", description="普通卖出报价允许的最大滑点，单位 bps。", value_type="int", default=2000, min_value=0),
    TradingParamSpec(key="EMERGENCY_SLIPPAGE_CAP_BPS", label="紧急卖出滑点上限", description="风控或一键卖出时允许的最大滑点，单位 bps。", value_type="int", default=3500, min_value=0),
    TradingParamSpec(key="PRICE_IMPACT_HARD_CAP_PCT", label="价格冲击硬上限", description="报价 priceImpactPct 超过该百分比时禁止成交。", value_type="float", default=10.0, min_value=0),
    TradingParamSpec(key="LIVE_ROLLING_10_LOSS_LIMIT", label="实盘近10笔亏损阈值", description="近10笔实盘滚动收益低于该值时触发保护逻辑。", value_type="float", default=-0.20),
    TradingParamSpec(key="MAX_REQUOTE_RETRY", label="最大重新报价次数", description="报价失败或过期时最多重新请求报价的次数。", value_type="int", default=2, min_value=0),
    TradingParamSpec(key="ENTRY_SIZE_LIQUIDITY_PCT", label="入场占流动性比例", description="入场金额按流动性乘以该比例计算。", value_type="float", default=0.015, min_value=0),
    TradingParamSpec(key="ENTRY_MAX_USD", label="单笔最大入场金额", description="单笔入场金额美元上限；模拟盘仍为 min(流动性比例,$上限)。", value_type="float", default=200.0, min_value=0),
    TradingParamSpec(key="RISK_FEATURE_SCAN_TIER_1_USD", label="风控扫描档1金额", description="持仓金额不低于该值时使用档1频率。", value_type="float", default=150.0, min_value=0),
    TradingParamSpec(key="RISK_FEATURE_SCAN_TIER_1_SECONDS", label="风控扫描档1秒数", description="档1持仓风控重扫间隔。", value_type="int", default=2, min_value=0),
    TradingParamSpec(key="RISK_FEATURE_SCAN_TIER_2_USD", label="风控扫描档2金额", description="持仓金额不低于该值时使用档2频率。", value_type="float", default=100.0, min_value=0),
    TradingParamSpec(key="RISK_FEATURE_SCAN_TIER_2_SECONDS", label="风控扫描档2秒数", description="档2持仓风控重扫间隔。", value_type="int", default=3, min_value=0),
    TradingParamSpec(key="RISK_FEATURE_SCAN_TIER_3_USD", label="风控扫描档3金额", description="持仓金额不低于该值时使用档3频率。", value_type="float", default=50.0, min_value=0),
    TradingParamSpec(key="RISK_FEATURE_SCAN_TIER_3_SECONDS", label="风控扫描档3秒数", description="档3持仓风控重扫间隔。", value_type="int", default=6, min_value=0),
    TradingParamSpec(key="RISK_FEATURE_SCAN_TIER_4_USD", label="风控扫描档4金额", description="持仓金额不低于该值时使用档4频率。", value_type="float", default=25.0, min_value=0),
    TradingParamSpec(key="RISK_FEATURE_SCAN_TIER_4_SECONDS", label="风控扫描档4秒数", description="档4持仓风控重扫间隔。", value_type="int", default=12, min_value=0),
    TradingParamSpec(key="RISK_FEATURE_SCAN_TIER_5_SECONDS", label="风控扫描档5秒数", description="低于档4金额时的风控重扫间隔。", value_type="int", default=24, min_value=0),
    TradingParamSpec(key="TOP1_HOLDER_SCAN_TIER_1_USD", label="Top1扫描档1金额", description="持仓金额不低于该值时使用档1 Top1 扫描频率。", value_type="float", default=150.0, min_value=0),
    TradingParamSpec(key="TOP1_HOLDER_SCAN_TIER_1_SECONDS", label="Top1扫描档1秒数", description="档1 Top1 addr_type=0 持有人扫描间隔。", value_type="int", default=10, min_value=0),
    TradingParamSpec(key="TOP1_HOLDER_SCAN_TIER_2_USD", label="Top1扫描档2金额", description="持仓金额不低于该值时使用档2 Top1 扫描频率。", value_type="float", default=100.0, min_value=0),
    TradingParamSpec(key="TOP1_HOLDER_SCAN_TIER_2_SECONDS", label="Top1扫描档2秒数", description="档2 Top1 addr_type=0 持有人扫描间隔。", value_type="int", default=15, min_value=0),
    TradingParamSpec(key="TOP1_HOLDER_SCAN_TIER_3_USD", label="Top1扫描档3金额", description="持仓金额不低于该值时使用档3 Top1 扫描频率。", value_type="float", default=50.0, min_value=0),
    TradingParamSpec(key="TOP1_HOLDER_SCAN_TIER_3_SECONDS", label="Top1扫描档3秒数", description="档3 Top1 addr_type=0 持有人扫描间隔。", value_type="int", default=30, min_value=0),
    TradingParamSpec(key="TOP1_HOLDER_SCAN_TIER_4_USD", label="Top1扫描档4金额", description="持仓金额不低于该值时使用档4 Top1 扫描频率。", value_type="float", default=25.0, min_value=0),
    TradingParamSpec(key="TOP1_HOLDER_SCAN_TIER_4_SECONDS", label="Top1扫描档4秒数", description="档4 Top1 addr_type=0 持有人扫描间隔。", value_type="int", default=60, min_value=0),
    TradingParamSpec(key="TOP1_HOLDER_SCAN_TIER_5_SECONDS", label="Top1扫描档5秒数", description="低于档4金额时的 Top1 扫描间隔；0 表示不扫描。", value_type="int", default=0, min_value=0),
    TradingParamSpec(key="DUST_FORCE_EXIT_USD", label="尘埃仓位强制退出", description="持仓美元价值低于该值时下一次检查强制全卖。", value_type="float", default=12.5, min_value=0),
    TradingParamSpec(key="DUST_FORCE_EXIT_SOL", label="尘埃仓位SOL兼容值", description="旧调用兼容字段，仅在缺少美元价值时作为兜底。", value_type="float", default=0.15, min_value=0),
]

TRADING_PARAM_INDEX = {spec.key: spec for spec in TRADING_PARAM_SPECS}


class TradingParamsRequest(BaseModel):
    values: Dict[str, float | int]


async def _load_trading_params(repo) -> Dict[str, float | int]:
    rows = await _fetch_all(
        repo.db,
        "SELECT key, value FROM runtime_settings WHERE key LIKE 'trading.%'",
    )
    stored = {str(r["key"])[len("trading."):]: r["value"] for r in rows}
    result: Dict[str, float | int] = {}
    for spec in TRADING_PARAM_SPECS:
        raw = stored.get(spec.key, getattr(settings, spec.key, spec.default))
        result[spec.key] = _coerce_param_value(spec, raw)
    _apply_trading_params_to_settings(result)
    return result


def _coerce_param_value(spec: TradingParamSpec, value: Any) -> float | int:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = float(spec.default)
    if spec.min_value is not None:
        v = max(v, float(spec.min_value))
    return int(v) if spec.value_type == "int" else float(v)


def _apply_trading_params_to_settings(values: Dict[str, float | int]) -> None:
    for key, value in values.items():
        if key in TRADING_PARAM_INDEX:
            setattr(settings, key, value)


async def _apply_worker_intervals(request: Request, values: Dict[str, float | int]) -> None:
    worker_mgr = getattr(request.app.state, "worker_manager", None)
    if not worker_mgr or not hasattr(worker_mgr, "update_interval"):
        return
    if "POLL_INTERVAL_SECONDS" in values:
        worker_mgr.update_interval("discovery", int(values["POLL_INTERVAL_SECONDS"]))
    if "ACTIVE_POSITION_PRICE_POLL_SECONDS" in values:
        worker_mgr.update_interval("price_monitor", int(values["ACTIVE_POSITION_PRICE_POLL_SECONDS"]))


@router.get("/trading-params")
async def get_trading_params(request: Request):
    values = await _load_trading_params(request.app.state.repo)
    return _json_response({
        "specs": [spec.model_dump() for spec in TRADING_PARAM_SPECS],
        "values": values,
    })


@router.put("/trading-params")
async def update_trading_params(payload: TradingParamsRequest, request: Request):
    repo = request.app.state.repo
    values: Dict[str, float | int] = {}
    for key, raw in payload.values.items():
        spec = TRADING_PARAM_INDEX.get(key)
        if not spec:
            return _json_response({"ok": False, "error": f"Unknown trading parameter: {key}"}, status_code=400)
        values[key] = _coerce_param_value(spec, raw)

    all_values = await _load_trading_params(repo)
    all_values.update(values)
    _apply_trading_params_to_settings(all_values)
    await _apply_worker_intervals(request, all_values)

    for key, value in values.items():
        await repo.set_runtime_setting(f"trading.{key}", str(value), "frontend")

    await repo.append_system_event(
        "INFO",
        "RUNTIME_CONFIG",
        "Trading parameters updated",
        _safe_json_dumps(values),
        account_type="SIM",
    )
    return _json_response({"ok": True, "values": all_values})


# ---------------------------------------------------------------------------
# Portfolio / PnL
# ---------------------------------------------------------------------------

@router.get("/portfolio/table")
async def portfolio_table(request: Request, account_type: str = "LIVE"):
    repo = request.app.state.repo
    account = str(account_type or "LIVE").upper()
    positions = await repo.list_positions_for_portfolio(account, 200)
    result = []
    for p in positions:
        mint = p.get("token_mint", "")
        entry_price = p.get("entry_price_usd", 0) or 0
        current_price = p.get("last_fill_price_usd") or p.get("entry_price_usd", 0) or 0
        ratio = (current_price / entry_price) if entry_price and entry_price > 0 else 1.0
        locked = _safe_json_loads(p.get("locked_strategy_config_json"), {}) or {}

        result.append({
            "id": p["id"],
            "status": p.get("status", "UNKNOWN"),
            "ratio": round(ratio, 2),
            "remaining": p.get("remaining_value_usd", 0),
            "remaining_value_usd": p.get("remaining_value_usd", 0),
            "liquidity": None,
            "pnl_pct": p.get("pnl_pct") or p.get("realized_pnl_pct"),
            "market_cap": None,
            "token_symbol": None,
            "mint_short": mint[:8] + ".." + mint[-4:] if len(mint) > 12 else mint,
            "mint": mint,
            "token_mint": mint,
            "account_type": p.get("account_type", account),
            "strategy_id": p.get("live_strategy_id") or locked.get("id") or locked.get("strategy_id"),
            "strategy_name": locked.get("name"),
            "risk_check_interval_seconds": p.get("risk_check_interval_seconds"),
            "last_top1_holder_rate": p.get("last_top1_holder_rate"),
            "updated_at": p.get("updated_at", p.get("opened_at", "")),
        })
    return _json_response(result)


@router.get("/positions/summary")
async def positions_summary(request: Request):
    repo = request.app.state.repo
    try:
        summary = await repo.get_positions_summary()
    except Exception:
        summary = {}
    live_pnl_usd = await _estimate_live_realized_pnl_usd(repo.db)
    summary["total_pnl_usd"] = round(live_pnl_usd, 2)
    summary["live_pnl_usd"] = round(live_pnl_usd, 2)
    return _json_response(summary)


async def _estimate_live_realized_pnl_usd(db) -> float:
    rows = await _fetch_all(
        db,
        """
        SELECT total_cost_sol,total_return_sol,last_fill_price_usd,last_fill_price_sol,entry_price_usd,entry_price_sol
        FROM positions
        WHERE account_type='LIVE' AND status='CLOSED'
        """,
    )
    total = 0.0
    for r in rows:
        pnl_sol = _to_float(r.get("total_return_sol")) - _to_float(r.get("total_cost_sol"))
        sol_usd = 0.0
        if _to_float(r.get("last_fill_price_usd")) > 0 and _to_float(r.get("last_fill_price_sol")) > 0:
            sol_usd = _to_float(r.get("last_fill_price_usd")) / _to_float(r.get("last_fill_price_sol"))
        elif _to_float(r.get("entry_price_usd")) > 0 and _to_float(r.get("entry_price_sol")) > 0:
            sol_usd = _to_float(r.get("entry_price_usd")) / _to_float(r.get("entry_price_sol"))
        if sol_usd > 0:
            total += pnl_sol * sol_usd
    return total


async def _count_open_live_positions(db) -> int:
    row = await _fetch_one(db, "SELECT COUNT(*) AS n FROM positions WHERE account_type='LIVE' AND status!='CLOSED'")
    return int(row.get("n", 0) if row else 0)


# ---------------------------------------------------------------------------
# Emergency actions
# ---------------------------------------------------------------------------

@router.post("/emergency/kill-switch")
async def toggle_kill_switch(request: Request, enable: bool = True):
    # Backward-compatible endpoint.  The current UI uses /sell-all-live instead.
    repo = request.app.state.repo
    request.app.state.pause_new_entries = bool(enable)
    if enable:
        await repo.set_runtime_setting("live_entries_enabled", "false", "emergency")
    await repo.append_system_event(
        "WARN" if enable else "INFO",
        "EMERGENCY",
        f"Kill switch {'ON' if enable else 'OFF'}",
        None,
        account_type="SIM",
    )
    return _json_response({"ok": True, "kill_switch_active": enable})


@router.post("/emergency/sell-all-live")
async def sell_all_live_positions(request: Request):
    """Sell every open LIVE position and then switch the system to SIM mode."""
    repo = request.app.state.repo
    settings_dict = await repo.get_all_runtime_settings()
    user_mode = settings_dict.get("user_mode", "IDLE")
    if user_mode != "FORMAL_SIM_LIVE":
        return _json_response({"ok": False, "error": "系统不在实盘交易状态，不能执行一键卖出。"}, status_code=400)

    positions = await _fetch_all(
        repo.db,
        "SELECT * FROM positions WHERE account_type='LIVE' AND status!='CLOSED' ORDER BY opened_at ASC",
    )
    if not positions:
        return _json_response({"ok": False, "error": "当前没有实盘持仓。"}, status_code=400)

    pipeline = getattr(request.app.state, "trading_pipeline", None)
    if not pipeline or not hasattr(pipeline, "execute_sell"):
        return _json_response({"ok": False, "error": "TradingPipeline not initialized"}, status_code=503)

    results = []
    for position in positions:
        try:
            res = await pipeline.execute_sell(position, exit_pct=1.0, exit_reason="ONE_CLICK_SELL_ALL")
            results.append({"position_id": position.get("id"), "result": res})
        except Exception as e:
            logger.exception("one-click live sell failed", position_id=position.get("id"), error=str(e))
            await repo.append_system_event(
                "ERROR",
                "EMERGENCY",
                "One-click sell failed for a live position",
                _safe_json_dumps({"position_id": position.get("id"), "token": position.get("token_mint"), "error": str(e)}),
                account_type="LIVE",
            )
            results.append({"position_id": position.get("id"), "ok": False, "error": str(e)})

    await _switch_user_mode(request, "SIM_TEST", updated_by="emergency")
    await repo.append_system_event(
        "WARN",
        "EMERGENCY",
        "One-click sell executed; switched to SIM_TEST",
        _safe_json_dumps({"count": len(positions), "results": results}),
        account_type="LIVE",
    )
    return _json_response({"ok": True, "sold_count": len(positions), "results": results, "user_mode": "SIM_TEST"})


@router.post("/emergency/stop-live")
async def stop_live_mode(request: Request):
    repo = request.app.state.repo
    request.app.state.pause_new_entries = False
    await repo.set_runtime_setting("live_entries_enabled", "false", "emergency")
    return await _switch_user_mode(request, "SIM_TEST", updated_by="emergency")


@router.post("/emergency/resume-live")
async def resume_live_mode(request: Request):
    request.app.state.pause_new_entries = False
    return await _switch_user_mode(request, "FORMAL_SIM_LIVE", updated_by="emergency")


@router.post("/emergency/backup-db")
async def backup_db(request: Request):
    """Export only rows created/updated since this backend process started."""
    repo = request.app.state.repo
    session_started_at = getattr(request.app.state, "session_started_at", None) or _iso()
    export_dir = Path("data_backup")
    export_dir.mkdir(parents=True, exist_ok=True)
    ts = _utc_now().strftime("%Y%m%d_%H%M%S")
    dst = export_dir / f"session_backup_{ts}.json"

    async with repo.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'") as cur:
        table_rows = await cur.fetchall()
    tables = [row[0] for row in table_rows]

    payload = {
        "export_type": "session_backup",
        "session_started_at": session_started_at,
        "exported_at": _iso(),
        "tables": {},
    }
    for table in tables:
        cols = await _table_columns(repo.db, table)
        time_cols = [c for c in cols if c in {"created_at", "updated_at", "opened_at", "closed_at", "observed_at", "first_seen_at", "last_fill_at"}]
        if time_cols:
            where = " OR ".join([f"{c} >= ?" for c in time_cols])
            rows = await _fetch_all(repo.db, f"SELECT * FROM {table} WHERE {where}", [session_started_at] * len(time_cols))
        elif table == "runtime_settings":
            rows = await _fetch_all(repo.db, f"SELECT * FROM {table}")
        else:
            rows = []
        payload["tables"][table] = {"count": len(rows), "rows": rows}

    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return _json_response({"ok": True, "export_path": str(dst), "session_started_at": session_started_at})


@router.post("/emergency/export-losing")
async def export_losing_trades(request: Request):
    """Export structured full context for all losing LIVE+SIM trades in this session."""
    repo = request.app.state.repo
    session_started_at = getattr(request.app.state, "session_started_at", None) or "1970-01-01T00:00:00+00:00"
    export_dir = Path("data_backup")
    export_dir.mkdir(parents=True, exist_ok=True)
    ts = _utc_now().strftime("%Y%m%d_%H%M%S")
    dst = export_dir / f"losing_trades_full_{ts}.json"

    positions = await _fetch_all(
        repo.db,
        """
        SELECT * FROM positions
        WHERE status='CLOSED'
          AND opened_at >= ?
          AND (COALESCE(realized_pnl_sol,0) < 0 OR COALESCE(realized_pnl_pct,0) < 0 OR COALESCE(pnl_pct,0) < 0)
        ORDER BY closed_at DESC
        """,
        [session_started_at],
    )

    items = []
    for p in positions:
        token = p.get("token_mint")
        pos_id = p.get("id")
        discovery_id = p.get("discovery_event_id")
        opened_at = p.get("opened_at") or session_started_at
        closed_at = p.get("closed_at") or _iso()
        trades = await _fetch_all(
            repo.db,
            """
            SELECT * FROM trade_events
            WHERE position_id=? OR (token_mint=? AND created_at BETWEEN ? AND ?)
            ORDER BY created_at ASC
            """,
            [pos_id, token, opened_at, closed_at],
        )
        discovery = await _fetch_all(repo.db, "SELECT * FROM discovery_events WHERE id=? OR token_mint=? ORDER BY created_at ASC", [discovery_id, token])
        matches = await _fetch_all(repo.db, "SELECT * FROM token_strategy_matches WHERE discovery_event_id=? OR token_mint=? ORDER BY created_at ASC", [discovery_id, token])
        snapshots = await _fetch_all(repo.db, "SELECT * FROM token_metric_snapshots WHERE token_mint=? AND observed_at BETWEEN ? AND ? ORDER BY observed_at ASC", [token, opened_at, closed_at])
        klines = await _fetch_all(repo.db, "SELECT * FROM kline_snapshots WHERE token_mint=? AND open_time BETWEEN ? AND ? ORDER BY open_time ASC", [token, opened_at, closed_at])
        ticks = await _fetch_all(repo.db, "SELECT * FROM tick_snapshots WHERE token_mint=? AND observed_at BETWEEN ? AND ? ORDER BY observed_at ASC", [token, opened_at, closed_at])
        provider_requests = await _fetch_all(repo.db, "SELECT * FROM provider_requests WHERE created_at BETWEEN ? AND ? ORDER BY created_at ASC", [opened_at, closed_at])
        system_events = await _fetch_all(
            repo.db,
            """
            SELECT * FROM system_events
            WHERE created_at BETWEEN ? AND ?
              AND (context_json LIKE ? OR context_json LIKE ? OR message LIKE ?)
            ORDER BY created_at ASC
            """,
            [opened_at, closed_at, f"%{token}%", f"%{pos_id}%", f"%{token}%"],
        )
        items.append({
            "position": p,
            "entry_summary": _summarize_entry(p, trades),
            "exit_summary": _summarize_exits(trades),
            "trade_events": trades,
            "discovery_events": discovery,
            "strategy_matches": _decode_json_fields(matches, ["pass_fail_detail_json", "feature_vector_json"]),
            "token_metric_snapshots": _decode_json_fields(snapshots, ["raw_json"]),
            "kline_snapshots": _decode_json_fields(klines, ["raw_json"]),
            "tick_snapshots": _decode_json_fields(ticks, ["raw_json"]),
            "provider_requests_in_window": _decode_json_fields(provider_requests, ["request_summary_json", "response_summary_json"]),
            "system_events": _decode_json_fields(system_events, ["context_json"]),
        })

    payload = {
        "export_type": "losing_trades_full_context",
        "session_started_at": session_started_at,
        "exported_at": _iso(),
        "losing_count": len(items),
        "items": items,
    }
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return _json_response({"ok": True, "export_path": str(dst), "losing_count": len(items)})


@router.post("/emergency/export-logs")
async def export_logs(request: Request):
    repo = request.app.state.repo
    session_started_at = getattr(request.app.state, "session_started_at", None) or "1970-01-01T00:00:00+00:00"
    export_dir = Path("data_backup")
    export_dir.mkdir(parents=True, exist_ok=True)
    ts = _utc_now().strftime("%Y%m%d_%H%M%S")
    dst = export_dir / f"session_error_and_strategy_report_{ts}.json"

    errors = await _fetch_all(
        repo.db,
        """
        SELECT level,category,message,context_json,account_type,MIN(created_at) AS first_seen_at,MAX(created_at) AS last_seen_at,COUNT(*) AS count
        FROM system_events
        WHERE created_at >= ? AND UPPER(level)='ERROR'
        GROUP BY level,category,message,context_json,account_type
        ORDER BY last_seen_at DESC
        """,
        [session_started_at],
    )

    matches = await _fetch_all(
        repo.db,
        "SELECT * FROM token_strategy_matches WHERE created_at >= ? ORDER BY created_at ASC",
        [session_started_at],
    )
    strategy_rows = await _fetch_all(repo.db, "SELECT id,name,is_live,enabled FROM strategy_groups")
    strategies = {int(r["id"]): r for r in strategy_rows}
    screen_summary = _build_screen_summary(matches, strategies)
    failure_top = _build_failure_top(matches, strategies)
    trade_stats = await _build_trade_stats(repo.db, session_started_at, strategies)

    payload = {
        "export_type": "session_error_and_strategy_report",
        "session_started_at": session_started_at,
        "exported_at": _iso(),
        "errors_deduped": _decode_json_fields(errors, ["context_json"]),
        "screening_summary": screen_summary,
        "failure_top10": failure_top,
        "trade_stats": trade_stats,
        "notes": [
            "first_round uses token_strategy_matches.stage='initial_filter'.",
            "second_round uses stages 'second_core_recheck' and 'second_filter'.",
            "max_simultaneous_exposure is reconstructed from confirmed BUY/SELL trade events and is best-effort when price_usd is missing.",
        ],
    }
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return _json_response({"ok": True, "export_path": str(dst), "error_count": len(errors)})


# Deprecated endpoint retained as a no-op so stale frontends do not 404.
@router.post("/emergency/repair-legacy-db")
async def repair_legacy_db(request: Request):
    await request.app.state.repo.append_system_event("INFO", "EMERGENCY", "repair-legacy-db deprecated noop", None, account_type="SIM")
    return _json_response({"ok": True, "deprecated": True, "repaired_count": 0})


# ---------------------------------------------------------------------------
# Strategy Groups
# ---------------------------------------------------------------------------

class StrategyGroupRequest(BaseModel):
    name: Optional[str] = None
    x: float
    y: float
    t_seconds: int
    enabled: bool = True
    is_live: bool = False


@router.get("/strategies")
async def list_runtime_strategies(request: Request):
    rows = await _fetch_all(
        request.app.state.repo.db,
        "SELECT * FROM strategy_groups ORDER BY is_live DESC, id ASC",
    )
    return _json_response({"strategies": rows})


@router.post("/strategies")
async def create_runtime_strategy(payload: StrategyGroupRequest, request: Request):
    repo = request.app.state.repo
    if payload.is_live and await _has_another_live_strategy(repo.db, None):
        return _json_response({"ok": False, "error": "实盘策略最多只能有一条；请先把现有实盘策略改为模拟盘。"}, status_code=400)
    name = payload.name or f"x={payload.x}, y={payload.y}, t={payload.t_seconds}s"
    now = _iso()
    raw_config = _safe_json_dumps({"x": payload.x, "y": payload.y, "t_seconds": payload.t_seconds})
    cur = await repo.db.execute(
        """
        INSERT INTO strategy_groups(name,enabled,is_live,priority,config_version,x,y,t_seconds,raw_config_json,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (name, 1 if payload.enabled else 0, 1 if payload.is_live else 0, 100, 1, payload.x, payload.y, payload.t_seconds, raw_config, now, now),
    )
    await repo.db.commit()
    row_id = cur.lastrowid
    row = await _fetch_one(repo.db, "SELECT * FROM strategy_groups WHERE id=?", [row_id])
    return _json_response({"strategy": row})


@router.put("/strategies/{strategy_id}")
async def update_runtime_strategy(strategy_id: int, payload: StrategyGroupRequest, request: Request):
    repo = request.app.state.repo
    existing = await _fetch_one(repo.db, "SELECT * FROM strategy_groups WHERE id=?", [strategy_id])
    if not existing:
        return _json_response({"ok": False, "error": "Strategy group not found"}, status_code=404)
    if payload.is_live and await _has_another_live_strategy(repo.db, strategy_id):
        return _json_response({"ok": False, "error": "实盘策略最多只能有一条；请先把现有实盘策略改为模拟盘。"}, status_code=400)

    name = payload.name or f"x={payload.x}, y={payload.y}, t={payload.t_seconds}s"
    now = _iso()
    raw_old = _safe_json_loads(existing.get("raw_config_json"), {}) or {}
    raw_old.update({"x": payload.x, "y": payload.y, "t_seconds": payload.t_seconds})
    await repo.db.execute(
        """
        UPDATE strategy_groups
        SET name=?, enabled=?, is_live=?, x=?, y=?, t_seconds=?, raw_config_json=?, config_version=COALESCE(config_version,1)+1, updated_at=?
        WHERE id=?
        """,
        (name, 1 if payload.enabled else 0, 1 if payload.is_live else 0, payload.x, payload.y, payload.t_seconds, _safe_json_dumps(raw_old), now, strategy_id),
    )
    await repo.db.commit()
    row = await _fetch_one(repo.db, "SELECT * FROM strategy_groups WHERE id=?", [strategy_id])
    return _json_response({"strategy": row})


@router.delete("/strategies/{strategy_id}")
async def delete_runtime_strategy(strategy_id: int, request: Request):
    repo = request.app.state.repo
    existing = await _fetch_one(repo.db, "SELECT * FROM strategy_groups WHERE id=?", [strategy_id])
    if not existing:
        return _json_response({"ok": False, "error": "Strategy group not found"}, status_code=404)
    await repo.db.execute("DELETE FROM strategy_groups WHERE id=?", [strategy_id])
    await repo.db.commit()
    return _json_response({"ok": True, "deleted_id": strategy_id})


async def _has_another_live_strategy(db, strategy_id: Optional[int]) -> bool:
    if strategy_id is None:
        row = await _fetch_one(db, "SELECT COUNT(*) AS n FROM strategy_groups WHERE is_live=1")
    else:
        row = await _fetch_one(db, "SELECT COUNT(*) AS n FROM strategy_groups WHERE is_live=1 AND id<>?", [strategy_id])
    return int(row.get("n", 0) if row else 0) > 0


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def _decode_json_fields(rows: List[Dict[str, Any]], fields: List[str]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        d = dict(row)
        for field in fields:
            if field in d:
                d[field.replace("_json", "")] = _safe_json_loads(d.get(field), d.get(field))
        out.append(d)
    return out


def _summarize_entry(position: Dict[str, Any], trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    buys = [t for t in trades if str(t.get("side", "")).upper() == "BUY"]
    return {
        "opened_at": position.get("opened_at"),
        "entry_price_usd": position.get("entry_price_usd"),
        "entry_price_sol": position.get("entry_price_sol"),
        "entry_token_amount": position.get("entry_token_amount"),
        "buy_events": len(buys),
        "requested_sol_amount": sum(_to_float(t.get("requested_sol_amount")) for t in buys),
        "executed_sol_amount": sum(_to_float(t.get("executed_sol_amount")) for t in buys),
        "avg_buy_slippage_bps": _avg([_to_float(t.get("slippage_bps"), math.nan) for t in buys]),
        "avg_buy_price_impact_pct": _avg([_to_float(t.get("price_impact_pct"), math.nan) for t in buys]),
    }


def _summarize_exits(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sells = [t for t in trades if str(t.get("side", "")).upper() == "SELL"]
    return [
        {
            "created_at": t.get("created_at"),
            "event_type": t.get("event_type"),
            "status": t.get("status"),
            "requested_pct": t.get("requested_pct"),
            "executed_token_amount": t.get("executed_token_amount"),
            "executed_sol_amount": t.get("executed_sol_amount"),
            "price_usd": t.get("price_usd"),
            "price_sol": t.get("price_sol"),
            "slippage_bps": t.get("slippage_bps"),
            "price_impact_pct": t.get("price_impact_pct"),
            "tx_signature": t.get("tx_signature"),
            "error_code": t.get("error_code"),
            "error_message": t.get("error_message"),
        }
        for t in sells
    ]


def _avg(values: List[float]) -> Optional[float]:
    vals = [v for v in values if math.isfinite(v)]
    return sum(vals) / len(vals) if vals else None


def _strategy_label(strategy_id: int, strategies: Dict[int, Dict[str, Any]]) -> str:
    row = strategies.get(int(strategy_id or 0), {})
    name = row.get("name") or f"strategy_{strategy_id}"
    attr = "实盘" if row.get("is_live") else "模拟盘"
    return f"{name}#{strategy_id}({attr})"


def _build_screen_summary(matches: List[Dict[str, Any]], strategies: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    data: Dict[int, Dict[str, Any]] = defaultdict(lambda: {
        "strategy": None,
        "first_round": {"screened": 0, "passed": 0, "failed": 0},
        "second_round": {"screened": 0, "passed": 0, "failed": 0, "core_recheck_screened": 0, "core_recheck_passed": 0},
    })
    for m in matches:
        sid = int(m.get("strategy_id") or 0)
        stage = str(m.get("stage") or "")
        passed = bool(m.get("passed"))
        data[sid]["strategy"] = _strategy_label(sid, strategies)
        if stage == "initial_filter":
            data[sid]["first_round"]["screened"] += 1
            data[sid]["first_round"]["passed" if passed else "failed"] += 1
        elif stage == "second_core_recheck":
            data[sid]["second_round"]["core_recheck_screened"] += 1
            if passed:
                data[sid]["second_round"]["core_recheck_passed"] += 1
        elif stage == "second_filter":
            data[sid]["second_round"]["screened"] += 1
            data[sid]["second_round"]["passed" if passed else "failed"] += 1
    return {str(sid): summary for sid, summary in sorted(data.items())}


def _detail_feature_names(detail_json: Any) -> List[str]:
    details = _safe_json_loads(detail_json, [])
    if isinstance(details, dict):
        details = [details]
    names: List[str] = []
    if not isinstance(details, list):
        return names
    for d in details:
        if not isinstance(d, dict):
            continue
        passed = d.get("passed")
        ok = d.get("ok")
        if passed is True or ok is True:
            continue
        name = d.get("name") or d.get("rule") or d.get("feature") or d.get("field") or d.get("metric") or d.get("key")
        if name:
            names.append(str(name))
    return names


def _build_failure_top(matches: List[Dict[str, Any]], strategies: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    counters: Dict[Tuple[int, str], Counter] = defaultdict(Counter)
    for m in matches:
        if bool(m.get("passed")):
            continue
        sid = int(m.get("strategy_id") or 0)
        stage = str(m.get("stage") or "")
        if stage == "initial_filter":
            round_name = "first_round"
        elif stage in {"second_core_recheck", "second_filter"}:
            round_name = "second_round"
        else:
            continue
        for name in _detail_feature_names(m.get("pass_fail_detail_json")):
            counters[(sid, round_name)][name] += 1

    result: Dict[str, Any] = {}
    for (sid, round_name), counter in counters.items():
        result.setdefault(str(sid), {"strategy": _strategy_label(sid, strategies), "first_round": [], "second_round": []})
        result[str(sid)][round_name] = [{"feature": k, "filtered_count": v} for k, v in counter.most_common(10)]
    return result


async def _build_trade_stats(db, session_started_at: str, strategies: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    trades = await _fetch_all(
        db,
        "SELECT * FROM trade_events WHERE created_at >= ? AND status='CONFIRMED' ORDER BY created_at ASC",
        [session_started_at],
    )
    positions = await _fetch_all(
        db,
        "SELECT * FROM positions WHERE opened_at >= ? ORDER BY opened_at ASC",
        [session_started_at],
    )
    stats: Dict[str, Any] = defaultdict(lambda: {
        "strategy": None,
        "trade_events": 0,
        "buy_events": 0,
        "sell_events": 0,
        "estimated_realized_pnl_sol": 0.0,
        "estimated_realized_pnl_usd": None,
        "max_simultaneous_exposure_usd": 0.0,
        "max_simultaneous_exposure_sol": 0.0,
    })

    for p in positions:
        locked = _safe_json_loads(p.get("locked_strategy_config_json"), {}) or {}
        sid = int(p.get("live_strategy_id") or locked.get("id") or locked.get("strategy_id") or 0)
        key = str(sid)
        stats[key]["strategy"] = _strategy_label(sid, strategies)
        stats[key]["estimated_realized_pnl_sol"] += _to_float(p.get("realized_pnl_sol"))

    exposure_usd: Dict[str, float] = defaultdict(float)
    exposure_sol: Dict[str, float] = defaultdict(float)
    for t in trades:
        sid = int(t.get("strategy_id") or 0)
        key = str(sid)
        stats[key]["strategy"] = _strategy_label(sid, strategies)
        stats[key]["trade_events"] += 1
        side = str(t.get("side") or "").upper()
        sol = _to_float(t.get("executed_sol_amount"))
        token_amount = _to_float(t.get("executed_token_amount"))
        usd = token_amount * _to_float(t.get("price_usd")) if token_amount and _to_float(t.get("price_usd")) else 0.0
        if usd <= 0 and sol > 0 and _to_float(t.get("price_usd")) > 0 and _to_float(t.get("price_sol")) > 0:
            usd = sol * (_to_float(t.get("price_usd")) / _to_float(t.get("price_sol")))
        if side == "BUY":
            stats[key]["buy_events"] += 1
            exposure_usd[key] += usd
            exposure_sol[key] += sol
        elif side == "SELL":
            stats[key]["sell_events"] += 1
            exposure_usd[key] = max(0.0, exposure_usd[key] - usd)
            exposure_sol[key] = max(0.0, exposure_sol[key] - sol)
        stats[key]["max_simultaneous_exposure_usd"] = max(stats[key]["max_simultaneous_exposure_usd"], exposure_usd[key])
        stats[key]["max_simultaneous_exposure_sol"] = max(stats[key]["max_simultaneous_exposure_sol"], exposure_sol[key])

    return dict(stats)


# ---------------------------------------------------------------------------
# Live readiness
# ---------------------------------------------------------------------------

def _check_live_readiness() -> dict:
    missing = []
    warnings = []
    checks = {}

    rpc_urls = settings.get_rpc_http_urls()
    gmgn_keys = settings.get_gmgn_api_keys()
    gmgn_client_ids = settings.get_gmgn_client_ids() if hasattr(settings, "get_gmgn_client_ids") else []
    gmgn_credentials = settings.get_gmgn_credentials() if hasattr(settings, "get_gmgn_credentials") else []
    jupiter_keys = settings.get_jupiter_api_keys()
    alchemy_keys = settings.get_alchemy_api_keys()
    ankr_keys = settings.get_ankr_api_keys()
    wallet_public_key = settings.get_wallet_public_key()
    wallet_private_key = settings.get_wallet_private_key_base58()

    checks["PROVIDER_MODE"] = settings.get_provider_mode().value
    checks["DRY_RUN"] = settings.DRY_RUN
    checks["SIMULATION_ENABLED"] = settings.SIMULATION_ENABLED
    checks["JITO_ENABLED"] = settings.JITO_ENABLED
    checks["JITO_BLOCK_ENGINE_URL"] = bool(settings.JITO_BLOCK_ENGINE_URL)
    checks["WALLET_PUBLIC_KEY"] = bool(wallet_public_key)
    checks["WALLET_PRIVATE_KEY_BASE58"] = bool(wallet_private_key)
    checks["GMGN_API_KEY_COUNT"] = len(gmgn_keys)
    checks["GMGN_CLIENT_ID_COUNT"] = len(gmgn_client_ids)
    checks["GMGN_CREDENTIAL_COUNT"] = len(gmgn_credentials)
    checks["JUPITER_API_BASE_URL"] = bool(settings.get_jupiter_api_base_url())
    checks["JUPITER_API_KEY_COUNT"] = len(jupiter_keys)
    checks["SOLANA_RPC_HTTP_URL_COUNT"] = len(rpc_urls)
    checks["SOLANA_RPC_WS_REQUIRED"] = False
    checks["SOLANA_RPC_WS_CONFIGURED"] = bool(settings.get_rpc_ws_url())
    checks["ALCHEMY_API_KEY_COUNT"] = len(alchemy_keys)
    checks["ANKR_API_KEY_COUNT"] = len(ankr_keys)
    checks["RPC_HTTP_PROVIDERS"] = [
        "alchemy" if "alchemy.com" in url else "ankr" if "ankr.com" in url else "custom"
        for url in rpc_urls
    ]

    if settings.DRY_RUN:
        missing.append("DRY_RUN=false required for formal live trading")
    if not settings.JITO_ENABLED:
        missing.append("JITO_ENABLED=true required")
    if not settings.JITO_BLOCK_ENGINE_URL:
        missing.append("JITO_BLOCK_ENGINE_URL missing")
    if not wallet_public_key:
        missing.append("WALLET_PUBLIC_KEY missing")
    if not wallet_private_key:
        missing.append("WALLET_PRIVATE_KEY_BASE58 missing")
    if not (gmgn_keys or gmgn_client_ids or gmgn_credentials):
        missing.append("GMGN_API_KEY_N or GMGN_CLIENT_ID_N/GMGN_PUBLIC_KEY_N missing")
    if not settings.get_jupiter_api_base_url():
        missing.append("JUPITER_API_BASE_URL missing")
    if not jupiter_keys:
        missing.append("JUPITER_API_KEY_N missing")
    if not rpc_urls:
        missing.append("SOLANA_RPC_HTTP_URLS missing")

    if settings.get_rpc_ws_url():
        warnings.append("SOLANA_RPC_WS_* is configured but the current RPC provider uses HTTP polling only")

    checks["missing"] = missing
    checks["warnings"] = warnings
    checks["ready"] = len(missing) == 0
    return checks
