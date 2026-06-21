from __future__ import annotations

import json
import math
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from ..config import ProviderMode, settings
from ..db.repositories import Repositories
from ..logging_config import logger
from ..strategy.thresholds import requires_smart_degen_for_x


BJ_TZ = timezone(timedelta(hours=8))

def now_beijing() -> datetime:
    return datetime.now(BJ_TZ)

def parse_beijing_time(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    s = str(value).strip()
    s = s.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        raise ValueError(f"Invalid Beijing datetime: {value}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BJ_TZ)
    else:
        dt = dt.astimezone(BJ_TZ)
    return dt

def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BJ_TZ)
    return dt.astimezone(timezone.utc).isoformat()

def to_beijing_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJ_TZ).isoformat()

def resolve_beijing_window(
    payload: Dict[str, Any],
    *,
    default_hours: int,
    allow_all: bool = False,
) -> Dict[str, Any]:
    preset = str(payload.get("preset") or f"{default_hours}h").lower()
    if allow_all and preset == "all":
        return {
            "preset": "all",
            "is_all": True,
            "start_bj": None,
            "end_bj": None,
            "start_utc": None,
            "end_utc": None,
            "timezone": "Asia/Shanghai",
        }
    end_bj = parse_beijing_time(payload.get("end_at_beijing")) or now_beijing()
    start_bj = parse_beijing_time(payload.get("start_at_beijing"))
    if start_bj is None:
        if preset in ("12h", "12"):
            start_bj = end_bj - timedelta(hours=12)
        elif preset in ("24h", "24"):
            start_bj = end_bj - timedelta(hours=24)
        elif preset in ("48h", "48"):
            start_bj = end_bj - timedelta(hours=48)
        elif preset in ("7d", "7day", "7days"):
            start_bj = end_bj - timedelta(days=7)
        else:
            start_bj = end_bj - timedelta(hours=default_hours)
    if start_bj >= end_bj:
        raise ValueError("start_at_beijing must be earlier than end_at_beijing")
    return {
        "preset": preset,
        "is_all": False,
        "start_bj": start_bj,
        "end_bj": end_bj,
        "start_utc": to_utc_iso(start_bj),
        "end_utc": to_utc_iso(end_bj),
        "timezone": "Asia/Shanghai",
    }

NOISE_LOG_PATTERNS = (
    "Duplicate tokens detected across type shards",
)

def _is_noise_log(message: Any, category: Any = None) -> bool:
    msg = str(message or "")
    cat = str(category or "")
    if cat == "DISCOVERY" and "Duplicate tokens detected across type shards" in msg:
        return True
    return any(p in msg for p in NOISE_LOG_PATTERNS)


SMART_DEGEN_AUDIT_FIELDS = {
    "smart_degen_max_holder_address",
    "smart_degen_max_holder_pct",
    "smart_degen_max_holder_usd",
    "smart_degen_min_holder_address",
    "smart_degen_min_holder_pct",
    "smart_degen_min_holder_usd",
}


def _requires_smart_degen_for_x_safe(x_value: Any) -> bool:
    try:
        x = float(x_value if x_value is not None else settings.STRATEGY_DEFAULT_X)
    except Exception:
        x = float(settings.STRATEGY_DEFAULT_X)
    return requires_smart_degen_for_x(x)
from ..trading.audit_builder import ENTRY_AUDIT_REQUIRED_FIELDS

router = APIRouter(prefix="/api/runtime", tags=["runtime"])

LOG_EXPORT_DIR = Path("./logs")
OPEN_POSITION_EXCLUDED_STATUSES = ("CLOSED", "LEGACY_INVALID_CONFIG", "MIGRATION_NEEDED")
FINAL_TRADE_STATUSES = ("CONFIRMED", "EXECUTED", "FILLED", "SUCCESS", "SIMULATED")

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
    "swaps_1h_min": {"label": "过去一小时交易数", "stage": "price_filter", "section": "价格面及其他指标"},
    "volume_per_swap_1h": {"label": "1h单笔平均成交额", "stage": "price_filter", "section": "价格面及其他指标"},
    "price_range_24h_percentile": {"label": "24h价格区间分位", "stage": "price_filter", "section": "价格面及其他指标"},
    "price_change_1h": {"label": "1h价格涨幅不足", "stage": "price_filter", "section": "价格面及其他指标"},
    "smart_degen": {"label": "聪明钱指标不满足", "stage": "smart_degen_filter", "section": "价格面及其他指标"},
    "smart_degen_not_required": {"label": "聪明钱条件未启用(x>0.15)", "stage": "smart_degen_filter", "section": "observed_only"},
    "top1_holder_rate_observed": {"label": "TOP1持有率观测", "stage": "risk_filter", "section": "observed_only"},
    "data_unavailable": {"label": "数据不可用", "stage": "api_error", "section": "data_unavailable"},
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
    {"name": "模拟盘1", "x": settings.STRATEGY_DEFAULT_X, "is_live": False, "priority": 10},
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


def _safe_float(payload: Dict[str, Any], key: str, default: float) -> float:
    val = payload.get(key)
    if val is None or val == "":
        return float(default)
    return float(val)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "enabled"):
        return True
    if s in ("0", "false", "no", "off", "disabled"):
        return False
    return default


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


