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

# ---------------------------------------------------------------------------
# Bootstrap: make backend importable
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

os.environ.setdefault("PROVIDER_MODE", "live")

from app.config import settings
from app.db.database import init_db
from app.db.repositories import Repositories
from app.providers.gmgn_real import GMGNProvider


# Candidate key paths for each target field
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
    """Walk a nested JSON-like structure and return all leaf key paths + sample values."""
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


def _find_candidate(endpoint: str, raw: Dict[str, Any], candidates: List[str]) -> Dict[str, Any]:
    """Search raw JSON for any of the candidate keys (dot-path aware)."""
    flat = _recursive_key_search(raw)
    found: Dict[str, Any] = {}
    for cand in candidates:
        # Exact match
        if cand in flat:
            found[cand] = flat[cand]
        # Partial match
        for path, val in flat.items():
            if cand in path or path.endswith(cand):
                found[path] = val
    return found


def _top_level_keys(obj: Any, prefix: str = "") -> List[str]:
    if not isinstance(obj, dict):
        return []
    return [f"{prefix}.{k}" if prefix else k for k in obj]


def _summary_for_target(endpoint: str, raw: Any) -> str:
    if not isinstance(raw, dict):
        return "not_a_dict"
    has = {f: len(_find_candidate(endpoint, raw, cands)) > 0
           for f, cands in TARGET_FIELDS.items()}
    parts = [f"{f}=Y" if v else f"{f}=N" for f, v in has.items()]
    return ", ".join(parts)


async def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = REPO / "logs"
    log_dir.mkdir(exist_ok=True)

    probe: Dict[str, Any] = {
        "timestamp": ts,
        "sample_tokens": [],
        "endpoints": {},
    }

    # -----------------------------------------------------------------------
    # DB connection & sample selection
    # -----------------------------------------------------------------------
    db = await init_db(path=settings.SQLITE_PATH)
    repo = Repositories(db)
    gmgn = GMGNProvider(repo, mode="live")

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

    probe["sample_tokens"] = samples

    # -----------------------------------------------------------------------
    # Endpoint calls per sample
    # -----------------------------------------------------------------------
    ENDPOINTS = [
        ("token_info", settings.GMGN_TOKEN_INFO_PATH),
        ("token_security", getattr(settings, "GMGN_TOKEN_SECURITY_PATH", "/v1/token/security")),
        ("pool_info", getattr(settings, "GMGN_TOKEN_POOL_INFO_PATH", "/v1/token/pool_info")),
        ("top_holders", getattr(settings, "GMGN_TOP_HOLDERS_PATH", "/v1/market/token_top_holders")),
        ("trenches", settings.GMGN_TRENCHES_PATH),
    ]

    for label, path in ENDPOINTS:
        probe["endpoints"][label] = {"path": path, "samples": []}

    for sample in samples:
        mint = sample["token_mint"]
        pool = sample["pool_address"]
        entry: Dict[str, Any] = {
            "token_mint": mint,
            "pool_address": pool,
        }

        for label, path in ENDPOINTS:
            if not path:
                entry[f"{label}_skipped"] = "no_path"
                continue

            params_mint = {"chain": "sol", "address": mint}
            if label == "trenches":
                params_mint = {"chain": "sol", "limit": 1}

            raw_mint = None
            try:
                raw_mint = await gmgn._make_request(path, params_mint, method="GET")
            except Exception as exc:
                raw_mint = {"_error": str(exc)[:200]}

            entry[f"{label}_via_mint"] = {
                "status": "ok" if raw_mint and "_error" not in raw_mint else "error",
                "summary": _summary_for_target(label, raw_mint),
                "top_keys": _top_level_keys(raw_mint.get("data", raw_mint) if isinstance(raw_mint, dict) else {}),
                "candidates": {f: _find_candidate(label, raw_mint if isinstance(raw_mint, dict) else {}, cands)
                               for f, cands in TARGET_FIELDS.items()},
            }

            # Also call with pool_address if available
            if pool:
                params_pool = {"chain": "sol", "address": pool}
                raw_pool = None
                try:
                    raw_pool = await gmgn._make_request(path, params_pool, method="GET")
                except Exception as exc:
                    raw_pool = {"_error": str(exc)[:200]}
                entry[f"{label}_via_pool"] = {
                    "status": "ok" if raw_pool and "_error" not in raw_pool else "error",
                    "summary": _summary_for_target(label, raw_pool),
                }

        # Also call current fetch_token_snapshot and check normalized output
        try:
            snap = await gmgn.fetch_token_snapshot(mint)
            entry["normalized"] = {
                field: snap.get(field) for field in
                ("max_rug_ratio", "rug_ratio", "max_insider_ratio", "insider_ratio",
                 "suspected_insider_hold_rate", "is_wash_trading")
            }
        except Exception as exc:
            entry["normalized_error"] = str(exc)[:200]

        for label in ENDPOINTS:
            en = label[0]
            if en in entry:
                probe["endpoints"][label]["samples"].append(entry)

    # -----------------------------------------------------------------------
    # Build markdown report
    # -----------------------------------------------------------------------
    report_lines = [
        "# GMGN 风控字段缺失诊断报告",
        f"\n## 诊断时间\n{ts}",
        "\n## 样本",
    ]
    for s in samples:
        report_lines.append(f"- token_mint: `{s['token_mint']}`, pool_address: `{s['pool_address']}`, source: {s['source']}")

    report_lines.append("\n## 结论总览")
    report_lines.append("\n| 字段 | token/info | token/security | pool_info | top_holders | trenches | normalized | 初步判断 |")
    report_lines.append("|---|---|---|---|---|---|---|---|")

    for field, candidates in TARGET_FIELDS.items():
        rows = []
        for ep_label in ("token_info", "token_security", "pool_info", "top_holders", "trenches"):
            found_any = False
            for s_entry in entry.get(ep_label, {}).get("samples", []):
                cands = s_entry.get(f"{ep_label}_via_mint", {}).get("candidates", {}).get(field, {})
                if cands:
                    found_any = True
                    break
            rows.append("Y" if found_any else "N")
        # Normalized check
        norm_val = entry.get("normalized", {}).get(field)
        rows.append("Y" if norm_val is not None else "N")
        rows.append("待分析")
        report_lines.append(f"| {field} | {' | '.join(rows)} |")

    report_lines.append("\n## 发现的候选字段路径")
    for field, candidates in TARGET_FIELDS.items():
        report_lines.append(f"\n### {field}")
        report_lines.append(f"- 候选 key: {', '.join(candidates)}")
        for ep_label in ("token_info", "token_security", "pool_info", "top_holders", "trenches"):
            for s_entry in entry.get(ep_label, {}).get("samples", []):
                cands = s_entry.get(f"{ep_label}_via_mint", {}).get("candidates", {}).get(field, {})
                if cands:
                    for path, val in cands.items():
                        report_lines.append(f"- endpoint={ep_label}, raw_path={path}, value_sample={val}")

    report_lines.append("\n## 初步判断")
    report_lines.append("请检查原始 JSON 输出后补充判断。")

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
