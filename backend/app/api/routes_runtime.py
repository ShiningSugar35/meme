from __future__ import annotations

import json
import math
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from ..config import ProviderMode, settings
from ..db.repositories import Repositories
from ..logging_config import logger

router = APIRouter(prefix="/api/runtime", tags=["runtime"])

LOG_EXPORT_DIR = Path("./logs")
OPEN_POSITION_EXCLUDED_STATUSES = ("CLOSED", "LEGACY_INVALID_CONFIG", "MIGRATION_NEEDED")

RULE_META: Dict[str, Dict[str, str]] = {
    "type_new_creation": {"label": "type≠new_creation", "stage": "risk_filter", "section": "prefilter"},
    "min_liquidity_usd": {"label": "流动性不足", "stage": "risk_filter", "section": "风控指标"},
    "platform": {"label": "平台不在白名单", "stage": "risk_filter", "section": "prefilter"},
    "top_10_holder_rate_range": {"label": "top10持仓比超限", "stage": "risk_filter", "section": "风控指标"},
    "renounced_mint": {"label": "mint未renounce", "stage": "risk_filter", "section": "prefilter"},
    "renounced_freeze_account": {"label": "freeze未renounce", "stage": "risk_filter", "section": "prefilter"},
    "rug_ratio": {"label": "rug比例超标", "stage": "risk_filter", "section": "风控指标"},
    "entrapment_ratio": {"label": "entrapment超标", "stage": "risk_filter", "section": "风控指标"},
    "is_wash_trading": {"label": "疑似wash trading", "stage": "risk_filter", "section": "风控指标"},
    "rat_trader_amount_rate": {"label": "rat trader超标", "stage": "risk_filter", "section": "风控指标"},
    "suspected_insider_hold_rate": {"label": "疑似内幕持仓", "stage": "risk_filter", "section": "风控指标"},
    "bundler_trader_amount_rate": {"label": "bundler比例超标", "stage": "risk_filter", "section": "风控指标"},
    "fresh_wallet_rate": {"label": "新钱包比例超标", "stage": "risk_filter", "section": "风控指标"},
    "sell_tax": {"label": "sell_tax超标", "stage": "risk_filter", "section": "风控指标"},
    "has_at_least_one_social": {"label": "缺少社交(仅x<0.15)", "stage": "risk_filter", "section": "基础/社交"},
    "burn_status": {"label": "burn状态不符", "stage": "risk_filter", "section": "风控指标"},
    "sniper_count": {"label": "sniper数量超标", "stage": "risk_filter", "section": "风控指标"},
    "top1_holder": {"label": "TOP1持仓超标", "stage": "risk_filter", "section": "风控指标"},
    "top1_holder_addr_type0": {"label": "TOP1普通地址持仓超标", "stage": "top_holder_filter", "section": "风控指标"},
    "latest_price_present": {"label": "最新价格缺失", "stage": "price_filter", "section": "价格面及其他指标"},
    "swaps_5m_scaled": {"label": "swaps_5m不达标", "stage": "price_filter", "section": "价格面及其他指标"},
    "price_change_1h": {"label": "1h价格涨幅不足", "stage": "price_filter", "section": "价格面及其他指标"},
    "smart_degen": {"label": "聪明钱指标不满足", "stage": "smart_degen_filter", "section": "价格面及其他指标"},
    "top1_holder_rate_observed": {"label": "TOP1持有率观测", "stage": "risk_filter", "section": "observed_only"},
}

FIELD_SECTION_MAP: Dict[str, Tuple[str, str, str]] = {
    "type": ("风控指标", "type", "trenches"),
    "liquidity_usd": ("风控指标", "最新流动性", "trenches/token_metric_snapshots"),
    "top_10_holder_rate": ("风控指标", "top10持仓率", "trenches/token_metric_snapshots"),
    "top1_holder_rate": ("风控指标", "top1持仓率", "trenches/token_metric_snapshots"),
    "renounced_mint": ("基础/风控", "mint已renounce", "trenches/token_metric_snapshots"),
    "renounced_freeze_account": ("基础/风控", "freeze已renounce", "trenches/token_metric_snapshots"),
    "max_rug_ratio": ("风控指标", "rug比例", "trenches/token_metric_snapshots"),
    "max_entrapment_ratio": ("风控指标", "entrapment比例", "trenches/token_metric_snapshots"),
    "is_wash_trading": ("风控指标", "wash trading标记", "trenches/token_metric_snapshots"),
    "rat_trader_amount_rate": ("风控指标", "rat trader比例", "trenches/token_metric_snapshots"),
    "suspected_insider_hold_rate": ("风控指标", "内幕持仓率", "trenches/token_metric_snapshots"),
    "max_bundler_rate": ("风控指标", "bundler比例", "trenches/token_metric_snapshots"),
    "fresh_wallet_rate": ("风控指标", "新钱包比例", "trenches/token_metric_snapshots"),
    "sell_tax": ("风控指标", "卖税", "trenches/token_metric_snapshots"),
    "has_social": ("基础/社交", "社交标记", "trenches/token_metric_snapshots"),
    "burn_status": ("风控指标", "burn状态", "trenches/token_metric_snapshots"),
    "sniper_count": ("风控指标", "sniper数量", "trenches/token_metric_snapshots"),
    "launchpad": ("基础/社交", "平台", "trenches/token_metric_snapshots"),
    "market_cap": ("价格面", "市值", "trenches/token_metric_snapshots"),
    "price_usd": ("价格面", "价格", "trenches/token_metric_snapshots"),
}

CRITICAL_NUMERIC_FIELDS = {"liquidity_usd", "market_cap", "price_usd", "volume_usd"}