def _utc_to_beijing(iso_str: Optional[str]) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BJ_TZ).isoformat()
    except Exception:
        return iso_str


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
            ),
            None,
        )

        target = by_name or by_values
        if target is None:
            new_id = await repo.create_strategy_group(
                name=spec["name"],
                x=float(spec["x"]),
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
        if int(target.get("priority") or 0) != int(spec["priority"]):
            updates["priority"] = int(spec["priority"])

        if updates and hasattr(repo, "update_strategy_group"):
            await repo.update_strategy_group(int(target["id"]), updates)
            if any(key in updates for key in {"x"}) and hasattr(repo, "increment_config_version"):
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
        try:
            summary = await _positions_summary(repo)
        except Exception as exc:
            logger.warning("status _positions_summary failed", error=str(exc))
            summary = {"live_open_count": 0, "sim_open_count": 0, "total_open": 0}
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
            x=_safe_float(payload, "x", settings.STRATEGY_DEFAULT_X),
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
        "name", "enabled", "is_live", "priority", "config_version", "x",
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
            if any(k in updates for k in {"x", "raw_config_json"}):
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


@router.get("/trade-history")
async def trade_history(request: Request, account_type: str = "ALL", limit: int = 500, since_session: bool = False):
    repo, owned = await _get_repo(request)
    try:
        rows = await repo.list_trade_history(
            account_type=str(account_type or "ALL").upper(),
            limit=int(limit),
            since_session=bool(since_session),
        )
        return {"ok": True, "items": rows}
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
                "SELECT pass_fail_detail_json, stage, tsm.token_mint, COALESCE(NULLIF(de.pool_address,''), NULLIF(snap.pool_address,''), '') AS pool_addr FROM token_strategy_matches tsm LEFT JOIN token_metric_snapshots snap ON snap.id = tsm.snapshot_id LEFT JOIN discovery_events de ON de.id = tsm.discovery_event_id WHERE tsm.stage IN ('risk_filter','price_filter','top_holder_filter','smart_degen_filter') AND tsm.created_at >= ? AND tsm.created_at <= ?",
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


async def _count_stage_unique_pools(repo: Repositories, stage: str, passed: Optional[bool] = None, since: Optional[str] = None, until: Optional[str] = None) -> int:
    extra = ""
    params: List[Any] = [stage]
    if passed is True:
        extra = " AND tsm.passed=1"
    elif passed is False:
        extra = " AND tsm.passed=0"
    if since:
        extra += " AND tsm.created_at >= ?"
        params.append(since)
    if until:
        extra += " AND tsm.created_at < ?"
        params.append(until)
    row = await _fetch_one(repo,
        f"SELECT COUNT(DISTINCT tsm.token_mint || '|' || COALESCE(NULLIF(de.pool_address,''), NULLIF(snap.pool_address,''), '')) AS c FROM token_strategy_matches tsm LEFT JOIN token_metric_snapshots snap ON snap.id=tsm.snapshot_id LEFT JOIN discovery_events de ON de.id=tsm.discovery_event_id WHERE tsm.stage=?{extra}",
        tuple(params))
    return int((row or {}).get("c", 0))


async def _build_data_source_health(repo: Repositories, lower_bound: str, upper_bound: str, has_window: bool, trench_history: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"window_run_count": len(trench_history), "discovery_mode": "two_group_discovery", "stats_mode": "current_window_joint"}

    all_time_trench = await _count_all_time_unique_trench_pools(repo)
    summary["trench_total"] = all_time_trench
    summary["all_time_unique_pool_count"] = all_time_trench

    if has_window:
        risk_surface_checked_row = await _fetch_one(repo,
            "SELECT COUNT(DISTINCT discovery_event_id || '|' || strategy_id) AS c FROM token_strategy_matches WHERE stage='risk_filter' AND created_at>=? AND created_at<=?",
            (lower_bound, upper_bound))
        risk_surface_checked = int((risk_surface_checked_row or {}).get("c", 0))
        summary["risk_surface_checked_count"] = risk_surface_checked

        risk_surface_pass_row = await _fetch_one(repo,
            """SELECT COUNT(DISTINCT tsm.discovery_event_id || '|' || tsm.strategy_id) AS c
               FROM token_strategy_matches tsm
               WHERE tsm.stage='risk_filter' AND tsm.passed=1 AND tsm.created_at>=? AND tsm.created_at<=?
                 AND EXISTS (SELECT 1 FROM token_strategy_matches tsm2 WHERE tsm2.discovery_event_id=tsm.discovery_event_id AND tsm2.strategy_id=tsm.strategy_id AND tsm2.stage='top_holder_filter' AND tsm2.passed=1 AND tsm2.created_at>=? AND tsm2.created_at<=?)""",
            (lower_bound, upper_bound, lower_bound, upper_bound))
        risk_top_count = int((risk_surface_pass_row or {}).get("c", 0))

        # 对 risk+top 通过的每条记录，检查是否需要 smart_degen，分组合成 risk_surface_pass_count
        risk_top_rows = await _fetch_all(repo,
            """SELECT DISTINCT tsm.discovery_event_id AS eid, tsm.strategy_id AS sid, sg.x AS x
               FROM token_strategy_matches tsm
               LEFT JOIN strategy_groups sg ON sg.id = tsm.strategy_id
               WHERE tsm.stage='risk_filter' AND tsm.passed=1 AND tsm.created_at>=? AND tsm.created_at<=?
                 AND tsm.discovery_event_id IS NOT NULL AND tsm.strategy_id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM token_strategy_matches tsm2 WHERE tsm2.discovery_event_id=tsm.discovery_event_id AND tsm2.strategy_id=tsm.strategy_id AND tsm2.stage='top_holder_filter' AND tsm2.passed=1 AND tsm2.created_at>=? AND tsm2.created_at<=?)""",
            (lower_bound, upper_bound, lower_bound, upper_bound))

        smart_degen_passed_rows = await _fetch_all(repo,
            """SELECT DISTINCT tsm.discovery_event_id AS eid, tsm.strategy_id AS sid
               FROM token_strategy_matches tsm
               WHERE tsm.stage='smart_degen_filter' AND tsm.passed=1 AND tsm.created_at>=? AND tsm.created_at<=?
                 AND tsm.discovery_event_id IS NOT NULL AND tsm.strategy_id IS NOT NULL""",
            (lower_bound, upper_bound))
        smart_degen_passed = {(int(r["eid"]), int(r["sid"])) for r in smart_degen_passed_rows}

        risk_surface_keys = 0
        smart_degen_skipped = 0
        for r in risk_top_rows:
            key = (int(r["eid"]), int(r["sid"]))
            x_val = r.get("x")
            if not _requires_smart_degen_for_x_safe(x_val):
                risk_surface_keys += 1
                smart_degen_skipped += 1
            elif key in smart_degen_passed:
                risk_surface_keys += 1
        summary["risk_surface_pass_count"] = risk_surface_keys
        summary["smart_degen_skipped_not_required_count"] = smart_degen_skipped

        price_surface_checked_row = await _fetch_one(repo,
            "SELECT COUNT(DISTINCT discovery_event_id || '|' || strategy_id) AS c FROM token_strategy_matches WHERE stage='price_filter' AND created_at>=? AND created_at<=?",
            (lower_bound, upper_bound))
        price_surface_checked = int((price_surface_checked_row or {}).get("c", 0))
        summary["price_surface_checked_count"] = price_surface_checked

        price_surface_pass_row = await _fetch_one(repo,
            """SELECT COUNT(DISTINCT tsm.discovery_event_id || '|' || tsm.strategy_id) AS c
               FROM token_strategy_matches tsm
               WHERE tsm.stage='price_filter' AND tsm.passed=1 AND tsm.created_at>=? AND tsm.created_at<=?""",
            (lower_bound, upper_bound))
        summary["price_surface_pass_count"] = int((price_surface_pass_row or {}).get("c", 0))

        # entry_ready = risk_surface_keys(已含 smart_degen 条件) + price_filter passed
        price_pass_rows = await _fetch_all(repo,
            """SELECT DISTINCT tsm.discovery_event_id AS eid, tsm.strategy_id AS sid
               FROM token_strategy_matches tsm
               WHERE tsm.stage='price_filter' AND tsm.passed=1 AND tsm.created_at>=? AND tsm.created_at<=?
                 AND tsm.discovery_event_id IS NOT NULL AND tsm.strategy_id IS NOT NULL""",
            (lower_bound, upper_bound))
        price_pass_keys = {(int(r["eid"]), int(r["sid"])) for r in price_pass_rows}

        risk_top_keys_set = set()
        for r in risk_top_rows:
            key = (int(r["eid"]), int(r["sid"]))
            x_val = r.get("x")
            if not _requires_smart_degen_for_x_safe(x_val):
                risk_top_keys_set.add(key)
            elif key in smart_degen_passed:
                risk_top_keys_set.add(key)

        entry_ready_count = len(risk_top_keys_set & price_pass_keys)
        summary["entry_ready_count"] = entry_ready_count

        pos_rows = await _fetch_all(repo,
            "SELECT account_type, COUNT(*) AS c FROM positions WHERE opened_at>=? AND opened_at<=? GROUP BY account_type",
            (lower_bound, upper_bound))
        sim_pos = 0
        live_pos = 0
        for pr in pos_rows:
            if pr.get("account_type") == "SIM":
                sim_pos = int(pr.get("c", 0))
            elif pr.get("account_type") == "LIVE":
                live_pos = int(pr.get("c", 0))
        total_pos = sim_pos + live_pos
        summary["positions_created_count"] = total_pos
        summary["sim_positions_created_count"] = sim_pos
        summary["live_positions_created_count"] = live_pos

        if entry_ready_count == 0 and total_pos > 0:
            summary["severity"] = "critical"
            summary["severity_detail"] = f"entry_ready_count=0 but {total_pos} positions created in window (sim={sim_pos}, live={live_pos})"

        rl_429 = await _fetch_one(repo,
            "SELECT COUNT(*) AS c FROM provider_requests WHERE provider='GMGN' AND status_code=429 AND created_at>=? AND created_at<=?",
            (lower_bound, upper_bound))
        summary["total_429_count"] = int((rl_429 or {}).get("c", 0))
    else:
        summary["risk_surface_checked_count"] = 0
        summary["risk_surface_pass_count"] = 0
        summary["price_surface_checked_count"] = 0
        summary["price_surface_pass_count"] = 0
        summary["entry_ready_count"] = 0
        summary["smart_degen_skipped_not_required_count"] = 0
        summary["positions_created_count"] = 0
        summary["sim_positions_created_count"] = 0
        summary["live_positions_created_count"] = 0

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
                    is_empty = bool(g.get("empty", False))
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
                        "empty": is_empty,
                        "severity": "warn" if is_empty else ("critical" if not ok else ("warn" if raw_count == 0 else "ok")),
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
                if cred.is_disabled():
                    severity = "critical"
                elif ok_rate < 0.5:
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
                "disabled_until": cred.disabled_until if cred.is_disabled() else None,
                "disabled_remaining_s": round(cred.disabled_remaining(), 1) if cred.is_disabled() else None,
                "disabled_reason": cred.disabled_reason if cred.is_disabled() else None,
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
        {"stage": "risk_filter", "label": "风控面 AND-1: Trenches本地", "endpoint_filter": "%v1/trenches%", "weight": 3},
        {"stage": "top_holder_filter", "label": "风控面 AND-2: Top1 Holder", "endpoint_filter": "%v1/market/token_top_holders%", "weight": 5, "exclude_tag": "smart_degen"},
        {"stage": "smart_degen_filter", "label": "风控面 AND-3: Smart Degen", "endpoint_filter": "%v1/market/token_top_holders%", "weight": 5, "require_tag": "smart_degen"},
        {"stage": "price_filter", "label": "价格面：活跃度与价格面", "endpoint_filter": "%v1/token/info%", "weight": 1},
    ]
    result: List[Dict[str, Any]] = []
    for s in stages:
        stage = s["stage"]
        if stage == "risk_filter":
            total = summary.get("risk_surface_checked_count") or await _count_stage_unique_pools(
                repo, stage, passed=None, since=lower_bound, until=upper_bound
            )
            passed = await _count_stage_unique_pools(repo, stage, passed=True, since=lower_bound, until=upper_bound)
        else:
            total = await _count_stage_unique_pools(repo, stage, passed=None, since=lower_bound, until=upper_bound)
            passed = await _count_stage_unique_pools(repo, stage, passed=True, since=lower_bound, until=upper_bound)
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

        item: Dict[str, Any] = {
            "stage": stage, "label": s["label"], "endpoint": s["endpoint_filter"].replace('%',''), "weight": s["weight"],
            "candidates_in": total, "checked_count": total, "passed_count": passed,
            "failed_count": failed, "skipped_count": 0,
            "api_calls": api_calls, "ok_rate": ok_rate_val, "rate_limited_count": rate_limited,
            "avg_latency_ms": 0, "severity": severity,
        }

        if stage == "price_filter" and has_window:
            kline_api_row = await _fetch_one(repo, """
                SELECT COUNT(*) AS c FROM provider_requests
                WHERE provider='GMGN' AND endpoint LIKE '%market/token_kline%'
                  AND created_at>=? AND created_at<=?""",
                (lower_bound, upper_bound))
            item["kline_api_calls"] = int((kline_api_row or {}).get("c", 0))

            invalid_row = await _fetch_one(repo, """
                SELECT COUNT(*) AS c FROM token_strategy_matches
                WHERE stage='price_filter' AND passed=0
                  AND created_at>=? AND created_at<?
                  AND (pass_fail_detail_json LIKE '%kline_data_quality%'
                       OR pass_fail_detail_json LIKE '%kline_validation_pass%'
                       OR pass_fail_detail_json LIKE '%data_unavailable%')
            """, (lower_bound, upper_bound))
            item["kline_invalid_or_missing_count"] = int((invalid_row or {}).get("c", 0))

        result.append(item)
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
        ("renounced_mint", "mint renounce"),
        ("renounced_freeze_account", "freeze renounce"), ("max_rug_ratio", "rug比例"),
        ("max_entrapment_ratio", "entrapment比例"), ("is_wash_trading", "wash trading"),
        ("rat_trader_amount_rate", "rat trader"), ("suspected_insider_hold_rate", "内幕持仓率"),
        ("max_bundler_rate", "bundler比例"), ("fresh_wallet_rate", "新钱包比例"),
        ("sell_tax", "卖税"), ("has_social", "社交"), ("burn_status", "burn状态"),
        ("sniper_count", "sniper数量"), ("launchpad", "平台"), ("market_cap", "市值"),
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
           WHERE level IN ('ERROR', 'WARNING', 'WARN', 'CRITICAL') AND category = 'DISCOVERY' AND created_at >= ? AND created_at <= ?
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


