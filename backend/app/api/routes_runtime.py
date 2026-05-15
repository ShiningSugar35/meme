from __future__ import annotations

import json
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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    try:
        return dict(row)
    except Exception:
        return {str(i): v for i, v in enumerate(row)}


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


async def _column_exists(repo: Repositories, table: str, column: str) -> bool:
    async with repo.db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return any(_row_to_dict(r).get("name") == column or (len(r) > 1 and r[1] == column) for r in rows)


async def _set_runtime_mode(repo: Repositories, user_mode: str) -> Dict[str, Any]:
    await repo.set_runtime_setting("user_mode", user_mode, updated_by="api")
    return {"ok": True, "user_mode": user_mode, "provider_mode": settings.get_provider_mode().value}


@router.get("/status")
async def runtime_status(request: Request):
    repo, owned = await _get_repo(request)
    try:
        runtime = await repo.get_all_runtime_settings()
        user_mode = runtime.get("user_mode", "IDLE")
        return {
            "ok": True,
            "user_mode": user_mode,
            "provider_mode": settings.get_provider_mode().value,
            "dry_run": settings.DRY_RUN,
            "simulation_enabled": settings.SIMULATION_ENABLED,
            "db_path": settings.SQLITE_PATH,
            "log_export_dir": str(LOG_EXPORT_DIR),
        }
    finally:
        if owned:
            await repo.close()


@router.post("/mode")
async def set_mode(request: Request, payload: Dict[str, Any] = Body(...)):
    mode = str(payload.get("mode") or payload.get("user_mode") or "").strip().upper()
    if mode not in {"IDLE", "SIM_TEST", "FORMAL_SIM_LIVE"}:
        return JSONResponse({"ok": False, "error": "mode must be IDLE, SIM_TEST, or FORMAL_SIM_LIVE"}, status_code=400)
    repo, owned = await _get_repo(request)
    try:
        return await _set_runtime_mode(repo, mode)
    finally:
        if owned:
            await repo.close()


@router.post("/start-sim")
async def start_sim(request: Request):
    repo, owned = await _get_repo(request)
    try:
        return await _set_runtime_mode(repo, "SIM_TEST")
    finally:
        if owned:
            await repo.close()


@router.post("/start-formal")
async def start_formal(request: Request):
    repo, owned = await _get_repo(request)
    try:
        if settings.get_provider_mode() == ProviderMode.MOCK:
            return JSONResponse({"ok": False, "error": "PROVIDER_MODE=mock; formal mode requires online_readonly/live configuration"}, status_code=400)
        return await _set_runtime_mode(repo, "FORMAL_SIM_LIVE")
    finally:
        if owned:
            await repo.close()


@router.post("/stop")
async def stop_runtime(request: Request):
    repo, owned = await _get_repo(request)
    try:
        return await _set_runtime_mode(repo, "IDLE")
    finally:
        if owned:
            await repo.close()


@router.post("/emergency/stop-all")
async def emergency_stop_all(request: Request):
    repo, owned = await _get_repo(request)
    try:
        await _set_runtime_mode(repo, "IDLE")
        await repo.append_system_event("CRITICAL", "EMERGENCY", "Emergency stop all triggered", _json_dumps({"source": "api"}), account_type="SIM")
        return {"ok": True, "message": "runtime stopped"}
    finally:
        if owned:
            await repo.close()


@router.get("/strategies")
async def list_strategies(request: Request, include_disabled: bool = True):
    repo, owned = await _get_repo(request)
    try:
        if hasattr(repo, "list_strategy_groups"):
            return {"ok": True, "items": await repo.list_strategy_groups(include_disabled=include_disabled)}
        items = await _fetch_all(repo, "SELECT * FROM strategy_groups ORDER BY priority ASC, id ASC")
        return {"ok": True, "items": items}
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
            y=float(payload.get("y", 2.5)),
            t_seconds=int(payload.get("t_seconds", payload.get("t", 150))),
            is_live=bool(payload.get("is_live", False)),
            priority=int(payload.get("priority", 100)),
            raw_config_json=raw_config_json,
        )
        return {"ok": True, "id": strategy_id}
    finally:
        if owned:
            await repo.close()