def _resolve_rule_meta(rule: str) -> Dict[str, str]:
    if rule in RULE_META:
        meta = dict(RULE_META[rule])
        if meta.get("section") in ("observed_only", "data_unavailable", "prefilter"):
            meta["exclude_from_ranking"] = "true"
        return meta
    return {"label": rule, "stage": "unknown", "section": "未知/其他指标"}


DEFAULT_SIM_STRATEGIES: List[Dict[str, Any]] = [
    {"name": "模拟盘1", "x": settings.STRATEGY_DEFAULT_X, "y": settings.STRATEGY_DEFAULT_Y, "is_live": False, "priority": 10},
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
        if int(target.get("priority") or 0) != int(spec["priority"]):
            updates["priority"] = int(spec["priority"])

        if updates and hasattr(repo, "update_strategy_group"):
            await repo.update_strategy_group(int(target["id"]), updates)
            if any(key in updates for key in {"x", "y"}) and hasattr(repo, "increment_config_version"):
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
        workers_enabled = True
        live_entries_enabled = user_mode == "FORMAL_SIM_LIVE" and bool(readiness["ok"])
        request.app.state.pause_new_entries = False

    await repo.set_runtime_setting("user_mode", user_mode, updated_by="api")
    await repo.set_runtime_setting("workers_enabled", "true" if workers_enabled else "false", updated_by="api")
    await repo.set_runtime_setting("live_entries_enabled", "true" if live_entries_enabled else "false", updated_by="api")

    if user_mode != "IDLE" and worker_mgr is not None:
        await worker_mgr.start_all()
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
            x=float(payload.get("x", settings.STRATEGY_DEFAULT_X)),
            y=float(payload.get("y", settings.STRATEGY_DEFAULT_Y)),
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
        "name", "enabled", "is_live", "priority", "config_version", "x", "y",
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
            if any(k in updates for k in {"x", "y", "raw_config_json"}):
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
        latest_ev = await _fetch_one(repo,
            "SELECT id, context_json, created_at FROM system_events WHERE category='DISCOVERY' AND message='Discovery run complete' ORDER BY created_at DESC LIMIT 1")
        if not latest_ev:
            return {"trench_history": [], "filter_fails": [], "data_source_health": {"summary": {}, "endpoint_health": [], "field_health": [], "price_age_health": {}}}

        ctx = json.loads(latest_ev.get("context_json", "{}")) if isinstance(latest_ev.get("context_json"), str) else (latest_ev.get("context_json") or {})
        if not isinstance(ctx, dict):
            ctx = {}
        run_started = ctx.get("run_started_at") or ""
        run_finished = ctx.get("run_finished_at") or ""
        if not run_started:
            ev_ts = latest_ev.get("created_at") or ""
            try:
                from datetime import timedelta
                dt = datetime.fromisoformat(str(ev_ts).replace("Z", "+00:00"))
                dt = dt - timedelta(seconds=max(getattr(settings, 'POLL_INTERVAL_SECONDS', 60), 120))
                run_started = dt.isoformat()
            except Exception:
                run_started = ev_ts
        if not run_finished:
            run_finished = latest_ev.get("created_at") or utc_now_iso()

        latest_unique = ctx.get("unique_fetched_count") or ctx.get("count") or 0
        latest_raw = ctx.get("raw_fetched_count") or latest_unique
        latest_dup = ctx.get("duplicate_count_estimate") or 0

        latest_risk_pass = 0
        if run_started:
            row = await _fetch_one(repo,
                """SELECT COUNT(DISTINCT tsm.token_mint || '|' || COALESCE(NULLIF(de.pool_address,''), NULLIF(snap.pool_address,''), '')) AS c
                   FROM token_strategy_matches tsm
                   LEFT JOIN token_metric_snapshots snap ON snap.id = tsm.snapshot_id
                   LEFT JOIN discovery_events de ON de.id = tsm.discovery_event_id
                   WHERE tsm.stage='risk_filter' AND tsm.passed=1 AND tsm.created_at >= ? AND tsm.created_at <= ?""",
                (run_started, run_finished))
            latest_risk_pass = int((row or {}).get("c", 0))

        trench_history = [{
            "count": latest_unique, "raw_count": latest_raw, "unique_count": latest_unique,
            "duplicate_count_estimate": latest_dup, "passed": latest_risk_pass,
            "trench_groups": ctx.get("trench_groups"), "created_at": latest_ev.get("created_at"),
            "run_started_at": run_started, "run_finished_at": run_finished,
        }]

        filter_fails: List[Dict[str, Any]] = []
        if run_started and latest_unique > 0:
            match_rows = await _fetch_all(repo,
                "SELECT pass_fail_detail_json, stage, tsm.token_mint, COALESCE(NULLIF(de.pool_address,''), NULLIF(snap.pool_address,''), '') AS pool_addr FROM token_strategy_matches tsm LEFT JOIN token_metric_snapshots snap ON snap.id = tsm.snapshot_id LEFT JOIN discovery_events de ON de.id = tsm.discovery_event_id WHERE tsm.stage IN ('risk_filter','price_filter','kline_fallback','top_holder_filter','smart_degen_filter') AND tsm.created_at >= ? AND tsm.created_at <= ?",
                (run_started, run_finished))
            rule_stats: Dict[str, Dict[str, Any]] = {}
            for row in match_rows:
                try:
                    details = json.loads(row.get("pass_fail_detail_json", "[]"))
                except Exception:
                    details = []
                if not isinstance(details, list):
                    details = []
                for d in details:
                    if not isinstance(d, dict):
                        continue
                    rule = str(d.get("name") or d.get("rule") or "unknown")
                    passed = bool(d.get("passed", True))
                    missing = bool(d.get("missing", False)) or bool(d.get("age_missing", False))
                    if rule not in rule_stats:
                        meta = _resolve_rule_meta(rule)
                        rule_stats[rule] = {"rule": rule, "label": meta.get("label", rule), "stage": meta.get("stage", "unknown"),
                                             "section": meta.get("section", ""), "exclude_from_ranking": meta.get("exclude_from_ranking") == "true",
                                             "checked_count": 0, "failed_count": 0, "sample_values": []}
                    rs = rule_stats[rule]
                    rs["checked_count"] += 1
                    if not passed:
                        rs["failed_count"] += 1
                    if missing:
                        rs["missing_count"] = rs.get("missing_count", 0) + 1
                    if len(rs.get("sample_values", [])) < 5:
                        rs["sample_values"].append(str(d.get("value", ""))[:40])
            for rule, rs in rule_stats.items():
                cc = max(rs["checked_count"], 1)
                rs["actual_checked_count"] = rs["checked_count"]
                rs["denominator_count"] = rs["checked_count"]
                rs["fail_rate"] = rs["failed_count"] / cc
                rs["fail_rate_pct"] = round(rs["fail_rate"] * 100.0, 1)
            filter_fails = sorted(
                [rs for rule, rs in rule_stats.items() if not rs.get("exclude_from_ranking")],
                key=lambda x: (-x["fail_rate"], -x["failed_count"], x["rule"]),
            )

        lower_bound = run_started
        upper_bound = run_finished or utc_now_iso()
        has_window = bool(lower_bound)
        data_source_health = await _build_data_source_health(repo, lower_bound, upper_bound, has_window, trench_history)

        return {"trench_history": trench_history, "filter_fails": filter_fails, "data_source_health": data_source_health}
    except Exception as e:
        logger.warning("filter-stats error", error=str(e))
        return {"trench_history": [], "filter_fails": [], "data_source_health": {}, "error": str(e)}
    finally:
        if owned:
            await repo.close()


async def _count_all_time_unique_trench_pools(repo: Repositories) -> int:
    row = await _fetch_one(repo,
        "SELECT COUNT(DISTINCT token_mint || '|' || COALESCE(NULLIF(pool_address,''), '')) AS c FROM token_metric_snapshots")
    return int((row or {}).get("c", 0))


async def _count_stage_unique_pools(repo: Repositories, stage: str, passed: Optional[bool] = None) -> int:
    extra = ""
    if passed is True:
        extra = " AND tsm.passed=1"
    elif passed is False:
        extra = " AND tsm.passed=0"
    row = await _fetch_one(repo,
        f"SELECT COUNT(DISTINCT tsm.token_mint || '|' || COALESCE(NULLIF(de.pool_address,''), NULLIF(snap.pool_address,''), '')) AS c FROM token_strategy_matches tsm LEFT JOIN token_metric_snapshots snap ON snap.id=tsm.snapshot_id LEFT JOIN discovery_events de ON de.id=tsm.discovery_event_id WHERE tsm.stage=?{extra}",
        (stage,))
    return int((row or {}).get("c", 0))


async def _build_data_source_health(repo: Repositories, lower_bound: str, upper_bound: str, has_window: bool, trench_history: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"window_run_count": len(trench_history), "discovery_mode": "two_group_discovery", "stats_mode": "all_time_unique"}

    all_time_trench = await _count_all_time_unique_trench_pools(repo)
    summary["trench_total"] = all_time_trench
    summary["risk_filter_count"] = all_time_trench
    summary["risk_filter_pass_count"] = await _count_stage_unique_pools(repo, "risk_filter", passed=True)
    summary["price_filter_count"] = await _count_stage_unique_pools(repo, "price_filter", passed=None)
    summary["price_filter_pass_count"] = await _count_stage_unique_pools(repo, "price_filter", passed=True)
    summary["all_time_unique_pool_count"] = all_time_trench

    if has_window:
        rl_429 = await _fetch_one(repo,
            "SELECT COUNT(*) AS c FROM provider_requests WHERE provider='GMGN' AND status_code=429 AND created_at>=? AND created_at<=?",
            (lower_bound, upper_bound))
        summary["total_429_count"] = int((rl_429 or {}).get("c", 0))

    endpoint_health, credential_summary = await _build_endpoint_health(repo, lower_bound, upper_bound, has_window)
    field_health = await _build_field_health(repo, lower_bound, upper_bound, has_window)
    price_age_health = await _build_price_age_health(repo, lower_bound, upper_bound, has_window)
    price_face_health = await _build_price_face_health(repo, lower_bound, upper_bound, has_window)

    discovery_fetch_health = await _build_discovery_fetch_health(trench_history)
    credential_health = await _build_credential_health()
    feature_stage_health = await _build_feature_stage_health(repo, lower_bound, upper_bound, has_window, summary, all_time_trench)

    system_event_warnings = await _build_system_event_warnings(repo, lower_bound, upper_bound, has_window)

    ok_groups = [g for g in discovery_fetch_health if g.get("ok")]
    summary["discovery_groups_ok"] = len(ok_groups)
    summary["discovery_groups_total"] = len(discovery_fetch_health)

    return {
        "summary": summary,
        "endpoint_health": endpoint_health,
        "credential_summary": credential_summary,
        "credential_health": credential_health,
        "discovery_fetch_health": discovery_fetch_health,
        "feature_stage_health": feature_stage_health,
        "field_health": field_health,
        "price_age_health": price_age_health,
        "price_face_health": price_face_health,
        "system_event_warnings": system_event_warnings,
    }


async def _build_discovery_fetch_health(trench_history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for th in reversed(trench_history):
        groups = th.get("trench_groups")
        if isinstance(groups, list) and groups:
            for g in groups:
                if isinstance(g, dict):
                    ok = bool(g.get("ok", False))
                    raw_count = g.get("raw_count", 0)
                    items.append({
                        "group_name": g.get("group_name", ""),
                        "platforms": g.get("platforms", []),
                        "slot": g.get("slot"),
                        "role": g.get("role"),
                        "ok": ok,
                        "raw_count": raw_count,
                        "unique_count": g.get("unique_count", 0),
                        "duplicate_count": g.get("duplicate_count", 0),
                        "status_code": g.get("status_code"),
                        "error": g.get("error"),
                        "cooldown_until": g.get("cooldown_until"),
                        "latency_ms": g.get("latency_ms"),
                        "severity": "critical" if not ok else ("warn" if raw_count == 0 else "ok"),
                    })
            break
    return items


async def _build_credential_health() -> List[Dict[str, Any]]:
    try:
        from ..providers.rate_limiter import get_rate_limiter
        rl = get_rate_limiter()
        result = []
        for slot, cred in rl.slots.items():
            severity: str
            ok_rate: Optional[float]
            if cred.total_calls == 0:
                severity = "idle"
                ok_rate = None
            else:
                calls = max(cred.total_calls, 1)
                ok_rate = cred.ok_calls / calls
                if ok_rate < 0.5:
                    severity = "critical"
                elif ok_rate < 0.9 or cred.is_cooldown():
                    severity = "warn"
                else:
                    severity = "ok"
            result.append({
                "slot": slot,
                "role": cred.role,
                "total_calls": cred.total_calls,
                "total_weight": cred.total_weight,
                "ok_calls": cred.ok_calls,
                "failed_calls": cred.failed_calls,
                "rate_limited_count": cred.rate_limited_count,
                "local_rate_limited_count": cred.rate_limited_count,
                "cooldown_until": cred.cooldown_until if cred.is_cooldown() else None,
                "cooldown_remaining_s": round(cred.cooldown_remaining(), 1),
                "ok_rate": round(ok_rate, 3) if ok_rate is not None else None,
                "endpoints": dict(cred.endpoints),
                "severity": severity,
            })
        return sorted(result, key=lambda x: x["slot"])
    except Exception:
        return []


async def _build_feature_stage_health(repo: Repositories, lower_bound: str, upper_bound: str, has_window: bool, summary: Dict[str, Any], all_time_trench: int) -> List[Dict[str, Any]]:
    from ..providers.rate_limiter import _endpoint_weight
    stages = [
        {"stage": "risk_filter", "label": "Trenches本地(Stage 0)", "endpoint_filter": "%v1/trenches%", "weight": 3},
        {"stage": "price_filter", "label": "Token Info价格(Stage 1)", "endpoint_filter": "%v1/token/info%", "weight": 1},
        {"stage": "kline_fallback", "label": "Kline回退(Stage 2)", "endpoint_filter": "%v1/market/token_kline%", "weight": 2},
        {"stage": "top_holder_filter", "label": "Top1 Holder(Stage 3)", "endpoint_filter": "%v1/market/token_top_holders%", "weight": 5, "exclude_tag": "smart_degen"},
        {"stage": "smart_degen_filter", "label": "Smart Degen(Stage 4)", "endpoint_filter": "%v1/market/token_top_holders%", "weight": 5, "require_tag": "smart_degen"},
    ]
    result: List[Dict[str, Any]] = []
    for s in stages:
        stage = s["stage"]
        if stage == "risk_filter":
            total = all_time_trench
            passed = await _count_stage_unique_pools(repo, stage, passed=True)
            failed = total - passed
        else:
            total = await _count_stage_unique_pools(repo, stage, passed=None)
            passed = await _count_stage_unique_pools(repo, stage, passed=True)
            failed = total - passed
        api_calls = 0
        ok_rate_val = None
        rate_limited = 0
        if has_window:
            ep_filter = s["endpoint_filter"]
            tag_extra = ""
            if s.get("exclude_tag"):
                tag_extra = " AND (request_summary_json NOT LIKE ?)"
            elif s.get("require_tag"):
                tag_extra = " AND (request_summary_json LIKE ?)"
            tag_param = f"%{s.get('exclude_tag') or s.get('require_tag') or ''}%" if (s.get("exclude_tag") or s.get("require_tag")) else ""
            api_row = await _fetch_one(repo,
                f"SELECT COUNT(*) AS c FROM provider_requests WHERE provider='GMGN' AND endpoint LIKE ?{tag_extra} AND created_at>=? AND created_at<=?",
                (ep_filter, tag_param, lower_bound, upper_bound) if tag_param else (ep_filter, lower_bound, upper_bound))
            api_calls = int((api_row or {}).get("c", 0))
            if api_calls > 0:
                ok_row = await _fetch_one(repo,
                    f"SELECT COUNT(*) AS c FROM provider_requests WHERE provider='GMGN' AND endpoint LIKE ?{tag_extra} AND ok=1 AND created_at>=? AND created_at<=?",
                    (ep_filter, tag_param, lower_bound, upper_bound) if tag_param else (ep_filter, lower_bound, upper_bound))
                ok_rate_val = round(int((ok_row or {}).get("c", 0)) / max(api_calls, 1), 3)
            rl_row = await _fetch_one(repo,
                f"SELECT COUNT(*) AS c FROM provider_requests WHERE provider='GMGN' AND endpoint LIKE ?{tag_extra} AND status_code=429 AND created_at>=? AND created_at<=?",
                (ep_filter, tag_param, lower_bound, upper_bound) if tag_param else (ep_filter, lower_bound, upper_bound))
            rate_limited = int((rl_row or {}).get("c", 0))
        if total > 0 and failed == total:
            severity = "critical"
        elif rate_limited > 0 or (ok_rate_val is not None and ok_rate_val < 0.9):
            severity = "warn"
        else:
            severity = "ok"
        result.append({
            "stage": stage, "label": s["label"], "endpoint": s["endpoint_filter"].replace('%',''), "weight": s["weight"],
            "candidates_in": total, "checked_count": total, "passed_count": passed,
            "failed_count": failed, "skipped_count": 0,
            "api_calls": api_calls, "ok_rate": ok_rate_val, "rate_limited_count": rate_limited,
            "avg_latency_ms": 0, "severity": severity,
        })
    return result


async def _build_endpoint_health(repo: Repositories, lower_bound: str, upper_bound: str, has_window: bool) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Returns (endpoint_health_by_slot, credential_summary_stats)."""
    if not has_window:
        return [], []
    all_rows = await _fetch_all(repo,
        "SELECT endpoint, method, status_code, latency_ms, ok, error_summary, request_summary_json FROM provider_requests WHERE provider='GMGN' AND created_at>=? AND created_at<=? ORDER BY id DESC",
        (lower_bound, upper_bound))
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in all_rows:
        ep = str(row.get("endpoint") or "")
        method = str(row.get("method", "GET"))
        cred_slot = "unknown"
        try:
            rs = json.loads(row.get("request_summary_json") or "{}")
            if isinstance(rs, dict):
                cred_slot = str(rs.get("credential_slot", "unknown"))
                role = rs.get("credential_role", "round_robin")
        except Exception:
            pass
        key = f"{ep}|{method}|slot={cred_slot}"
        if key not in grouped:
            grouped[key] = {"endpoint": ep, "method": method, "credential_slot": cred_slot,
                            "calls": 0, "ok_calls": 0, "total_latency": 0,
                            "latest_status_code": None, "latest_error": None}
        grp = grouped[key]
        grp["calls"] += 1
        grp["total_latency"] += int(row.get("latency_ms") or 0)
        if int(row.get("ok") or 0):
            grp["ok_calls"] += 1
        sc = row.get("status_code")
        if sc is not None and grp["latest_status_code"] is None:
            grp["latest_status_code"] = int(sc)
        err = row.get("error_summary")
        if err and not grp["latest_error"]:
            grp["latest_error"] = str(err)[:200]

    result: List[Dict[str, Any]] = []
    cred_stats: Dict[str, Dict[str, Any]] = {}
    for key, grp in grouped.items():
        calls = max(grp["calls"], 1)
        ok_rate = grp["ok_calls"] / calls
        if ok_rate < 0.5:
            severity = "critical"
        elif ok_rate < 0.9:
            severity = "warn"
        else:
            severity = "ok"
        result.append({
            "endpoint": grp["endpoint"],
            "method": grp["method"],
            "credential_slot": grp["credential_slot"],
            "calls": grp["calls"],
            "ok_calls": grp["ok_calls"],
            "ok_rate": round(ok_rate, 3),
            "latest_status_code": grp["latest_status_code"],
            "avg_latency_ms": round(grp["total_latency"] / calls, 1),
            "latest_error": grp["latest_error"],
            "severity": severity,
        })
        slot = grp["credential_slot"]
        if slot not in cred_stats:
            cred_stats[slot] = {"slot": slot, "total_calls": 0, "failed_calls": 0, "ok_calls": 0}
        cs = cred_stats[slot]
        cs["total_calls"] += grp["calls"]
        cs["failed_calls"] += (grp["calls"] - grp["ok_calls"])
        cs["ok_calls"] += grp["ok_calls"]

    summary = []
    failed_slots = []
    for slot, cs in sorted(cred_stats.items()):
        ok_rate = cs["ok_calls"] / max(cs["total_calls"], 1)
        summary.append({"slot": slot, "total_calls": cs["total_calls"], "failed_calls": cs["failed_calls"],
                        "ok_rate": round(ok_rate, 3)})
        if ok_rate < 0.9:
            failed_slots.append(slot)

    return sorted(result, key=lambda x: (0 if x["severity"] == "critical" else 1 if x["severity"] == "warn" else 2, -x["ok_rate"])), summary


async def _build_field_health(repo: Repositories, lower_bound: str, upper_bound: str, has_window: bool) -> List[Dict[str, Any]]:
    FIELDS = [
        ("type", "type"), ("liquidity_usd", "流动性"), ("top_10_holder_rate", "top10持仓率"),
        ("top1_holder_rate", "top1持仓率"), ("renounced_mint", "mint renounce"),
        ("renounced_freeze_account", "freeze renounce"), ("max_rug_ratio", "rug比例"),
        ("max_entrapment_ratio", "entrapment比例"), ("is_wash_trading", "wash trading"),
        ("rat_trader_amount_rate", "rat trader"), ("suspected_insider_hold_rate", "内幕持仓率"),
        ("max_bundler_rate", "bundler比例"), ("fresh_wallet_rate", "新钱包比例"),
        ("sell_tax", "卖税"), ("has_social", "社交"), ("burn_status", "burn状态"),
        ("sniper_count", "sniper数量"), ("launchpad", "平台"), ("market_cap", "市值"),
        ("price_usd", "价格"),
    ]

    token_count = 0
    if has_window:
        cnt_row = await _fetch_one(repo,
            "SELECT COUNT(*) AS c FROM tokens WHERE first_seen_at >= ? AND first_seen_at <= ?",
            (lower_bound, upper_bound))
        token_count = int((cnt_row or {}).get("c", 0))
    else:
        cnt_row = await _fetch_one(repo, "SELECT COUNT(*) AS c FROM tokens")
        token_count = int((cnt_row or {}).get("c", 0))

    if token_count == 0:
        return []

    results: List[Dict[str, Any]] = []
    for col, label in FIELDS:
        if has_window:
            nonnull_row = await _fetch_one(repo,
                f"SELECT COUNT(*) AS c FROM token_metric_snapshots WHERE {col} IS NOT NULL AND {col} != '' AND observed_at >= ? AND observed_at <= ?",
                (lower_bound, upper_bound))
            all_row = await _fetch_one(repo,
                "SELECT COUNT(*) AS c FROM token_metric_snapshots WHERE observed_at >= ? AND observed_at <= ?",
                (lower_bound, upper_bound))
        else:
            nonnull_row = await _fetch_one(repo,
                f"SELECT COUNT(*) AS c FROM token_metric_snapshots WHERE {col} IS NOT NULL AND {col} != ''")
            all_row = await _fetch_one(repo,
                "SELECT COUNT(*) AS c FROM token_metric_snapshots")

        checked = int((all_row or {}).get("c", 0))
        nonnull = int((nonnull_row or {}).get("c", 0))
        if checked == 0:
            continue
        missing = checked - nonnull
        missing_rate = missing / checked

        is_numeric = col in CRITICAL_NUMERIC_FIELDS or any(t in col.lower() for t in ("rate", "ratio", "tax", "count", "cap"))
        zero_count = 0
        if is_numeric and nonnull > 0:
            zero_clause = f"AND ({col} = 0 OR {col} = 0.0 OR CAST({col} AS REAL) = 0)"
            if has_window:
                zero_row = await _fetch_one(repo,
                    f"SELECT COUNT(*) AS c FROM token_metric_snapshots WHERE observed_at >= ? AND observed_at <= ? {zero_clause}",
                    (lower_bound, upper_bound))
            else:
                zero_row = await _fetch_one(repo,
                    f"SELECT COUNT(*) AS c FROM token_metric_snapshots WHERE {zero_clause}")
            zero_count = int((zero_row or {}).get("c", 0))
        zero_rate = zero_count / max(nonnull, 1)

        sample_vals: List[str] = []
        if has_window:
            sample_rows = await _fetch_all(repo,
                f"SELECT {col}, token_mint FROM token_metric_snapshots WHERE {col} IS NOT NULL AND {col} != '' AND observed_at >= ? AND observed_at <= ? LIMIT 5",
                (lower_bound, upper_bound))
        else:
            sample_rows = await _fetch_all(repo,
                f"SELECT {col}, token_mint FROM token_metric_snapshots WHERE {col} IS NOT NULL AND {col} != '' LIMIT 5")
        for sr in sample_rows:
            v = sr.get(col)
            sample_vals.append(str(v)[:40])
        sample_tokens: List[str] = []
        for sr in sample_rows:
            t = sr.get("token_mint")
            if t:
                sample_tokens.append(str(t)[:12])

        note = ""
        if missing_rate >= 0.9:
            severity = "critical"
            note = "字段基本缺失，可能GMGN字段名未对齐或未返回"
        elif missing_rate >= 0.2 or (is_numeric and zero_rate >= 0.8):
            severity = "warn"
            if missing_rate >= 0.2:
                note = f"字段缺失率较高 ({missing_rate:.0%})"
            elif zero_rate >= 0.8:
                note = f"数值字段为0比例极高 ({zero_rate:.0%})"
        else:
            severity = "ok"

        results.append({
            "section": "风控指标",
            "field": col,
            "label": label,
            "source": "trenches/token_metric_snapshots",
            "checked_count": checked,
            "nonnull_count": nonnull,
            "missing_count": missing,
            "zero_count": zero_count,
            "missing_rate": round(missing_rate, 3),
            "zero_rate": round(zero_rate, 3),
            "sample_values": sample_vals,
            "sample_tokens": sample_tokens,
            "severity": severity,
            "note": note,
        })

    results.sort(key=lambda x: (
        0 if x["severity"] == "critical" else 1 if x["severity"] == "warn" else 2,
        -x["missing_rate"],
        -x["zero_rate"],
    ))
    return results


async def _build_price_age_health(repo: Repositories, lower_bound: str, upper_bound: str, has_window: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "under_60m_count": 0,
        "age_parse_missing_count": 0,
        "price_change_source_counts": {},
        "swaps_source_counts": {},
        "price_screen_reached_count": 0,
        "risk_only_failed_count": 0,
        "price_screen_not_reached_reason": "",
        "warnings": [],
    }
    if not has_window:
        return result

    price_rows = await _fetch_all(repo,
        "SELECT feature_vector_json FROM token_strategy_matches WHERE stage='price_filter' AND created_at>=? AND created_at<=?",
        (lower_bound, upper_bound))
    result["price_screen_reached_count"] = len(price_rows)

    risk_fail_rows = await _fetch_all(repo,
        """SELECT tsm.token_mint FROM token_strategy_matches tsm
           WHERE tsm.stage='risk_filter' AND tsm.passed=0 AND tsm.created_at>=? AND tsm.created_at<=?
             AND NOT EXISTS (SELECT 1 FROM token_strategy_matches psm
                             WHERE psm.token_mint=tsm.token_mint AND psm.stage='price_filter'
                               AND psm.created_at>=? AND psm.created_at<=?)
           LIMIT 5""",
        (lower_bound, upper_bound, lower_bound, upper_bound))
    risk_failed_count_row = await _fetch_one(repo,
        """SELECT COUNT(DISTINCT token_mint) AS c FROM token_strategy_matches
           WHERE stage='risk_filter' AND passed=0 AND created_at>=? AND created_at<=?
             AND NOT EXISTS (SELECT 1 FROM token_strategy_matches psm
                             WHERE psm.token_mint=token_strategy_matches.token_mint AND psm.stage='price_filter'
                               AND psm.created_at>=? AND psm.created_at<=?)""",
        (lower_bound, upper_bound, lower_bound, upper_bound))
    result["risk_only_failed_count"] = int((risk_failed_count_row or {}).get("c", 0))

    if result["risk_only_failed_count"] > 0 and result["price_screen_reached_count"] == 0:
        result["price_screen_not_reached_reason"] = f"全部{result['risk_only_failed_count']}个token在risk_filter阶段失败，未到达price_filter阶段。请检查风控字段数据覆盖率与阈值设定。"
    elif result["risk_only_failed_count"] > 0:
        result["price_screen_not_reached_reason"] = f"{result['risk_only_failed_count']}个token在risk_filter失败后未进入price_filter（已通过{result['price_screen_reached_count']}个）。"

    if not price_rows:
        return result

    source_counts: Dict[str, int] = defaultdict(int)
    swaps_src_counts: Dict[str, int] = defaultdict(int)
    under_60m = 0
    age_missing = 0

    for row in price_rows:
        try:
            fv = json.loads(row.get("feature_vector_json") or "{}")
        except Exception:
            fv = {}
        if not isinstance(fv, dict):
            continue
        age = fv.get("age_minutes")
        if age is not None and isinstance(age, (int, float)) and age < 60:
            under_60m += 1
        if fv.get("age_missing"):
            age_missing += 1
        src = str(fv.get("price_change_source") or fv.get("price_change_1h_pct_source") or "missing")
        source_counts[src] = source_counts.get(src, 0) + 1
        sw_src = str(fv.get("swaps_source") or "missing")
        swaps_src_counts[sw_src] = swaps_src_counts.get(sw_src, 0) + 1

    result["under_60m_count"] = under_60m
    result["age_parse_missing_count"] = age_missing
    result["price_change_source_counts"] = dict(source_counts)
    result["swaps_source_counts"] = dict(swaps_src_counts)

    warnings: List[str] = []
    if age_missing > 0:
        warnings.append(f"{age_missing}条记录无法解析创建时间(age_missing=true)")
    sm = source_counts.get("missing", 0)
    if sm > 0:
        warnings.append(f"{sm}条price_change记录缺失source")
    sw_m = swaps_src_counts.get("missing", 0)
    if sw_m > 0:
        warnings.append(f"{sw_m}条swaps记录缺失source")
    result["warnings"] = warnings
    return result


async def _build_price_face_health(repo: Repositories, lower_bound: str, upper_bound: str, has_window: bool) -> Dict[str, Any]:
    """Diagnose price-filter-specific field coverage from pass_fail_detail_json and feature_vector_json."""
    result: Dict[str, Any] = {
        "latest_price_ok_rate": None,
        "holder_endpoint_ok_rate": None,
        "pass_fail_stats": {},
        "feature_vector_field_missing": {},
        "warnings": [],
    }
    if not has_window:
        return result

    price_rows = await _fetch_all(repo,
        "SELECT pass_fail_detail_json, feature_vector_json FROM token_strategy_matches WHERE stage='price_filter' AND created_at>=? AND created_at<=?",
        (lower_bound, upper_bound))
    if not price_rows:
        return result

    total = len(price_rows)
    rule_stats: Dict[str, Dict[str, int]] = {}
    field_missing: Dict[str, int] = defaultdict(int)

    for row in price_rows:
        try:
            details = json.loads(row.get("pass_fail_detail_json") or "[]")
        except Exception:
            details = []
        try:
            fv = json.loads(row.get("feature_vector_json") or "{}")
        except Exception:
            fv = {}

        if isinstance(details, list):
            for d in details:
                if not isinstance(d, dict):
                    continue
                rule = str(d.get("rule") or "unknown")
                if rule not in rule_stats:
                    rule_stats[rule] = {"total": 0, "passed": 0, "failed": 0, "missing": 0, "reasons": []}
                rs = rule_stats[rule]
                rs["total"] += 1
                if d.get("passed"):
                    rs["passed"] += 1
                else:
                    rs["failed"] += 1
                    reason = str(d.get("reason") or d.get("source") or "")[:60]
                    if reason and reason not in rs["reasons"]:
                        rs["reasons"].append(reason)
                    if d.get("missing") or d.get("age_missing"):
                        rs["missing"] += 1

        if isinstance(fv, dict):
            for key in ("swaps_5m", "swaps_1h", "price_change_1h_pct", "current_price"):
                if fv.get(key) is None or fv.get(key) == "":
                    field_missing[key] += 1

    for rule, rs in rule_stats.items():
        rs["fail_rate"] = round(rs["failed"] / max(rs["total"], 1), 3)
        rs["missing_rate"] = round(rs["missing"] / max(rs["total"], 1), 3)

    for key, missing in field_missing.items():
        field_missing[key] = round(missing / total, 3)

    latest_price_rows = await _fetch_all(repo,
        "SELECT ok FROM provider_requests WHERE provider='GMGN' AND endpoint LIKE '%token/info%' AND created_at>=? AND created_at<=?",
        (lower_bound, upper_bound))
    if latest_price_rows:
        ok_count = sum(1 for r in latest_price_rows if int(r.get("ok") or 0))
        result["latest_price_ok_rate"] = round(ok_count / len(latest_price_rows), 3)

    holder_rows = await _fetch_all(repo,
        "SELECT ok FROM provider_requests WHERE provider='GMGN' AND endpoint LIKE '%holders%' AND created_at>=? AND created_at<=?",
        (lower_bound, upper_bound))
    if holder_rows:
        ok_count = sum(1 for r in holder_rows if int(r.get("ok") or 0))
        result["holder_endpoint_ok_rate"] = round(ok_count / len(holder_rows), 3)

    result["pass_fail_stats"] = dict(rule_stats)
    result["feature_vector_field_missing"] = dict(field_missing)

    if result["latest_price_ok_rate"] is not None and result["latest_price_ok_rate"] < 0.9:
        result["warnings"].append(f"latest price token/info endpoint ok_rate={result['latest_price_ok_rate']}")
    if result["holder_endpoint_ok_rate"] is not None and result["holder_endpoint_ok_rate"] < 0.9:
        result["warnings"].append(f"holder endpoint ok_rate={result['holder_endpoint_ok_rate']}")

    return result


async def _build_platform_health(repo: Repositories, lower_bound: str, upper_bound: str, has_window: bool, trench_history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    platform_items: List[Dict[str, Any]] = []
    # trench_history is oldest→newest; iterate reversed so we pick the latest round
    for th in reversed(trench_history):
        pf_meta = th.get("platform_fetch") or {}
        if pf_meta.get("mode") == "platform_sharded":
            items = pf_meta.get("items", [])
            for item in items:
                if isinstance(item, dict):
                    ok = bool(item.get("ok", False))
                    raw_count = item.get("raw_count", 0)
                    fallback = bool(item.get("fallback_used", False))
                    if not ok:
                        severity = "critical"
                    elif raw_count == 0:
                        severity = "warn"
                    elif fallback:
                        severity = "warn"
                    else:
                        severity = "ok"
                    platform_items.append({
                        "platform": item.get("platform", ""),
                        "primary_slot": item.get("primary_slot"),
                        "used_slot": item.get("used_slot"),
                        "used_role": item.get("used_role", "primary"),
                        "ok": ok,
                        "raw_count": raw_count,
                        "unique_count": item.get("unique_count", 0),
                        "duplicate_count": item.get("duplicate_count", 0),
                        "fallback_used": fallback,
                        "error": item.get("error"),
                        "severity": severity,
                        "latency_ms": item.get("latency_ms"),
                    })
            break
    if not platform_items and has_window:
        rows = await _fetch_all(repo,
            "SELECT request_summary_json, ok, status_code FROM provider_requests WHERE provider='GMGN' AND endpoint LIKE '%trenches%' AND created_at>=? AND created_at<=?",
            (lower_bound, upper_bound))
        for row in rows:
            try:
                req = json.loads(row.get("request_summary_json") or "{}")
            except Exception:
                req = {}
            if isinstance(req, dict) and "platforms" in req:
                platforms_in = req.get("platforms", [])
                if isinstance(platforms_in, list) and len(platforms_in) == 1:
                    platform_items.append({
                        "platform": str(platforms_in[0]),
                        "ok": bool(row.get("ok")),
                        "raw_count": -1,
                        "severity": "ok" if bool(row.get("ok")) else "critical",
                    })

    summary = {"platform_sharding_enabled": len(platform_items) > 0}
    # Attach to meta
    return platform_items


async def _build_system_event_warnings(repo: Repositories, lower_bound: str, upper_bound: str, has_window: bool) -> List[Dict[str, Any]]:
    if not has_window:
        return []
    rows = await _fetch_all(repo,
        """SELECT message, context_json, created_at FROM system_events
           WHERE level IN ('ERROR', 'WARNING') AND category = 'DISCOVERY' AND created_at >= ? AND created_at <= ?
           ORDER BY created_at DESC LIMIT 20""",
        (lower_bound, upper_bound))
    results: List[Dict[str, Any]] = []
    for row in rows:
        msg = str(row.get("message", "") or "")
        if any(kw in msg for kw in ("fetch_trenches failed", "price screen fetch failed", "risk filter exception", "price filter exception", "fetch failed")):
            try:
                ctx = json.loads(row.get("context_json", "{}"))
            except Exception:
                ctx = {}
            results.append({"message": msg, "context": ctx, "created_at": row.get("created_at")})
    return results[:10]


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
        all_strategies = await repo.list_strategy_groups(include_disabled=True) if hasattr(repo, "list_strategy_groups") else []
        enabled_strategies = [s for s in all_strategies if int(s.get("enabled", 1))]
        summary = await _positions_summary(repo)
        session_started = (await _runtime_settings(repo)).get("session_started_at")

        # per-strategy screening stats (current session only)
        screening_summary = await _screening_summary(repo, enabled_strategies, since=session_started)

        # raw sample: one risk-passed-price-failed pool per enabled strategy
        raw_samples = await _raw_samples(repo, enabled_strategies)

        payload = {
            "export_type": "runtime_error_logs",
            "exported_at": utc_now_iso(),
            "session_started_at": session_started,
            "errors_deduped": errors,
            "positions_summary": summary,
            "strategy_groups": all_strategies,
            "screening_summary": screening_summary,
            "gmgn_raw_samples_by_strategy": raw_samples,
            "notes": [
                "screening_summary: per-strategy risk_filter & price_filter pass/fail counts scoped to current session (since session_started_at).",
                "gmgn_raw_samples_by_strategy: one pool per strategy that passed risk_filter but did not pass price_filter (most recent).",
            ],
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


async def _screening_summary(repo: Repositories, strategies: List[Dict[str, Any]], since: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not await _table_exists(repo, "token_strategy_matches"):
        return out
    time_clause = "AND created_at >= ?" if since else ""
    for sg in strategies:
        sid = int(sg.get("id"))
        params: tuple = (sid,) if not since else (sid, since)
        rows = await _fetch_all(
            repo,
            f"""
            SELECT stage,
                   COUNT(*) AS screened,
                   SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) AS passed,
                   SUM(CASE WHEN passed = 0 THEN 1 ELSE 0 END) AS failed
            FROM token_strategy_matches
            WHERE strategy_id = ? {time_clause}
            GROUP BY stage
            """,
            params,
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
                   AVG(realized_pnl_pct) AS avg_realized_pnl_pct
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