@router.post("/emergency/export-trade-audit")
async def export_trade_audit(request: Request, payload: Dict[str, Any] = Body(default={})):
    repo, owned = await _get_repo(request)
    try:
        payload = payload or {}
        acct = str(payload.get("account_type") or "ALL").upper()
        if acct not in {"ALL", "SIM", "LIVE"}:
            return JSONResponse({"ok": False, "error": "account_type must be ALL, SIM, or LIVE"}, status_code=400)

        window = resolve_beijing_window(payload, default_hours=24, allow_all=True)
        is_all = bool(window["is_all"])

        LOG_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        status_placeholders = ",".join(["?"] * len(FINAL_TRADE_STATUSES))

        # Find positions in window or all positions
        if is_all:
            if acct == "ALL":
                positions = await _fetch_all(repo, """
                    SELECT p.*, t.symbol, t.name, sg.name AS strategy_name
                    FROM positions p
                    LEFT JOIN tokens t ON t.token_mint=p.token_mint
                    LEFT JOIN strategy_groups sg ON sg.id=p.live_strategy_id
                    ORDER BY COALESCE(p.opened_at, p.updated_at) DESC, p.id DESC
                """)
            else:
                positions = await _fetch_all(repo, """
                    SELECT p.*, t.symbol, t.name, sg.name AS strategy_name
                    FROM positions p
                    LEFT JOIN tokens t ON t.token_mint=p.token_mint
                    LEFT JOIN strategy_groups sg ON sg.id=p.live_strategy_id
                    WHERE p.account_type=?
                    ORDER BY COALESCE(p.opened_at, p.updated_at) DESC, p.id DESC
                """, (acct,))
        else:
            start_utc = window["start_utc"]
            end_utc = window["end_utc"]
            params: List[Any] = []
            if acct != "ALL":
                params.append(acct)
            params.extend([start_utc, end_utc])
            params.extend([start_utc, end_utc])
            params.extend(list(FINAL_TRADE_STATUSES))
            params.extend([start_utc, end_utc])

            account_clause = "AND p.account_type=?" if acct != "ALL" else ""
            positions = await _fetch_all(repo, f"""
                SELECT DISTINCT p.*, t.symbol, t.name, sg.name AS strategy_name
                FROM positions p
                LEFT JOIN tokens t ON t.token_mint=p.token_mint
                LEFT JOIN strategy_groups sg ON sg.id=p.live_strategy_id
                WHERE 1=1
                  {account_clause}
                  AND (
                    (p.opened_at IS NOT NULL AND p.opened_at >= ? AND p.opened_at < ?)
                    OR
                    (p.closed_at IS NOT NULL AND p.closed_at >= ? AND p.closed_at < ?)
                    OR
                    EXISTS (
                      SELECT 1 FROM trade_events te
                      WHERE te.position_id=p.id
                        AND te.status IN ({status_placeholders})
                        AND te.side IN ('BUY','SELL')
                        AND te.created_at >= ?
                        AND te.created_at < ?
                    )
                  )
                ORDER BY COALESCE(p.opened_at, p.updated_at) DESC, p.id DESC
            """, tuple(params))

        def event_in_window(ev: dict) -> bool:
            if is_all:
                return True
            ts = str(ev.get("created_at") or "")
            if not ts:
                return False
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts_utc = dt.astimezone(timezone.utc).isoformat()
                return window["start_utc"] <= ts_utc < window["end_utc"]
            except Exception:
                return False

        position_items: List[Dict[str, Any]] = []
        sim_window_pnl = 0.0
        live_window_pnl = 0.0
        sim_lifecycle_pnl = 0.0
        live_lifecycle_pnl = 0.0
        total_window_events = 0
        total_all_events = 0

        for position in positions:
            pid = int(position["id"])
            account_type = str(position.get("account_type") or "SIM")
            token_mint = str(position.get("token_mint") or "")

            all_events_raw = await _fetch_all(repo, f"""
                SELECT te.*, t.symbol, t.name,
                       COALESCE(te.executed_token_amount, te.requested_token_amount) AS token_amount
                FROM trade_events te
                LEFT JOIN tokens t ON t.token_mint=te.token_mint
                WHERE te.position_id=?
                  AND te.status IN ({status_placeholders})
                  AND te.side IN ('BUY','SELL')
                ORDER BY te.created_at ASC, te.id ASC
            """, tuple([pid] + list(FINAL_TRADE_STATUSES)))

            window_events_raw = [e for e in all_events_raw if event_in_window(e)]

            def serialize_events(evs: list) -> list:
                out = []
                for e in evs:
                    created_at_utc = str(e.get("created_at") or "")
                    created_at_beijing = _utc_to_beijing(created_at_utc)
                    side = str(e.get("side") or "")
                    trade_value_raw = float(e.get("trade_value_usd_net") or 0)
                    trade_value_usd_net = round(-abs(trade_value_raw) if side == "BUY" else abs(trade_value_raw), 6)
                    out.append({
                        "side": side,
                        "created_at_utc": created_at_utc,
                        "created_at_beijing": created_at_beijing,
                        "price_usd": e.get("price_usd"),
                        "token_amount": e.get("token_amount"),
                        "trade_value_usd_net": trade_value_usd_net,
                        "exit_reason_code": e.get("exit_reason"),
                        "exit_reason_label": e.get("exit_reason_label"),
                    })
                return out

            def calc_pnl(evs: list) -> dict:
                buys = sum(abs(float(e.get("trade_value_usd_net") or 0)) for e in evs if str(e.get("side") or "") == "BUY")
                sells = sum(abs(float(e.get("trade_value_usd_net") or 0)) for e in evs if str(e.get("side") or "") == "SELL")
                return {
                    "buy_value_usd": round(buys, 6),
                    "sell_value_usd": round(sells, 6),
                    "pnl_usd": round(sells - buys, 6),
                    "buy_count": len([e for e in evs if str(e.get("side") or "") == "BUY"]),
                    "sell_count": len([e for e in evs if str(e.get("side") or "") == "SELL"]),
                }

            window_pnl = calc_pnl(window_events_raw)
            lifecycle_pnl = calc_pnl(all_events_raw)

            if account_type == "LIVE":
                live_window_pnl += window_pnl["pnl_usd"]
                live_lifecycle_pnl += lifecycle_pnl["pnl_usd"]
            else:
                sim_window_pnl += window_pnl["pnl_usd"]
                sim_lifecycle_pnl += lifecycle_pnl["pnl_usd"]

            total_window_events += len(window_events_raw)
            total_all_events += len(all_events_raw)

            item = {
                "position_id": pid,
                "account_type": account_type,
                "symbol": position.get("symbol") or "",
                "token_mint": token_mint,
                "strategy_name": position.get("strategy_name") or "",
                "opened_at_utc": position.get("opened_at"),
                "opened_at_beijing": _utc_to_beijing(position.get("opened_at")),
                "closed_at_utc": position.get("closed_at"),
                "closed_at_beijing": _utc_to_beijing(position.get("closed_at")),
                "window_pnl": window_pnl,
                "lifecycle_pnl": lifecycle_pnl,
                "trade_events_in_window": serialize_events(window_events_raw),
                "trade_events_all": serialize_events(all_events_raw),
            }
            position_items.append(item)

        # Orphan trade events
        orphan_events = []
        if payload.get("include_orphan_trade_events", True):
            orphan_params: List[Any] = list(FINAL_TRADE_STATUSES)
            orphan_sql = f"""
                SELECT te.*, t.symbol, t.name,
                       COALESCE(te.executed_token_amount, te.requested_token_amount) AS token_amount
                FROM trade_events te
                LEFT JOIN tokens t ON t.token_mint=te.token_mint
                WHERE te.position_id IS NULL
                  AND te.status IN ({status_placeholders})
                  AND te.side IN ('BUY','SELL')
            """
            if acct != "ALL":
                orphan_sql += " AND te.account_type=?"
                orphan_params.append(acct)
            if not is_all:
                orphan_sql += " AND te.created_at >= ? AND te.created_at < ?"
                orphan_params.append(window["start_utc"])
                orphan_params.append(window["end_utc"])
            orphan_sql += " ORDER BY te.created_at ASC"
            orphan_rows = await _fetch_all(repo, orphan_sql, tuple(orphan_params))
            for e in orphan_rows:
                created_at_utc = str(e.get("created_at") or "")
                orphan_events.append({
                    "id": e.get("id"),
                    "side": e.get("side"),
                    "account_type": e.get("account_type"),
                    "token_mint": e.get("token_mint"),
                    "symbol": e.get("symbol"),
                    "price_usd": e.get("price_usd"),
                    "token_amount": e.get("token_amount"),
                    "trade_value_usd_net": e.get("trade_value_usd_net"),
                    "created_at_utc": created_at_utc,
                    "created_at_beijing": _utc_to_beijing(created_at_utc),
                    "status": e.get("status"),
                    "exit_reason": e.get("exit_reason"),
                })

        # Diagnostics
        try:
            diag_status_rows = await _fetch_all(repo, f"""
                SELECT account_type, side, status, COUNT(*) AS cnt
                FROM trade_events
                WHERE side IN ('BUY','SELL')
                  AND status IN ({status_placeholders})
                GROUP BY account_type, side, status
            """, tuple(FINAL_TRADE_STATUSES))
            trade_events_by_status = {}
            for r in diag_status_rows:
                key = f"{r.get('account_type')}|{r.get('side')}|{r.get('status')}"
                trade_events_by_status[key] = r.get("cnt")

            null_pos_buy_sell = await _fetch_one(repo, f"""
                SELECT COUNT(*) AS c FROM trade_events
                WHERE side IN ('BUY','SELL')
                  AND status IN ({status_placeholders})
                  AND position_id IS NULL
            """, tuple(FINAL_TRADE_STATUSES))

            pos_without_te = await _fetch_one(repo, """
                SELECT COUNT(*) AS c FROM positions p
                WHERE NOT EXISTS (
                    SELECT 1 FROM trade_events te WHERE te.position_id=p.id AND te.side IN ('BUY','SELL')
                )
            """)
        except Exception:
            trade_events_by_status = {}
            null_pos_buy_sell = {"c": 0}
            pos_without_te = {"c": 0}

        payload_out = {
            "export_type": "trade_audit",
            "timezone": "Asia/Shanghai",
            "window": {
                "preset": window["preset"],
                "is_all": is_all,
                "start_at_beijing": window["start_bj"].isoformat() if window["start_bj"] else None,
                "end_at_beijing": window["end_bj"].isoformat() if window["end_bj"] else None,
                "start_at_utc": window["start_utc"],
                "end_at_utc": window["end_utc"],
            },
            "exported_at_beijing": now_beijing().isoformat(),
            "exported_at_utc": utc_now_iso(),
            "summary": {
                "window_sim_pnl_usd": round(sim_window_pnl, 6),
                "window_live_pnl_usd": round(live_window_pnl, 6),
                "lifecycle_sim_pnl_usd": round(sim_lifecycle_pnl, 6),
                "lifecycle_live_pnl_usd": round(live_lifecycle_pnl, 6),
                "positions_exported": len(position_items),
                "window_trade_events_count": total_window_events,
                "all_trade_events_count": total_all_events,
            },
            "positions": position_items,
            "orphan_trade_events": orphan_events,
            "diagnostics": {
                "trade_events_by_status": trade_events_by_status,
                "null_position_id_buy_sell_events": int((null_pos_buy_sell or {}).get("c", 0)),
                "positions_without_trade_events": int((pos_without_te or {}).get("c", 0)),
            },
        }

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = LOG_EXPORT_DIR / f"trade_audit_{ts}.json"
        text = json.dumps(payload_out, ensure_ascii=False, indent=2, default=str)
        try:
            import asyncio as _asyncio
            await _asyncio.to_thread(out.write_text, text, encoding="utf-8")
        except Exception:
            out.write_text(text, encoding="utf-8")
        return {"ok": True, "export_path": str(out), "path": str(out), "data": payload_out}
    except Exception as exc:
        logger.exception("export_trade_audit failed")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    finally:
        if owned:
            await repo.close()


