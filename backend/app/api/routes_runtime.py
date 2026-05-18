from __future__ import annotations

import json
import math
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from ..config import ProviderMode, settings
from ..db.repositories import Repositories
from ..logging_config import logger

router = APIRouter(prefix="/api/runtime", tags=["runtime"])

LOG_EXPORT_DIR = Path("./logs")
OPEN_POSITION_EXCLUDED_STATUSES = ("CLOSED", "LEGACY_INVALID_CONFIG", "MIGRATION_NEEDED")


DEFAULT_SIM_STRATEGIES: List[Dict[str, Any]] = [
    {"name": "模拟盘1", "x": 0.20, "y": 2.25, "min_created": 180, "max_created": 300, "is_live": False, "priority": 10},
]

TRADING_PARAM_SPECS: List[Dict[str, Any]] = [
    {"key": "POLL_INTERVAL_SECONDS", "label": "主轮询间隔", "description": "discovery 主轮询间隔，单位秒。", "value_type": "int", "default": settings.POLL_INTERVAL_SECONDS, "min_value": 1},
    {"key": "ACTIVE_POSITION_PRICE_POLL_SECONDS", "label": "持仓价格轮询", "description": "活跃持仓价格监控间隔，单位秒。", "value_type": "int", "default": settings.ACTIVE_POSITION_PRICE_POLL_SECONDS, "min_value": 1},
    {"key": "TIP_FLOOR_REFRESH_SECONDS", "label": "Jito tip刷新", "description": "Jito tip floor 刷新间隔，单位秒。", "value_type": "int", "default": settings.TIP_FLOOR_REFRESH_SECONDS, "min_value": 1},
    {"key": "BUY_SLIPPAGE_CAP_BPS", "label": "买入滑点上限", "description": "买入报价滑点上限，单位 bps。", "value_type": "int", "default": settings.BUY_SLIPPAGE_CAP_BPS, "min_value": 0},
    {"key": "SELL_SLIPPAGE_CAP_BPS", "label": "卖出滑点上限", "description": "普通卖出滑点上限，单位 bps。", "value_type": "int", "default": settings.SELL_SLIPPAGE_CAP_BPS, "min_value": 0},
    {"key": "EMERGENCY_SLIPPAGE_CAP_BPS", "label": "紧急卖出滑点上限", "description": "紧急撤仓滑点上限，单位 bps。", "value_type": "int", "default": settings.EMERGENCY_SLIPPAGE_CAP_BPS, "min_value": 0},
    {"key": "PRICE_IMPACT_HARD_CAP_PCT", "label": "价格冲击硬上限", "description": "Jupiter quote 价格冲击硬上限，百分比。", "value_type": "float", "default": settings.PRICE_IMPACT_HARD_CAP_PCT, "min_value": 0},
    {"key": "LIVE_ROLLING_10_LOSS_LIMIT", "label": "实盘近10笔亏损限制", "description": "实盘熔断用近10笔滚动亏损阈值。", "value_type": "float", "default": settings.LIVE_ROLLING_10_LOSS_LIMIT, "min_value": None},
    {"key": "MAX_REQUOTE_RETRY", "label": "重新报价次数", "description": "交易报价失败或滑点超限后的最大重试次数。", "value_type": "int", "default": settings.MAX_REQUOTE_RETRY, "min_value": 0},
    {"key": "ENTRY_SIZE_LIQUIDITY_PCT", "label": "入场流动性比例", "description": "单笔入场金额占流动性的比例。", "value_type": "float", "default": settings.ENTRY_SIZE_LIQUIDITY_PCT, "min_value": 0},
    {"key": "ENTRY_MAX_USD", "label": "单笔最大入场USD", "description": "模拟/实盘单笔入场金额上限，单位 USD。", "value_type": "float", "default": settings.ENTRY_MAX_USD, "min_value": 0},
    {"key": "DUST_FORCE_EXIT_USD", "label": "尘埃仓强制清仓USD", "description": "持仓价值低于该值时，下次撤仓全部卖出。", "value_type": "float", "default": settings.DUST_FORCE_EXIT_USD, "min_value": 0},
    {"key": "RISK_FEATURE_SCAN_TIER_1_SECONDS", "label": "风控扫描T1秒数", "description": "持仓≥T1金额时的风控特征扫描间隔。", "value_type": "int", "default": settings.RISK_FEATURE_SCAN_TIER_1_SECONDS, "min_value": 1},
    {"key": "RISK_FEATURE_SCAN_TIER_2_SECONDS", "label": "风控扫描T2秒数", "description": "持仓≥T2金额时的风控特征扫描间隔。", "value_type": "int", "default": settings.RISK_FEATURE_SCAN_TIER_2_SECONDS, "min_value": 1},
    {"key": "RISK_FEATURE_SCAN_TIER_3_SECONDS", "label": "风控扫描T3秒数", "description": "持仓≥T3金额时的风控特征扫描间隔。", "value_type": "int", "default": settings.RISK_FEATURE_SCAN_TIER_3_SECONDS, "min_value": 1},
    {"key": "RISK_FEATURE_SCAN_TIER_4_SECONDS", "label": "风控扫描T4秒数", "description": "持仓≥T4金额时的风控特征扫描间隔。", "value_type": "int", "default": settings.RISK_FEATURE_SCAN_TIER_4_SECONDS, "min_value": 1},
    {"key": "RISK_FEATURE_SCAN_TIER_5_SECONDS", "label": "风控扫描T5秒数", "description": "持仓低于T4金额时的风控特征扫描间隔。", "value_type": "int", "default": settings.RISK_FEATURE_SCAN_TIER_5_SECONDS, "min_value": 1},
    {"key": "TOP1_HOLDER_SCAN_TIER_1_SECONDS", "label": "Top1扫描T1秒数", "description": "持仓≥T1金额时的 Top1 持仓扫描间隔。", "value_type": "int", "default": settings.TOP1_HOLDER_SCAN_TIER_1_SECONDS, "min_value": 1},
    {"key": "TOP1_HOLDER_SCAN_TIER_2_SECONDS", "label": "Top1扫描T2秒数", "description": "持仓≥T2金额时的 Top1 持仓扫描间隔。", "value_type": "int", "default": settings.TOP1_HOLDER_SCAN_TIER_2_SECONDS, "min_value": 1},
    {"key": "TOP1_HOLDER_SCAN_TIER_3_SECONDS", "label": "Top1扫描T3秒数", "description": "持仓≥T3金额时的 Top1 持仓扫描间隔。", "value_type": "int", "default": settings.TOP1_HOLDER_SCAN_TIER_3_SECONDS, "min_value": 1},
    {"key": "TOP1_HOLDER_SCAN_TIER_4_SECONDS", "label": "Top1扫描T4秒数", "description": "持仓≥T4金额时的 Top1 持仓扫描间隔。", "value_type": "int", "default": settings.TOP1_HOLDER_SCAN_TIER_4_SECONDS, "min_value": 1},
    {"key": "TOP1_HOLDER_SCAN_TIER_5_SECONDS", "label": "Top1扫描T5秒数", "description": "低价值持仓Top1扫描间隔；0 表示不单独扫描。", "value_type": "int", "default": settings.TOP1_HOLDER_SCAN_TIER_5_SECONDS, "min_value": 0},
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    try:
        return dict(row)
    except Exception:
        return {str(i): v for i, v in enumerate(row)}


def _as_bool(value: Any) -> bool:
    return value is True or value == 1 or str(value).lower() in {"1", "true", "yes", "on"}


def _close_number(a: Any, b: Any, *, tol: float = 1e-9) -> bool:
    try:
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=tol)
    except Exception:
        return False


