#!/usr/bin/env python3
"""Diagnose why rug_ratio/insider_ratio/suspected_insider_hold_rate/is_wash_trading
are missing from holding risk snapshots.  No production code changes.

Outputs:
  logs/gmgn_risk_field_probe_<ts>.json       — raw probe results
  logs/gmgn_risk_field_probe_report_<ts>.md  — summary report
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

os.environ.setdefault("PROVIDER_MODE", "live")

from app.config import settings
from app.db.database import init_db
from app.db.repositories import Repositories
from app.providers.gmgn_real import GMGNProvider


TARGET_FIELDS = {
    "rug_ratio": [
        "rug_ratio", "max_rug_ratio", "rug", "rugged_ratio", "rug_rate",
        "risk.rug_ratio", "security.rug_ratio", "stat.rug_ratio",
    ],
    "insider_ratio": [
        "insider_ratio", "max_insider_ratio", "insider_rate",
        "insider_hold_rate", "insider_holding_rate",
        "insider_trader_amount_rate",
    ],
    "suspected_insider_hold_rate": [
        "suspected_insider_hold_rate", "suspected_insider_rate",
        "suspected_insider", "suspected_insider_hold",
        "insider_hold_rate",
    ],
    "is_wash_trading": [
        "is_wash_trading", "wash_trading", "wash_trading_detected",
        "is_wash", "wash",
    ],
}


def _recursive_key_search(obj: Any, prefix: str = "", depth: int = 0) -> Dict[str, Any]:
    if depth > 6 or not isinstance(obj, dict):
        return {}
    results: Dict[str, Any] = {}
    for k, v in obj.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and depth < 6:
            results.update(_recursive_key_search(v, path, depth + 1))
        elif not isinstance(v, (dict, list)):
            results[path] = v
    return results


def _find_candidate(raw: Dict[str, Any], candidates: List[str]) -> Dict[str, Any]:
    flat = _recursive_key_search(raw)
    found: Dict[str, Any] = {}
    for cand in candidates:
        if cand in flat:
            found[cand] = flat[cand]
        for path, val in flat.items():
            if cand in path or path.endswith(cand):
                found[path] = val
    return found


def _top_level_keys(obj: Any, prefix: str = "") -> List[str]:
    if not isinstance(obj, dict):
        return []
    return [f"{prefix}.{k}" if prefix else k for k in obj]


async def _probe_endpoint(
    gmgn: GMGNProvider,
    label: str,
    path: str,
    token_mint: str,
    pool_address: str,
) -> Dict[str, Any]:
    """Probe a single endpoint and return the raw response + candidate analysis."""
    result: Dict[str, Any] = {
        "endpoint": label,
        "path": path,
        "token_mint": token_mint,
        "pool_address": pool_address,
        "via_mint": None,
        "via_pool": None,
    }

    # Call with token_mint as address
    if label == "trenches":
        params = {"chain": "sol", "limit": 1, "type": "new_creation"}
        try:
            items = await gmgn.fetch_trenches(params)
            raw = {"data": items, "_source": "fetch_trenches"}
        except Exception as exc:
            raw = {"_error": str(exc)[:200]}
    else:
        params_mint = {"chain": "sol", "address": token_mint}
        try:
            raw = await gmgn._make_request(path, params_mint, method="GET")
        except Exception as exc:
            raw = {"_error": str(exc)[:200]}

    result["via_mint"] = {
        "status": "ok" if raw and "_error" not in raw else "error",
        "raw_json_preview": json.dumps(raw, indent=2, default=str)[:2000],
        "has_data": "data" in raw,
        "top_keys": _top_level_keys(raw.get("data", raw) if isinstance(raw, dict) else {}),
        "candidates": {f: _find_candidate(raw if isinstance(raw, dict) else {}, cands)
                       for f, cands in TARGET_FIELDS.items()},
    }

    # Call with pool_address if available
    if pool_address:
        if label == "trenches":
            result["via_pool"] = {"status": "skipped", "reason": "trenches has no pool-address lookup"}
        else:
            params_pool = {"chain": "sol", "address": pool_address}
            try:
                raw_pool = await gmgn._make_request(path, params_pool, method="GET")
            except Exception as exc:
                raw_pool = {"_error": str(exc)[:200]}
            result["via_pool"] = {
                "status": "ok" if raw_pool and "_error" not in raw_pool else "error",
                "candidates": {f: _find_candidate(raw_pool if isinstance(raw_pool, dict) else {}, cands)
                               for f, cands in TARGET_FIELDS.items()},
            }

    return result


async def _probe_normalized(gmgn: GMGNProvider, token_mint: str) -> Dict[str, Any]:
    """Call fetch_token_snapshot and check normalized output for risk fields."""
    try:
        snap = await gmgn.fetch_token_snapshot(token_mint)
        return {
            field: snap.get(field)
            for field in ("max_rug_ratio", "rug_ratio", "max_insider_ratio", "insider_ratio",
                          "suspected_insider_hold_rate", "is_wash_trading")
        }
    except Exception as exc:
        return {"_error": str(exc)[:200]}


async def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = REPO / "logs"
    log_dir.mkdir(exist_ok=True)

    probe: Dict[str, Any] = {
        "timestamp": ts,
        "samples": [],
    }

    db = await init_db(path=settings.SQLITE_PATH)
    repo = Repositories(db)
    gmgn = GMGNProvider(repo, mode="live")

    # -----------------------------------------------------------------------
    # Sample selection
    # -----------------------------------------------------------------------
    samples: List[Dict[str, str]] = []

    # Priority 1: open positions
    try:
        open_rows = await repo.fetchall(
            """SELECT token_mint, pool_address FROM positions
               WHERE status IN ('POSITION_OPEN','SIM_OPEN','PARTIAL_EXIT')
                 AND COALESCE(remaining_token_amount, 0) > 0
               ORDER BY id DESC LIMIT 5"""
        )
        for r in open_rows:
            samples.append({"token_mint": r["token_mint"], "pool_address": r.get("pool_address") or "",
                            "source": "open_position"})
    except Exception as exc:
        probe["sample_error"] = f"open_positions: {exc}"

    # Priority 2: snapshots
    if not samples:
        try:
            snap_rows = await repo.fetchall(
                """SELECT token_mint, pool_address FROM token_metric_snapshots
                   ORDER BY id DESC LIMIT 5"""
            )
            for r in snap_rows:
                samples.append({"token_mint": r["token_mint"], "pool_address": r.get("pool_address") or "",
                                "source": "snapshot"})
        except Exception as exc:
            probe["sample_error_snap"] = f"snapshots: {exc}"

    # Priority 3: fallback — call trenches to get sample tokens
    if not samples:
        try:
            fallback_params = {"chain": "sol", "limit": 5, "type": "new_creation"}
            trenches_items = await gmgn.fetch_trenches(fallback_params)
            for item in (trenches_items or []):
                samples.append({
                    "token_mint": item.get("token_mint") or item.get("address") or "",
                    "pool_address": item.get("pool_address") or "",
                    "source": "trenches_fallback",
                })
        except Exception as exc:
            probe["sample_fallback_error"] = f"trenches_fallback: {exc}"

    probe["samples"] = samples

    # -----------------------------------------------------------------------
    # Endpoint probes per sample
    # -----------------------------------------------------------------------
    ENDPOINTS = [
        ("token_info", settings.GMGN_TOKEN_INFO_PATH),
        ("token_security", getattr(settings, "GMGN_TOKEN_SECURITY_PATH", "/v1/token/security")),
        ("pool_info", getattr(settings, "GMGN_TOKEN_POOL_INFO_PATH", "/v1/token/pool_info")),
        ("top_holders", settings.GMGN_TOKEN_HOLDERS_PATH),
        ("trenches", settings.GMGN_TRENCHES_PATH),
    ]

    for sample in samples:
        mint = sample["token_mint"]
        pool = sample["pool_address"]
        entry: Dict[str, Any] = {
            "token_mint": mint,
            "pool_address": pool,
            "source": sample["source"],
            "endpoints": {},
        }

        for label, path in ENDPOINTS:
            entry["endpoints"][label] = await _probe_endpoint(gmgn, label, path, mint, pool)

        # Normalized check
        entry["normalized"] = await _probe_normalized(gmgn, mint)

        probe["samples"].append(entry)

    # -----------------------------------------------------------------------
    # Build markdown report (aggregate from probe["samples"], not last entry)
    # -----------------------------------------------------------------------
    report_lines = [
        "# GMGN 风控字段缺失诊断报告",
        f"\n## 诊断时间\n{ts}",
    ]

    if samples:
        report_lines.append("\n## 样本")
        for s in samples:
            report_lines.append(f"- token_mint: `{s['token_mint']}`, pool_address: `{s['pool_address']}`, source: {s['source']}")

    # Aggregate: for each endpoint, count how many samples have each field via mint
    report_lines.append("\n## 结论总览")
    ep_labels = [ep[0] for ep in ENDPOINTS] + ["normalized"]
    header = "| 字段 | " + " | ".join(ep_labels) + " | raw_path | value_sample |"
    sep = "|" + "---|" * (len(ep_labels) + 2)
    report_lines.append(header)
    report_lines.append(sep)

    # Aggregate candidate paths found
    all_found_paths: Dict[str, List[tuple]] = {}

    for field in TARGET_FIELDS:
        ep_found = []
        for ep_label in ep_labels:
            if ep_label == "normalized":
                any_found = any(
                    isinstance(s.get("normalized"), dict) and s["normalized"].get(field) is not None
                    for s in probe["samples"]
                )
                ep_found.append("Y" if any_found else "N")
            else:
                any_found = False
                for s in probe["samples"]:
                    ep_data = s.get("endpoints", {}).get(ep_label, {})
                    cands = ep_data.get("via_mint", {}).get("candidates", {}).get(field, {})
                    if cands:
                        any_found = True
                        for raw_path, val in cands.items():
                            all_found_paths.setdefault(field, []).append(
                                (ep_label, raw_path, val)
                            )
                ep_found.append("Y" if any_found else "N")

        # Pick first found raw_path and value_sample
        first_path = ""
        first_val = ""
        if field in all_found_paths and all_found_paths[field]:
            first_path = all_found_paths[field][0][1]
            first_val = str(all_found_paths[field][0][2])
        report_lines.append(f"| {field} | {' | '.join(ep_found)} | {first_path} | {first_val} |")

    report_lines.append("\n## 所有候选字段路径")
    for field, paths in sorted(all_found_paths.items()):
        report_lines.append(f"\n### {field}")
        report_lines.append(f"- 候选 key: {', '.join(TARGET_FIELDS[field])}")
        for ep_label, raw_path, val in paths:
            report_lines.append(f"- endpoint={ep_label}, raw_path={raw_path}, value_sample={val}")

    report_lines.append("\n## 未匹配到的候选 key")
    for field, candidates in TARGET_FIELDS.items():
        if field not in all_found_paths or not all_found_paths[field]:
            report_lines.append(f"\n### {field} — 未找到")
            report_lines.append(f"- 候选 key: {', '.join(candidates)}")

    report_lines.append("\n## 建议下一步")
    report_lines.append("1. 确认 raw JSON 中是否包含候选 key。")
    report_lines.append("2. 对比 _normalize_token_data() 中的 alias 列表。")
    report_lines.append("3. 确认 PositionRiskRunner.RISK_REQUIRED_ALIASES 是否覆盖了正确字段名。")
    report_lines.append("4. 如字段缺失，检查 GMGN API 权限/套餐。")

    # -----------------------------------------------------------------------
    # Write outputs
    # -----------------------------------------------------------------------
    json_path = log_dir / f"gmgn_risk_field_probe_{ts}.json"
    md_path = log_dir / f"gmgn_risk_field_probe_report_{ts}.md"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(probe, f, ensure_ascii=False, indent=2, default=str)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))

    print(f"Diagnostic JSON: {json_path}")
    print(f"Diagnostic report: {md_path}")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