@router.post("/emergency/export-losing")
async def export_losing(request: Request):
    return await export_trade_audit(request)


@router.post("/emergency/export-logs")
async def export_logs(request: Request):
    repo, owned = await _get_repo(request)
    try:
        LOG_EXPORT_DIR.mkdir(parents=True, exist_ok=True)

        window = resolve_beijing_window({}, default_hours=12, allow_all=False)
        start_utc = window["start_utc"]
        end_utc = window["end_utc"]

        session_started = (await _runtime_settings(repo)).get("session_started_at")

        dup_issues, warning_count, error_count, critical_count = await _deduped_issues(
            repo, since=start_utc, until=end_utc
        )

        provider_failures, provider_summary = await _deduped_provider_failures(
            repo, since=start_utc, until=end_utc
        )

        raw_events = await _fetch_all(repo,
            """SELECT level, category, message, context_json, account_type, created_at
               FROM system_events
               WHERE level IN ('WARNING','WARN','ERROR','CRITICAL')
                 AND created_at >= ?
                 AND created_at < ?
               ORDER BY id DESC LIMIT 200""",
            (start_utc, end_utc))

        raw_events = [
            r for r in raw_events
            if not _is_noise_log(r.get("message"), r.get("category"))
        ]

        raw_provider_requests = await _fetch_all(repo,
            """SELECT provider, endpoint, method, status_code, ok, error_code, error_summary,
                      request_summary_json, response_summary_json, created_at
               FROM provider_requests
               WHERE ok = 0
                 AND created_at >= ?
                 AND created_at < ?
               ORDER BY id DESC LIMIT 500""",
            (start_utc, end_utc))

        payload = {
            "export_type": "runtime_warning_error_logs",
            "timezone": "Asia/Shanghai",
            "window": {
                "fixed": True,
                "hours": 12,
                "start_at_beijing": window["start_bj"].isoformat(),
                "end_at_beijing": window["end_bj"].isoformat(),
                "start_at_utc": start_utc,
                "end_at_utc": end_utc,
            },
            "session_started_at_utc": session_started,
            "exported_at_beijing": now_beijing().isoformat(),
            "exported_at_utc": utc_now_iso(),
            "summary": {
                "warning_count": warning_count,
                "error_count": error_count,
                "critical_count": critical_count,
                "deduped_issue_count": len(dup_issues),
                "provider_failed_request_count": provider_summary.get("total_failed", 0),
                "gmgn_auth_replay_count": provider_summary.get("auth_replay_count", 0),
                "gmgn_401_count": provider_summary.get("auth_401_count", 0),
                "gmgn_429_count": provider_summary.get("rate_429_count", 0),
            },
            "issues_deduped": dup_issues,
            "provider_failures_deduped": provider_failures,
            "raw_system_events": [
                {"level": r.get("level"), "category": r.get("category"), "message": r.get("message"),
                 "created_at": r.get("created_at"), "context": _safe_json_loads(r.get("context_json"), {})}
                for r in raw_events
            ],
            "raw_provider_failures": [
                {"provider": r.get("provider"), "endpoint": r.get("endpoint"), "method": r.get("method"),
                 "status_code": r.get("status_code"), "error_code": r.get("error_code"),
                 "error_summary": r.get("error_summary"), "created_at": r.get("created_at"),
                 "request": _safe_json_loads(r.get("request_summary_json"), {}),
                 "response": _safe_json_loads(r.get("response_summary_json"), {})}
                for r in raw_provider_requests
            ],
        }
        out = LOG_EXPORT_DIR / f"runtime_logs_last12h_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        try:
            import asyncio as _asyncio
            await _asyncio.to_thread(out.write_text, text, encoding="utf-8")
        except Exception:
            out.write_text(text, encoding="utf-8")
        return {
            "ok": True, "export_path": str(out), "path": str(out),
            "warning_count": warning_count, "error_count": error_count, "critical_count": critical_count,
            "issue_count": len(dup_issues),
        }
    except Exception as e:
        logger.exception("export-logs failed")
        return {"ok": False, "error": str(e), "error_count": 0, "critical_count": 0, "export_path": ""}
    finally:
        if owned:
            await repo.close()