def _safe_json_loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


async def ensure_runtime_defaults(repo: Any) -> None:
    """Ensure the required editable SIM strategy group exists.

    This only seeds/normalizes runtime configuration rows. It does not change
    trading filters, runners, order execution, or risk logic.
    """
    if not hasattr(repo, "list_strategy_groups") or not hasattr(repo, "create_strategy_group"):
        return

    existing = await repo.list_strategy_groups(include_disabled=True)

    for spec in DEFAULT_SIM_STRATEGIES:
        by_name = next((row for row in existing if str(row.get("name") or "") == spec["name"]), None)
        by_values = next(
            (
                row
                for row in existing
                if not _as_bool(row.get("is_live"))
                and int(row.get("min_created") or 0) == int(spec["min_created"])
                and int(row.get("max_created") or 0) == int(spec["max_created"])
                and _close_number(row.get("x"), spec["x"])
                and _close_number(row.get("y"), spec["y"])
            ),
            None,
        )

        target = by_name or by_values
        if target is None:
            new_id = await repo.create_strategy_group(
                name=spec["name"],
                x=float(spec["x"]),
                y=float(spec["y"]),
                min_created=int(spec["min_created"]),
                max_created=int(spec["max_created"]),
                is_live=False,
                priority=int(spec["priority"]),
                raw_config_json="{}",
            )
            if hasattr(repo, "get_strategy_group"):
                created = await repo.get_strategy_group(new_id)
                if created:
                    existing.append(created)
            continue

        updates: Dict[str, Any] = {}
        if _as_bool(target.get("is_live")):
            updates["is_live"] = 0
        if int(target.get("enabled") or 0) != 1:
            updates["enabled"] = 1
        if not _close_number(target.get("x"), spec["x"]):
            updates["x"] = float(spec["x"])
        if not _close_number(target.get("y"), spec["y"]):
            updates["y"] = float(spec["y"])
        if int(target.get("min_created") or 0) != int(spec["min_created"]):
            updates["min_created"] = int(spec["min_created"])
        if int(target.get("max_created") or 0) != int(spec["max_created"]):
            updates["max_created"] = int(spec["max_created"])
        if int(target.get("priority") or 0) != int(spec["priority"]):
            updates["priority"] = int(spec["priority"])

        if updates and hasattr(repo, "update_strategy_group"):
            await repo.update_strategy_group(int(target["id"]), updates)
            if any(key in updates for key in {"x", "y", "min_created", "max_created"}) and hasattr(repo, "increment_config_version"):
                await repo.increment_config_version(int(target["id"]))


async def _get_repo(request: Request) -> tuple[Repositories, bool]:
    repo = getattr(request.app.state, "repo", None)
    if repo is not None:
        return repo, False
    return await Repositories.create(settings.SQLITE_PATH), True