@router.patch("/strategies/{strategy_id}")
async def update_strategy(strategy_id: int, request: Request, payload: Dict[str, Any] = Body(...)):
    allowed = {
        "name", "enabled", "is_live", "priority", "config_version", "x", "y", "t_seconds",
        "buy_slippage_cap_bps", "sell_slippage_cap_bps", "emergency_slippage_cap_bps",
        "price_impact_hard_cap_pct", "raw_config_json",
    }
    updates = {k: v for k, v in payload.items() if k in allowed}
    if "raw_config_json" in updates and not isinstance(updates["raw_config_json"], str):
        updates["raw_config_json"] = _json_dumps(updates["raw_config_json"])
    repo, owned = await _get_repo(request)
    try:
        await repo.update_strategy_group(strategy_id, updates)
        if any(k in updates for k in {"x", "y", "t_seconds", "raw_config_json"}):
            await repo.increment_config_version(strategy_id)
        return {"ok": True}
    finally:
        if owned:
            await repo.close()


@router.delete("/strategies/{strategy_id}")
async def delete_strategy(strategy_id: int, request: Request):
    repo, owned = await _get_repo(request)
    try:
        if hasattr(repo, "delete_strategy_group"):
            await repo.delete_strategy_group(strategy_id)
        else:
            await repo.db.execute("DELETE FROM strategy_groups WHERE id=?", (strategy_id,))
            await repo.db.commit()
        return {"ok": True}
    finally:
        if owned:
            await repo.close()


async def _export_losing_positions(repo: Repositories) -> Dict[str, Any]:
    LOG_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    if await _table_exists(repo, "positions"):
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
    out_path = LOG_EXPORT_DIR / f"losing_positions_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"ok": True, "path": str(out_path), "count": len(rows), "data": payload}


@router.post("/emergency/export-losing")
async def export_losing(request: Request):
    repo, owned = await _get_repo(request)
    try:
        return await _export_losing_positions(repo)
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
        initial = by_stage.get("initial_filter", {})
        second = by_stage.get("second_filter", {})
        core = by_stage.get("second_core_recheck", {})
        out[str(sid)] = {
            "strategy": _strategy_label(sg),
            "first_round": {
                "screened": int(initial.get("screened") or 0),
                "passed": int(initial.get("passed") or 0),
                "failed": int(initial.get("failed") or 0),
            },
            "second_round": {
                "screened": int(second.get("screened") or 0),
                "passed": int(second.get("passed") or 0),
                "failed": int(second.get("failed") or 0),
                "core_recheck_screened": int(core.get("screened") or 0),
                "core_recheck_passed": int(core.get("passed") or 0),
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
        initial_counts: Counter[str] = Counter()
        second_counts: Counter[str] = Counter()
        for row in rows:
            stage = row.get("stage") or "unknown"
            names = _extract_failed_features(row.get("pass_fail_detail_json"), stage) or [stage]
            counter = initial_counts if stage == "initial_filter" else second_counts
            counter.update(names)
        out[str(sid)] = {
            "strategy": _strategy_label(sg),
            "first_round": [{"feature": k, "filtered_count": v} for k, v in initial_counts.most_common(10)],
            "second_round": [{"feature": k, "filtered_count": v} for k, v in second_counts.most_common(10)],
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


async def _sample_event(repo: Repositories, strategy_id: int, *, second_passed: bool) -> Optional[int]:
    if second_passed:
        row = await _fetch_one(
            repo,
            """
            SELECT discovery_event_id
            FROM token_strategy_matches
            WHERE strategy_id=? AND stage='second_filter' AND passed=1 AND discovery_event_id IS NOT NULL
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
          AND im.stage='initial_filter'
          AND im.passed=1
          AND im.discovery_event_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM token_strategy_matches sm
              WHERE sm.discovery_event_id = im.discovery_event_id
                AND sm.strategy_id = im.strategy_id
                AND sm.stage = 'second_filter'
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
        failed_event_id = await _sample_event(repo, sid, second_passed=False)
        passed_event_id = await _sample_event(repo, sid, second_passed=True)
        out[str(sid)] = {"strategy": _strategy_label(sg), "initial_passed_but_not_second_passed": None, "second_passed_pool": None}
        if failed_event_id is not None:
            out[str(sid)]["initial_passed_but_not_second_passed"] = await _raw_pool_payload(repo, sid, failed_event_id)
        if passed_event_id is not None:
            out[str(sid)]["second_passed_pool"] = await _raw_pool_payload(repo, sid, passed_event_id)
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
            "first_round uses token_strategy_matches.stage='initial_filter'.",
            "second_round uses stages 'second_core_recheck' and 'second_filter'.",
            "gmgn_raw_samples_by_strategy chooses one pool per strategy that passed initial_filter but did not pass second_filter, plus one pool that passed second_filter when available.",
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