async def _deduped_issues(repo: Repositories, since: Optional[str] = None, until: Optional[str] = None) -> tuple:
    """Returns (issues_deduped, warning_count, error_count, critical_count)."""
    rows = await _fetch_all(repo,
        """SELECT level, category, message, context_json, account_type,
                  MIN(created_at) AS first_seen_at,
                  MAX(created_at) AS last_seen_at,
                  COUNT(*) AS count
           FROM system_events
            WHERE level IN ('WARNING','WARN','ERROR','CRITICAL')
              AND (? IS NULL OR created_at >= ?)
              AND (? IS NULL OR created_at < ?)
            GROUP BY level, category, message, context_json, account_type
            ORDER BY last_seen_at DESC LIMIT 500""",
        (since, since, until, until))

    issues = []
    warning_count = 0
    error_count = 0
    critical_count = 0
    seen_keys: Dict[str, int] = {}

    for row in rows:
        msg = str(row.get("message") or "")
        category = row.get("category")

        if _is_noise_log(msg, category):
            continue

        level = str(row.get("level") or "").upper()
        if level == "WARNING" or level == "WARN":
            warning_count += int(row.get("count") or 0)
        elif level == "ERROR":
            error_count += int(row.get("count") or 0)
        elif level == "CRITICAL":
            critical_count += int(row.get("count") or 0)

        ctx = _safe_json_loads(row.get("context_json"), {})
        norm_msg = re.sub(r"slot=\d+", "slot=*", msg)
        norm_msg = re.sub(r"All \d+ discovery", "All N discovery", norm_msg)

        status_code = ctx.get("status_code")
        gmgn_error = ctx.get("gmgn_error")
        trench_type = ctx.get("trench_type") or ctx.get("type")
        endpoint = ctx.get("endpoint") or ctx.get("path")

        dedupe_key = f"{level}|{row.get('category','')}|{norm_msg}|{status_code}|{gmgn_error}|{trench_type}|{endpoint}"
        if dedupe_key in seen_keys:
            idx = seen_keys[dedupe_key]
            issues[idx]["count"] += int(row.get("count") or 0)
            continue
        seen_keys[dedupe_key] = len(issues)

        suggested = _suggest_action(level, msg, status_code, gmgn_error)
        issues.append({
            "level": level,
            "category": row.get("category"),
            "message": msg[:500],
            "count": int(row.get("count") or 0),
            "first_seen_at": row.get("first_seen_at"),
            "last_seen_at": row.get("last_seen_at"),
            "status_code": status_code,
            "gmgn_error": gmgn_error,
            "trench_type": trench_type,
            "endpoint": endpoint,
            "suggested_action": suggested,
        })

    return issues, warning_count, error_count, critical_count