async def _fetch_all(repo: Repositories, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    async with repo.db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def _fetch_one(repo: Repositories, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    async with repo.db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def _table_exists(repo: Repositories, table: str) -> bool:
    row = await _fetch_one(repo, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return bool(row)


async def _runtime_settings(repo: Repositories) -> Dict[str, str]:
    try:
        return await repo.get_all_runtime_settings()
    except Exception:
        return {}


def _runtime_bool(runtime: Dict[str, str], key: str, default: bool = False) -> bool:
    if key not in runtime:
        return default
    return _as_bool(runtime.get(key))


def _open_status_sql() -> str:
    placeholders = ",".join(["?"] * len(OPEN_POSITION_EXCLUDED_STATUSES))
    return f"status NOT IN ({placeholders})"


async def _positions_summary(repo: Repositories) -> Dict[str, Any]:
    if hasattr(repo, "get_positions_summary"):
        data = await repo.get_positions_summary()
    else:
        data = {}

    open_sql = _open_status_sql()
    live = await _fetch_one(repo, f"SELECT COUNT(*) AS c FROM positions WHERE account_type='LIVE' AND {open_sql}", OPEN_POSITION_EXCLUDED_STATUSES)
    sim = await _fetch_one(repo, f"SELECT COUNT(*) AS c FROM positions WHERE account_type='SIM' AND {open_sql}", OPEN_POSITION_EXCLUDED_STATUSES)

    live_open = int((live or {}).get("c") or data.get("live_open_count") or 0)
    sim_open = int((sim or {}).get("c") or data.get("sim_open_count") or 0)

    data.update(
        {
            "live_open_count": live_open,
            "sim_open_count": sim_open,
            "live_open": live_open,
            "sim_open": sim_open,
            "total_open": live_open + sim_open,
            # The DB stores realized pnl in SOL. Keep existing SOL fields and add
            # USD aliases only when a caller has already populated them.
            "live_pnl_usd": float(data.get("live_pnl_usd") or 0),
            "sim_pnl_usd": float(data.get("sim_pnl_usd") or 0),
            "total_pnl_usd": float(data.get("total_pnl_usd") or data.get("live_pnl_usd") or 0),
        }
    )
    return data


def _live_readiness() -> Dict[str, Any]:
    provider_mode = settings.get_provider_mode().value
    checks = {
        "provider_mode_live": provider_mode == ProviderMode.LIVE.value,
        "dry_run_disabled": not bool(settings.DRY_RUN),
        "wallet_public_key_present": bool(settings.get_wallet_public_key()),
        "wallet_private_key_present": bool(settings.get_wallet_private_key_base58()),
        "rpc_url_present": bool(settings.get_rpc_http_urls()),
        "jupiter_key_present": bool(settings.get_jupiter_api_keys()),
        "gmgn_credentials_present": bool(settings.get_gmgn_credentials() or settings.get_gmgn_api_keys()),
        "jito_enabled": bool(settings.JITO_ENABLED),
    }
    return {"ok": all(checks.values()), "checks": checks}


async def _set_runtime_mode(request: Request, repo: Repositories, user_mode: str) -> Dict[str, Any]:
    readiness = _live_readiness()
    worker_mgr = getattr(request.app.state, "worker_manager", None)

    if user_mode == "IDLE":
        if worker_mgr is not None:
            await worker_mgr.stop_all()
        workers_enabled = False
        live_entries_enabled = False
        request.app.state.pause_new_entries = True
    else:
        if worker_mgr is not None:
            await worker_mgr.start_all()
        workers_enabled = True
        live_entries_enabled = user_mode == "FORMAL_SIM_LIVE" and bool(readiness["ok"])
        request.app.state.pause_new_entries = False

    await repo.set_runtime_setting("user_mode", user_mode, updated_by="api")
    await repo.set_runtime_setting("workers_enabled", "true" if workers_enabled else "false", updated_by="api")
    await repo.set_runtime_setting("live_entries_enabled", "true" if live_entries_enabled else "false", updated_by="api")
    await repo.append_system_event(
        "INFO",
        "RUNTIME",
        f"Runtime mode switched to {user_mode}",
        _json_dumps({"workers_enabled": workers_enabled, "live_entries_enabled": live_entries_enabled}),
        account_type="SIM",
    )
    return {"ok": True, "user_mode": user_mode, "provider_mode": settings.get_provider_mode().value, "workers_enabled": workers_enabled, "live_entries_enabled": live_entries_enabled}


@router.get("/status")
async def runtime_status(request: Request):
    repo, owned = await _get_repo(request)
    try:
        runtime = await _runtime_settings(repo)
        summary = await _positions_summary(repo)
        worker_mgr = getattr(request.app.state, "worker_manager", None)
        worker_status = worker_mgr.get_status() if worker_mgr is not None else {}
        readiness = _live_readiness()
        user_mode = runtime.get("user_mode", "IDLE")
        return {
            "ok": True,
            "user_mode": user_mode,
            "workers_enabled": _runtime_bool(runtime, "workers_enabled", any(v.get("running") for v in worker_status.values())),
            "live_entries_enabled": _runtime_bool(runtime, "live_entries_enabled", False),
            "provider_mode": settings.get_provider_mode().value,
            "dry_run": settings.DRY_RUN,
            "simulation_enabled": settings.SIMULATION_ENABLED,
            "pause_new_entries": bool(getattr(request.app.state, "pause_new_entries", True)),
            "session_started_at": runtime.get("session_started_at") or getattr(request.app.state, "session_started_at", None),
            "db_path": settings.SQLITE_PATH,
            "log_export_dir": str(LOG_EXPORT_DIR),
            "workers": worker_status,
            "live_open_count": int(summary.get("live_open_count") or 0),
            "has_live_positions": int(summary.get("live_open_count") or 0) > 0,
            "can_live_trade": bool(readiness["ok"]),
            "live_readiness": readiness,
        }
    finally:
        if owned:
            await repo.close()


@router.post("/mode")
async def set_mode(request: Request, payload: Dict[str, Any] = Body(...)):
    mode = str(payload.get("mode") or payload.get("user_mode") or "").strip().upper()
    if mode not in {"IDLE", "SIM_TEST", "FORMAL_SIM_LIVE"}:
        return JSONResponse({"ok": False, "error": "mode must be IDLE, SIM_TEST, or FORMAL_SIM_LIVE"}, status_code=400)
    if mode == "FORMAL_SIM_LIVE" and settings.get_provider_mode() == ProviderMode.MOCK:
        return JSONResponse({"ok": False, "error": "PROVIDER_MODE=mock; formal mode requires online_readonly/live configuration"}, status_code=400)

    repo, owned = await _get_repo(request)
    try:
        return await _set_runtime_mode(request, repo, mode)
    finally:
        if owned:
            await repo.close()


@router.post("/start-sim")
async def start_sim(request: Request):
    repo, owned = await _get_repo(request)
    try:
        return await _set_runtime_mode(request, repo, "SIM_TEST")
    finally:
        if owned:
            await repo.close()


@router.post("/start-formal")
async def start_formal(request: Request):
    if settings.get_provider_mode() == ProviderMode.MOCK:
        return JSONResponse({"ok": False, "error": "PROVIDER_MODE=mock; formal mode requires online_readonly/live configuration"}, status_code=400)
    repo, owned = await _get_repo(request)
    try:
        return await _set_runtime_mode(request, repo, "FORMAL_SIM_LIVE")
    finally:
        if owned:
            await repo.close()


@router.post("/stop")
async def stop_runtime(request: Request):
    repo, owned = await _get_repo(request)
    try:
        return await _set_runtime_mode(request, repo, "IDLE")
    finally:
        if owned:
            await repo.close()


@router.post("/emergency/stop-all")
async def emergency_stop_all(request: Request):
    repo, owned = await _get_repo(request)
    try:
        await _set_runtime_mode(request, repo, "IDLE")
        await repo.append_system_event(
            "CRITICAL",
            "EMERGENCY",
            "Emergency stop all triggered",
            _json_dumps({"source": "api"}),
            account_type="SIM",
        )
        return {"ok": True, "message": "runtime stopped"}
    finally:
        if owned:
            await repo.close()


@router.get("/strategies")
async def list_strategies(request: Request, include_disabled: bool = True):
    repo, owned = await _get_repo(request)
    try:
        await ensure_runtime_defaults(repo)
        items = await repo.list_strategy_groups(include_disabled=include_disabled)
        return {"ok": True, "items": items, "strategies": items}
    finally:
        if owned:
            await repo.close()


@router.post("/strategies")
async def create_strategy(request: Request, payload: Dict[str, Any] = Body(...)):
    repo, owned = await _get_repo(request)
    try:
        raw_config_json = payload.get("raw_config_json")
        if not isinstance(raw_config_json, str):
            raw_config_json = _json_dumps(raw_config_json or {})
        strategy_id = await repo.create_strategy_group(
            name=str(payload.get("name") or "策略组"),
            x=float(payload.get("x", 0.2)),
            y=float(payload.get("y", 2.25)),
            min_created=int(payload.get("min_created", payload.get("t", 150))),
            max_created=int(payload.get("max_created", 300)),
            is_live=bool(payload.get("is_live", False)),
            priority=int(payload.get("priority", 100)),
            raw_config_json=raw_config_json,
        )
        if "enabled" in payload:
            await repo.update_strategy_group(strategy_id, {"enabled": 1 if _as_bool(payload.get("enabled")) else 0})
        strategy = await repo.get_strategy_group(strategy_id) if hasattr(repo, "get_strategy_group") else None
        return {"ok": True, "id": strategy_id, "strategy": strategy}
    finally:
        if owned:
            await repo.close()


@router.put("/strategies/{strategy_id}")
@router.patch("/strategies/{strategy_id}")
async def upsert_strategy(strategy_id: int, request: Request, payload: Dict[str, Any] = Body(...)):
    allowed = {
        "name", "enabled", "is_live", "priority", "config_version", "x", "y", "min_created", "max_created",
        "buy_slippage_cap_bps", "sell_slippage_cap_bps", "emergency_slippage_cap_bps",
        "price_impact_hard_cap_pct", "raw_config_json",
    }
    updates = {k: v for k, v in payload.items() if k in allowed}
    if "enabled" in updates:
        updates["enabled"] = 1 if _as_bool(updates["enabled"]) else 0
    if "is_live" in updates:
        updates["is_live"] = 1 if _as_bool(updates["is_live"]) else 0
    if "raw_config_json" in updates and not isinstance(updates["raw_config_json"], str):
        updates["raw_config_json"] = _json_dumps(updates["raw_config_json"])

    repo, owned = await _get_repo(request)
    try:
        if updates:
            await repo.update_strategy_group(strategy_id, updates)
            if any(k in updates for k in {"x", "y", "min_created", "max_created", "raw_config_json"}):
                await repo.increment_config_version(strategy_id)
        strategy = await repo.get_strategy_group(strategy_id) if hasattr(repo, "get_strategy_group") else None
        return {"ok": True, "strategy": strategy}
    finally:
        if owned:
            await repo.close()


@router.delete("/strategies/{strategy_id}")
async def delete_strategy(strategy_id: int, request: Request):
    repo, owned = await _get_repo(request)
    try:
        if hasattr(repo, "delete_strategy_group"):
            await repo.delete_strategy_group(strategy_id)
        return {"ok": True, "id": strategy_id}
    finally:
        if owned:
            await repo.close()


@router.get("/positions/summary")
async def runtime_positions_summary(request: Request):
    repo, owned = await _get_repo(request)
    try:
        return await _positions_summary(repo)
    finally:
        if owned:
            await repo.close()


@router.get("/portfolio/table")
async def runtime_portfolio_table(request: Request, account_type: str = "SIM", limit: int = 100):
    account = str(account_type or "SIM").upper()
    if account not in {"SIM", "LIVE"}:
        return JSONResponse({"ok": False, "error": "account_type must be SIM or LIVE"}, status_code=400)

    repo, owned = await _get_repo(request)
    try:
        rows = await _fetch_all(
            repo,
            """
            SELECT p.*, sg.name AS strategy_name
            FROM positions p
            LEFT JOIN strategy_groups sg ON sg.id = p.live_strategy_id
            WHERE p.account_type = ?
            ORDER BY COALESCE(p.updated_at, p.opened_at) DESC, p.id DESC
            LIMIT ?
            """,
            (account, int(limit)),
        )
        for row in rows:
            token = str(row.get("token_mint") or "")
            row["mint_short"] = f"{token[:4]}...{token[-4:]}" if len(token) > 10 else token
            row["strategy_id"] = row.get("live_strategy_id")
            row["remaining"] = row.get("remaining_value_usd")
            try:
                entry = float(row.get("entry_price_usd") or 0)
                remaining_token = float(row.get("remaining_token_amount") or 0)
                remaining_value = float(row.get("remaining_value_usd") or 0)
                current_price = remaining_value / remaining_token if remaining_token > 0 else 0
                row["ratio"] = round(current_price / entry, 4) if entry > 0 and current_price > 0 else None
            except Exception:
                row["ratio"] = None
        return rows
    finally:
        if owned:
            await repo.close()


@router.get("/filter-stats")
async def runtime_filter_stats(request: Request):
    repo, owned = await _get_repo(request)
    try:
        # 近10次 discovery run 的 trenches 拉回数 & 初筛通过数
        discovery_events = await _fetch_all(
            repo,
            """
            SELECT context_json FROM system_events
            WHERE category = 'DISCOVERY' AND message = 'Discovery run complete'
            ORDER BY created_at DESC LIMIT 10
            """,
        )
        trench_history: List[Dict[str, Any]] = []
        for ev in reversed(discovery_events):
            try:
                ctx = json.loads(ev.get("context_json", "{}"))
            except Exception:
                ctx = {}
            trench_history.append({
                "count": ctx.get("count", 0),
                "passed": ctx.get("tracked_initial_passed", 0),
            })

        # 淘汰统计：取最近 risk_filter + price_filter 未通过的记录
        fail_rows_raw = await _fetch_all(
            repo,
            """
            SELECT pass_fail_detail_json FROM token_strategy_matches
            WHERE stage IN ('risk_filter', 'price_filter') AND passed = 0
            ORDER BY created_at DESC LIMIT 1000
            """,
        )
        fail_counts: Dict[str, int] = {}
        for row in fail_rows_raw:
            try:
                details = json.loads(row.get("pass_fail_detail_json", "[]"))
            except Exception:
                continue
            for d in details:
                if isinstance(d, dict) and not d.get("passed", True):
                    name = str(d.get("name") or d.get("rule") or "unknown")
                    fail_counts[name] = fail_counts.get(name, 0) + 1
        filter_fails = sorted(
            [{"rule": k, "count": v} for k, v in fail_counts.items()],
            key=lambda x: x["count"], reverse=True,
        )

        return {
            "trench_history": trench_history,
            "filter_fails": filter_fails,
        }
    except Exception as e:
        return {"trench_history": [], "filter_fails": [], "error": str(e)}
    finally:
        if owned:
            await repo.close()


def _spec_by_key() -> Dict[str, Dict[str, Any]]:
    return {item["key"]: item for item in TRADING_PARAM_SPECS}


def _coerce_param_value(key: str, raw: Any) -> float | int:
    spec = _spec_by_key()[key]
    value = int(raw) if spec["value_type"] == "int" else float(raw)
    min_value = spec.get("min_value")
    if min_value is not None and value < min_value:
        raise ValueError(f"{key} must be >= {min_value}")
    return value


async def _trading_param_values(repo: Repositories) -> Dict[str, float | int]:
    runtime = await _runtime_settings(repo)
    values: Dict[str, float | int] = {}
    for spec in TRADING_PARAM_SPECS:
        key = spec["key"]
        raw = runtime.get(key, getattr(settings, key, spec["default"]))
        try:
            values[key] = _coerce_param_value(key, raw)
        except Exception:
            values[key] = spec["default"]
    return values


@router.get("/trading-params")
async def get_trading_params(request: Request):
    repo, owned = await _get_repo(request)
    try:
        return {"ok": True, "specs": TRADING_PARAM_SPECS, "values": await _trading_param_values(repo)}
    finally:
        if owned:
            await repo.close()


@router.put("/trading-params")
async def update_trading_params(request: Request, payload: Dict[str, Any] = Body(...)):
    incoming = payload.get("values") if isinstance(payload.get("values"), dict) else payload
    if not isinstance(incoming, dict):
        return JSONResponse({"ok": False, "error": "payload must be {values:{...}}"}, status_code=400)

    allowed = _spec_by_key()
    updates: Dict[str, float | int] = {}
    try:
        for key, raw in incoming.items():
            if key not in allowed:
                continue
            updates[key] = _coerce_param_value(key, raw)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    repo, owned = await _get_repo(request)
    try:
        for key, value in updates.items():
            await repo.set_runtime_setting(key, str(value), updated_by="api")
            try:
                setattr(settings, key, value)
            except Exception:
                logger.warning("Failed to update in-process setting", key=key, value=value)

        worker_mgr = getattr(request.app.state, "worker_manager", None)
        if worker_mgr is not None:
            if "POLL_INTERVAL_SECONDS" in updates:
                interval = int(updates["POLL_INTERVAL_SECONDS"])
                worker_mgr.update_interval("discovery", interval)
            if "ACTIVE_POSITION_PRICE_POLL_SECONDS" in updates:
                worker_mgr.update_interval("price_monitor", int(updates["ACTIVE_POSITION_PRICE_POLL_SECONDS"]))

        values = await _trading_param_values(repo)
        return {"ok": True, "values": values}
    finally:
        if owned:
            await repo.close()


@router.post("/emergency/stop-live")
async def stop_live(request: Request):
    repo, owned = await _get_repo(request)
    try:
        return await _set_runtime_mode(request, repo, "SIM_TEST")
    finally:
        if owned:
            await repo.close()


@router.post("/emergency/resume-live")
async def resume_live(request: Request):
    if settings.get_provider_mode() == ProviderMode.MOCK:
        return JSONResponse({"ok": False, "error": "PROVIDER_MODE=mock; formal mode requires online_readonly/live configuration"}, status_code=400)
    repo, owned = await _get_repo(request)
    try:
        return await _set_runtime_mode(request, repo, "FORMAL_SIM_LIVE")
    finally:
        if owned:
            await repo.close()


@router.post("/emergency/sell-all-live")
async def sell_all_live(request: Request):
    # Do not invent a sell implementation here. This endpoint removes frontend
    # 404s while preserving the existing trading pipeline boundary.
    repo, owned = await _get_repo(request)
    try:
        await repo.append_system_event(
            "ERROR",
            "RUNTIME",
            "sell-all-live endpoint called but no bulk live liquidation implementation is wired",
            _json_dumps({"source": "api"}),
            account_type="LIVE",
        )
        return JSONResponse({"ok": False, "error": "Bulk live liquidation is not wired in the current backend; no order was sent."}, status_code=501)
    finally:
        if owned:
            await repo.close()


@router.post("/emergency/backup-db")
async def backup_db(request: Request):
    repo, owned = await _get_repo(request)
    try:
        src = Path(settings.SQLITE_PATH)
        LOG_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = LOG_EXPORT_DIR / f"trading_bot_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.sqlite3"
        await repo.db.commit()
        if src.exists():
            shutil.copy2(src, out)
        else:
            return JSONResponse({"ok": False, "error": f"SQLite DB not found: {src}"}, status_code=404)
        return {"ok": True, "export_path": str(out), "path": str(out)}
    finally:
        if owned:
            await repo.close()


@router.post("/emergency/export-losing")
async def export_losing(request: Request):
    repo, owned = await _get_repo(request)
    try:
        LOG_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        rows = await _fetch_all(
            repo,
            """
            SELECT *
            FROM positions
            WHERE (realized_pnl_pct IS NOT NULL AND realized_pnl_pct < 0)
               OR (pnl_pct IS NOT NULL AND pnl_pct < 0)
               OR status IN ('EMERGENCY_CLOSED','CLOSED_LOSS')
            ORDER BY COALESCE(closed_at, updated_at, opened_at) DESC
            LIMIT 2000
            """,
        )
        payload = {"export_type": "losing_positions", "exported_at": utc_now_iso(), "items": rows}
        out = LOG_EXPORT_DIR / f"losing_positions_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return {"ok": True, "export_path": str(out), "path": str(out), "losing_count": len(rows), "count": len(rows), "data": payload}
    finally:
        if owned:
            await repo.close()


@router.post("/emergency/export-logs")
async def export_logs(request: Request):
    repo, owned = await _get_repo(request)
    try:
        LOG_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        errors = await _fetch_all(
            repo,
            """
            SELECT level, category, message, account_type,
                   MIN(created_at) AS first_seen_at,
                   MAX(created_at) AS last_seen_at,
                   COUNT(*) AS count
            FROM system_events
            WHERE level = 'ERROR'
            GROUP BY level, category, message, account_type
            ORDER BY last_seen_at DESC
            LIMIT 500
            """,
        )
        strategies = await repo.list_strategy_groups(include_disabled=True) if hasattr(repo, "list_strategy_groups") else []
        summary = await _positions_summary(repo)
        payload = {
            "export_type": "runtime_error_logs",
            "exported_at": utc_now_iso(),
            "session_started_at": (await _runtime_settings(repo)).get("session_started_at"),
            "errors_deduped": errors,
            "positions_summary": summary,
            "strategy_groups": strategies,
        }
        out = LOG_EXPORT_DIR / f"runtime_error_logs_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        try:
            import asyncio as _asyncio
            await _asyncio.to_thread(out.write_text, text, encoding="utf-8")
        except Exception:
            out.write_text(text, encoding="utf-8")
        return {"ok": True, "export_path": str(out), "path": str(out), "error_count": len(errors)}
    except Exception as e:
        logger.exception("export-logs failed")
        return {"ok": False, "error": str(e), "error_count": 0, "export_path": ""}
    finally:
        if owned:
            await repo.close()


async def _deduped_errors(repo: Repositories, limit: int = 200) -> List[Dict[str, Any]]:
    if not await _table_exists(repo, "system_events"):
        return []
    rows = await _fetch_all(
        repo,
        """
        SELECT level, category, message, context_json, account_type,
               MIN(created_at) AS first_seen_at,
               MAX(created_at) AS last_seen_at,
               COUNT(*) AS count
        FROM system_events
        WHERE level = 'ERROR'
        GROUP BY level, category, message, context_json, account_type
        ORDER BY last_seen_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    for row in rows:
        row["context"] = _safe_json_loads(row.get("context_json"), {})
    return rows


def _strategy_label(sg: Dict[str, Any]) -> str:
    acct = "正式盘" if int(sg.get("is_live") or 0) else "模拟盘"
    return f"{sg.get('name') or '策略'}#{sg.get('id')}({acct})"


async def _screening_summary(repo: Repositories, strategies: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not await _table_exists(repo, "token_strategy_matches"):
        return out
    for sg in strategies:
        sid = int(sg.get("id"))
        rows = await _fetch_all(
            repo,
            """
            SELECT stage,
                   COUNT(*) AS screened,
                   SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) AS passed,
                   SUM(CASE WHEN passed = 0 THEN 1 ELSE 0 END) AS failed
            FROM token_strategy_matches
            WHERE strategy_id = ?
            GROUP BY stage
            """,
            (sid,),
        )
        by_stage = {r["stage"]: r for r in rows}
        risk = by_stage.get("risk_filter", {})
        price = by_stage.get("price_filter", {})
        out[str(sid)] = {
            "strategy": _strategy_label(sg),
            "risk_filter": {
                "screened": int(risk.get("screened") or 0),
                "passed": int(risk.get("passed") or 0),
                "failed": int(risk.get("failed") or 0),
            },
            "price_filter": {
                "screened": int(price.get("screened") or 0),
                "passed": int(price.get("passed") or 0),
                "failed": int(price.get("failed") or 0),
            },
        }
    return out


def _extract_failed_features(pass_fail_detail_json: Any, fallback: str) -> List[str]:
    data = _safe_json_loads(pass_fail_detail_json, None)
    names: List[str] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            passed = item.get("passed")
            if passed is False or passed == 0 or str(passed).lower() == "false":
                names.append(str(item.get("name") or item.get("rule") or item.get("feature") or fallback))
    elif isinstance(data, dict):
        passed = data.get("passed")
        if passed is False or passed == 0 or str(passed).lower() == "false" or data.get("rule"):
            names.append(str(data.get("name") or data.get("rule") or data.get("feature") or fallback))
    return [n for n in names if n]


async def _failure_top10(repo: Repositories, strategies: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not await _table_exists(repo, "token_strategy_matches"):
        return out
    for sg in strategies:
        sid = int(sg.get("id"))
        rows = await _fetch_all(
            repo,
            """
            SELECT stage, pass_fail_detail_json
            FROM token_strategy_matches
            WHERE strategy_id = ? AND passed = 0
            """,
            (sid,),
        )
        risk_counts: Counter[str] = Counter()
        price_counts: Counter[str] = Counter()
        for row in rows:
            stage = row.get("stage") or "unknown"
            names = _extract_failed_features(row.get("pass_fail_detail_json"), stage) or [stage]
            if stage == "price_filter":
                price_counts.update(names)
            else:
                risk_counts.update(names)
        out[str(sid)] = {
            "strategy": _strategy_label(sg),
            "risk_fails": [{"feature": k, "filtered_count": v} for k, v in risk_counts.most_common(10)],
            "price_fails": [{"feature": k, "filtered_count": v} for k, v in price_counts.most_common(10)],
        }
    return out


async def _trade_stats(repo: Repositories) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if await _table_exists(repo, "positions"):
        rows = await _fetch_all(
            repo,
            """
            SELECT account_type, status, COUNT(*) AS count,
                   AVG(realized_pnl_pct) AS avg_realized_pnl_pct,
                   SUM(realized_pnl_sol) AS sum_realized_pnl_sol
            FROM positions
            GROUP BY account_type, status
            """,
        )
        out["positions_by_status"] = rows
    if await _table_exists(repo, "trade_events"):
        rows = await _fetch_all(
            repo,
            """
            SELECT account_type, side, status, COUNT(*) AS count
            FROM trade_events
            GROUP BY account_type, side, status
            """,
        )
        out["trade_events_by_side_status"] = rows
    return out


async def _snapshot_by_ids(repo: Repositories, token: str, ids: List[Any]) -> List[Dict[str, Any]]:
    if not await _table_exists(repo, "token_metric_snapshots"):
        return []
    clean_ids = [int(x) for x in ids if x not in (None, "")]
    rows: List[Dict[str, Any]] = []
    if clean_ids:
        placeholders = ",".join(["?"] * len(clean_ids))
        rows.extend(await _fetch_all(repo, f"SELECT * FROM token_metric_snapshots WHERE id IN ({placeholders}) ORDER BY id ASC", tuple(clean_ids)))
    if not rows:
        rows = await _fetch_all(repo, "SELECT * FROM token_metric_snapshots WHERE token_mint=? ORDER BY id DESC LIMIT 3", (token,))
    for row in rows:
        row["raw"] = _safe_json_loads(row.get("raw_json"), row.get("raw_json"))
    return rows


async def _raw_pool_payload(repo: Repositories, strategy_id: int, discovery_event_id: int) -> Dict[str, Any]:
    de = await _fetch_one(repo, "SELECT * FROM discovery_events WHERE id=?", (discovery_event_id,)) or {}
    token = de.get("token_mint") or ""
    matches = await _fetch_all(
        repo,
        "SELECT * FROM token_strategy_matches WHERE discovery_event_id=? AND strategy_id=? ORDER BY id ASC",
        (discovery_event_id, strategy_id),
    )
    snapshot_ids: List[Any] = [de.get("source_snapshot_id"), de.get("initial_snapshot_id"), de.get("recheck_snapshot_id")]
    snapshot_ids.extend([m.get("snapshot_id") for m in matches])
    snapshots = await _snapshot_by_ids(repo, token, snapshot_ids)

    klines: List[Dict[str, Any]] = []
    if await _table_exists(repo, "kline_snapshots"):
        klines = await _fetch_all(repo, "SELECT * FROM kline_snapshots WHERE token_mint=? ORDER BY id DESC LIMIT 20", (token,))
        for row in klines:
            row["raw"] = _safe_json_loads(row.get("raw_json"), row.get("raw_json"))

    ticks: List[Dict[str, Any]] = []
    if await _table_exists(repo, "tick_snapshots"):
        ticks = await _fetch_all(repo, "SELECT * FROM tick_snapshots WHERE token_mint=? ORDER BY id DESC LIMIT 20", (token,))
        for row in ticks:
            row["raw"] = _safe_json_loads(row.get("raw_json"), row.get("raw_json"))

    provider_requests: List[Dict[str, Any]] = []
    if await _table_exists(repo, "provider_requests") and token:
        like = f"%{token}%"
        provider_requests = await _fetch_all(
            repo,
            """
            SELECT *
            FROM provider_requests
            WHERE provider='GMGN'
              AND (request_summary_json LIKE ? OR response_summary_json LIKE ? OR endpoint LIKE ?)
            ORDER BY id DESC
            LIMIT 20
            """,
            (like, like, like),
        )
        for row in provider_requests:
            row["request_summary"] = _safe_json_loads(row.get("request_summary_json"), row.get("request_summary_json"))
            row["response_summary"] = _safe_json_loads(row.get("response_summary_json"), row.get("response_summary_json"))

    for m in matches:
        m["pass_fail_detail"] = _safe_json_loads(m.get("pass_fail_detail_json"), m.get("pass_fail_detail_json"))
        m["feature_vector"] = _safe_json_loads(m.get("feature_vector_json"), m.get("feature_vector_json"))

    return {
        "discovery_event": de,
        "token": token,
        "strategy_id": strategy_id,
        "strategy_matches": matches,
        "gmgn_raw_token_metric_snapshots": snapshots,
        "gmgn_raw_kline_snapshots": klines,
        "gmgn_raw_tick_snapshots": ticks,
        "gmgn_provider_request_summaries": provider_requests,
        "note": "Raw GMGN payloads are read from token_metric_snapshots.raw_json / kline_snapshots.raw_json / tick_snapshots.raw_json already persisted by the runners; export does not make new GMGN HTTP calls.",
    }


async def _sample_event(repo: Repositories, strategy_id: int, *, price_passed: bool) -> Optional[int]:
    if price_passed:
        row = await _fetch_one(
            repo,
            """
            SELECT discovery_event_id
            FROM token_strategy_matches
            WHERE strategy_id=? AND stage='price_filter' AND passed=1 AND discovery_event_id IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (strategy_id,),
        )
        return int(row["discovery_event_id"]) if row and row.get("discovery_event_id") is not None else None
    row = await _fetch_one(
        repo,
        """
        SELECT im.discovery_event_id
        FROM token_strategy_matches im
        WHERE im.strategy_id=?
          AND im.stage='risk_filter'
          AND im.passed=1
          AND im.discovery_event_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM token_strategy_matches sm
              WHERE sm.discovery_event_id = im.discovery_event_id
                AND sm.strategy_id = im.strategy_id
                AND sm.stage = 'price_filter'
                AND sm.passed = 1
          )
        ORDER BY im.id DESC
        LIMIT 1
        """,
        (strategy_id,),
    )
    return int(row["discovery_event_id"]) if row and row.get("discovery_event_id") is not None else None


async def _raw_samples(repo: Repositories, strategies: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not (await _table_exists(repo, "token_strategy_matches") and await _table_exists(repo, "discovery_events")):
        return out
    for sg in strategies:
        sid = int(sg.get("id"))
        failed_event_id = await _sample_event(repo, sid, price_passed=False)
        passed_event_id = await _sample_event(repo, sid, price_passed=True)
        out[str(sid)] = {"strategy": _strategy_label(sg), "risk_passed_but_not_price_passed": None, "price_passed_pool": None}
        if failed_event_id is not None:
            out[str(sid)]["risk_passed_but_not_price_passed"] = await _raw_pool_payload(repo, sid, failed_event_id)
        if passed_event_id is not None:
            out[str(sid)]["price_passed_pool"] = await _raw_pool_payload(repo, sid, passed_event_id)
    return out


async def _session_error_and_strategy_report(repo: Repositories) -> Dict[str, Any]:
    if await _table_exists(repo, "strategy_groups"):
        strategies = await _fetch_all(repo, "SELECT * FROM strategy_groups ORDER BY priority ASC, id ASC")
    else:
        strategies = []
    report = {
        "export_type": "session_error_and_strategy_report",
        "exported_at": utc_now_iso(),
        "errors_deduped": await _deduped_errors(repo),
        "screening_summary": await _screening_summary(repo, strategies),
        "failure_top10": await _failure_top10(repo, strategies),
        "trade_stats": await _trade_stats(repo),
        "gmgn_raw_samples_by_strategy": await _raw_samples(repo, strategies),
        "notes": [
            "risk_filter uses token_strategy_matches.stage='risk_filter'.",
            "price_filter uses stage 'price_filter'.",
            "gmgn_raw_samples_by_strategy chooses one pool per strategy that passed risk_filter but did not pass price_filter, plus one pool that passed price_filter when available.",
            "Raw GMGN data is exported from persisted raw_json columns; the export endpoint does not call GMGN again.",
            "Logs/reports are written under ./logs instead of ./data_backup.",
        ],
    }
    # Preserve optional session_started_at setting if the runtime layer stored it.
    try:
        session_started_at = await repo.get_runtime_setting("session_started_at")
        if session_started_at:
            report["session_started_at"] = session_started_at
    except Exception:
        pass
    return report


@router.post("/emergency/export-session-report")
async def export_session_report(request: Request):
    repo, owned = await _get_repo(request)
    try:
        LOG_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        report = await _session_error_and_strategy_report(repo)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = LOG_EXPORT_DIR / f"session_error_and_strategy_report_{ts}.json"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return {"ok": True, "path": str(out_path), "data": report}
    except Exception as exc:
        logger.exception("export_session_report failed", error=str(exc))
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    finally:
        if owned:
            await repo.close()


@router.get("/emergency/export-session-report")
async def export_session_report_get(request: Request):
    return await export_session_report(request)