def _suggest_action(level: str, message: str, status_code: Any, gmgn_error: Any) -> str:
    gmgn_error_str = str(gmgn_error or "").lower()
    msg_lower = message.lower()

    if "auth_client_id_replayed" in gmgn_error_str or "client_id replayed" in msg_lower:
        return "client_id 被 GMGN 判定重复。使用每请求唯一 client_id，不要复用 GMGN_CLIENT_ID_N / public_key。"
    if (status_code in (401, 403, "401", "403")) or (gmgn_error_str and "401" in gmgn_error_str):
        return "检查 API key、IPv4、GMGN 账号权限。"
    if status_code in (429, "429") or "rate limit" in msg_lower:
        return "检查 X-RateLimit-Reset / reset_at，暂停重试至 reset 时间。"
    if "local rate limiter" in msg_lower or "slot_cooldown" in msg_lower:
        return "本地 rate limiter 拦截，检查 slot 冷却和 endpoint weight。"
    if status_code in (500, 502, 503, 504, "500", "502", "503", "504") or "timeout" in msg_lower:
        return "GMGN 或网络异常，可稍后重试。"
    return ""


async def _deduped_provider_failures(repo: Repositories, since: Optional[str] = None, until: Optional[str] = None) -> tuple:
    """Returns (failures_deduped, summary_counts)."""
    rows = await _fetch_all(repo,
        """SELECT provider, endpoint, method, status_code, ok, error_code, error_summary,
                  request_summary_json, response_summary_json, created_at
           FROM provider_requests
            WHERE ok = 0 AND (? IS NULL OR created_at >= ?) AND (? IS NULL OR created_at < ?)
            ORDER BY id DESC LIMIT 1000""",
        (since, since, until, until))

    failures = []
    summary = {"auth_replay_count": 0, "auth_401_count": 0, "rate_429_count": 0, "total_failed": len(rows)}
    seen_keys: Dict[str, int] = {}

    for row in rows:
        status_code = row.get("status_code")
        error_code = str(row.get("error_code") or "")
        endpoint = str(row.get("endpoint") or "")
        error_summary_str = str(row.get("error_summary") or "")

        if "AUTH_CLIENT_ID_REPLAYED" in error_code:
            summary["auth_replay_count"] += 1
        if status_code in (401, 403):
            summary["auth_401_count"] += 1
        if status_code == 429:
            summary["rate_429_count"] += 1

        gmgn_error = ""
        if "AUTH_CLIENT_ID_REPLAYED" in error_code or "client_id replayed" in error_summary_str.lower():
            gmgn_error = "AUTH_CLIENT_ID_REPLAYED"

        req = _safe_json_loads(row.get("request_summary_json"), {})
        credential_slot = req.get("credential_slot")

        dedupe_key = f"{row.get('provider','')}|{endpoint}|{status_code}|{error_code}|{gmgn_error}|{error_summary_str[:80]}"
        if dedupe_key in seen_keys:
            idx = seen_keys[dedupe_key]
            failures[idx]["count"] += 1
            creds = failures[idx].get("credential_slots") or []
            if isinstance(credential_slot, int) and credential_slot not in creds:
                creds.append(credential_slot)
                failures[idx]["credential_slots"] = creds
            continue
        seen_keys[dedupe_key] = len(failures)

        suggested = _suggest_action("", str(row.get("error_summary") or "") or error_code, status_code, gmgn_error)
        failures.append({
            "provider": row.get("provider"),
            "endpoint": endpoint,
            "method": row.get("method"),
            "status_code": status_code,
            "error_code": error_code,
            "count": 1,
            "latest_error": error_summary_str[:300],
            "credential_slots": [credential_slot] if isinstance(credential_slot, int) else [],
            "suggested_action": suggested,
        })

    return failures, summary


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


@router.get("/pnl-summary")
async def pnl_summary(request: Request):
    repo, owned = await _get_repo(request)
    try:
        status_placeholders = ",".join(["?"] * len(FINAL_TRADE_STATUSES))
        rows = await _fetch_all(repo,
            f"""SELECT position_id, account_type, side, trade_value_usd_net,
                       COALESCE(executed_token_amount, requested_token_amount) AS token_amount
                FROM trade_events
                WHERE status IN ({status_placeholders})
                  AND side IN ('BUY','SELL')
                  AND position_id IS NOT NULL""",
            tuple(FINAL_TRADE_STATUSES))

        by_position = {
            "SIM": defaultdict(lambda: {"buy_value": 0.0, "sell_value": 0.0, "buy_amount": 0.0, "sell_amount": 0.0}),
            "LIVE": defaultdict(lambda: {"buy_value": 0.0, "sell_value": 0.0, "buy_amount": 0.0, "sell_amount": 0.0}),
        }

        for r in rows:
            acct = str(r.get("account_type") or "SIM").upper()
            if acct not in by_position:
                acct = "SIM"
            pid = int(r["position_id"])
            side = str(r.get("side") or "").upper()
            value = abs(float(r.get("trade_value_usd_net") or 0))
            amount = abs(float(r.get("token_amount") or 0))
            p = by_position[acct][pid]
            if side == "BUY":
                p["buy_value"] += value
                p["buy_amount"] += amount
            elif side == "SELL":
                p["sell_value"] += value
                p["sell_amount"] += amount

        def empty_bucket():
            return {
                "realized_pnl_usd": 0.0, "realized_pnl_pct": 0.0,
                "near_closed_positions": 0, "open_positions": 0,
                "closed_positions": 0, "losing_positions": 0, "winning_positions": 0,
                "closed_cost_basis": 0.0,
                "excluded_not_99pct_sold_positions": 0,
                "excluded_invalid_amount_positions": 0,
                "pnl_scope": "only_positions_with_sold_ratio_gt_0.99",
                "sold_ratio_threshold": ">0.99",
            }

        out = {"SIM": empty_bucket(), "LIVE": empty_bucket()}

        for acct in ("SIM", "LIVE"):
            for pid, p in by_position[acct].items():
                buy_amount = p["buy_amount"]
                sell_amount = p["sell_amount"]
                buy_value = p["buy_value"]
                sell_value = p["sell_value"]

                if buy_amount <= 0:
                    out[acct]["excluded_invalid_amount_positions"] += 1
                    continue

                sold_ratio = sell_amount / buy_amount

                if sold_ratio > 0.99:
                    pnl = sell_value - buy_value
                    out[acct]["realized_pnl_usd"] += pnl
                    out[acct]["near_closed_positions"] += 1
                    out[acct]["closed_cost_basis"] += buy_value
                    if pnl < 0:
                        out[acct]["losing_positions"] += 1
                    else:
                        out[acct]["winning_positions"] += 1
                else:
                    out[acct]["excluded_not_99pct_sold_positions"] += 1

        for acct in ("SIM", "LIVE"):
            cost = max(out[acct]["closed_cost_basis"], 0.000001)
            out[acct]["realized_pnl_pct"] = round(out[acct]["realized_pnl_usd"] / cost * 100, 2) if cost > 0 else 0.0
            out[acct]["realized_pnl_usd"] = round(out[acct]["realized_pnl_usd"], 6)

        pending_rpc_backfill_count = 0
        final_trade_count = 0
        estimated_trade_count = 0
        try:
            if await _table_exists(repo, "trade_events"):
                stats_rows = await _fetch_all(repo,
                    f"SELECT accounting_status, COUNT(*) AS cnt FROM trade_events WHERE status IN ({status_placeholders}) AND accounting_status IS NOT NULL GROUP BY accounting_status",
                    tuple(FINAL_TRADE_STATUSES))
                for sr in stats_rows:
                    s = str(sr.get("accounting_status") or "")
                    c = int(sr.get("cnt") or 0)
                    if s == "PENDING_RPC_BACKFILL":
                        pending_rpc_backfill_count = c
                    elif s == "FINAL":
                        final_trade_count += c
                    elif s == "ESTIMATED":
                        estimated_trade_count += c
        except Exception:
            pass

        return {
            "sim": {
                "realized_pnl_usd": out["SIM"]["realized_pnl_usd"],
                "unrealized_pnl_usd": 0.0,
                "total_pnl_usd": out["SIM"]["realized_pnl_usd"],
                "realized_pnl_pct": out["SIM"]["realized_pnl_pct"],
                "open_positions": 0,
                "closed_positions": out["SIM"]["near_closed_positions"],
                "losing_positions": out["SIM"]["losing_positions"],
                "winning_positions": out["SIM"]["winning_positions"],
                "sold_ratio_threshold": ">0.99",
                "excluded_not_99pct_sold": out["SIM"]["excluded_not_99pct_sold_positions"],
                "excluded_invalid_amount": out["SIM"]["excluded_invalid_amount_positions"],
            },
            "live": {
                "realized_pnl_usd": out["LIVE"]["realized_pnl_usd"],
                "unrealized_pnl_usd": 0.0,
                "total_pnl_usd": out["LIVE"]["realized_pnl_usd"],
                "realized_pnl_pct": out["LIVE"]["realized_pnl_pct"],
                "open_positions": 0,
                "closed_positions": out["LIVE"]["near_closed_positions"],
                "losing_positions": out["LIVE"]["losing_positions"],
                "winning_positions": out["LIVE"]["winning_positions"],
                "sold_ratio_threshold": ">0.99",
                "excluded_not_99pct_sold": out["LIVE"]["excluded_not_99pct_sold_positions"],
                "excluded_invalid_amount": out["LIVE"]["excluded_invalid_amount_positions"],
            },
            "accounting_status_summary": {
                "pending_rpc_backfill": pending_rpc_backfill_count,
                "final": final_trade_count,
                "estimated": estimated_trade_count,
            },
        }
    finally:
        if owned:
            await repo.close()


@router.get("/trade-events-ledger")
async def trade_events_ledger(request: Request, account_type: str = "ALL", since_session: bool = False, limit: int = 500):
    acct = str(account_type or "ALL").upper()
    if acct not in {"SIM", "LIVE", "ALL"}:
        return JSONResponse({"ok": False, "error": "account_type must be SIM, LIVE, or ALL"}, status_code=400)

    repo, owned = await _get_repo(request)
    try:
        if not await _table_exists(repo, "trade_events"):
            return {"ok": True, "items": [], "count": 0}

        where_parts = ["te.status='CONFIRMED'", "te.side IN ('BUY','SELL')"]
        params: List[Any] = []
        if acct != "ALL":
            where_parts.append("te.account_type=?")
            params.append(acct)
        if since_session:
            runtime = await _runtime_settings(repo)
            session_started = runtime.get("session_started_at")
            if session_started:
                where_parts.append("te.created_at >= ?")
                params.append(session_started)
        where_clause = " AND ".join(where_parts)
        params.append(int(limit))

        rows = await _fetch_all(repo,
            f"""SELECT te.*, t.symbol, t.name,
                       COALESCE(te.executed_token_amount, te.requested_token_amount) AS token_amount
                FROM trade_events te
                LEFT JOIN tokens t ON t.token_mint = te.token_mint
                WHERE {where_clause}
                ORDER BY te.created_at DESC LIMIT ?""",
            tuple(params))

        items: List[Dict[str, Any]] = []
        for row in rows:
            created_at_utc = str(row.get("created_at") or "")
            created_at_beijing = ""
            if created_at_utc:
                try:
                    dt = datetime.fromisoformat(created_at_utc.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    created_at_beijing = dt.astimezone(BJ_TZ).isoformat()
                except Exception:
                    created_at_beijing = created_at_utc

            token = str(row.get("token_mint") or "")
            mint_short = f"{token[:4]}...{token[-4:]}" if len(token) > 10 else token

            side = str(row.get("side") or "")
            trade_value_raw = float(row.get("trade_value_usd_net") or 0)
            trade_value_usd_net = round(-abs(trade_value_raw) if side == "BUY" else abs(trade_value_raw), 6)

            items.append({
                "trade_event_id": row.get("id"),
                "position_id": row.get("position_id"),
                "account_type": row.get("account_type"),
                "side": side,
                "event_type": row.get("event_type"),
                "created_at_utc": created_at_utc,
                "created_at_beijing": created_at_beijing,
                "token_mint": token,
                "mint_short": mint_short,
                "symbol": row.get("symbol"),
                "name": row.get("name"),
                "trade_value_usd_net": trade_value_usd_net,
                "price_usd": row.get("price_usd"),
                "token_amount": row.get("token_amount"),
                "exit_reason_code": row.get("exit_reason"),
                "exit_reason_label": row.get("exit_reason_label"),
                "quote_json": _safe_json_loads(row.get("quote_json"), row.get("quote_json")),
            })

        return {"ok": True, "items": items, "count": len(items)}
    finally:
        if owned:
            await repo.close()
