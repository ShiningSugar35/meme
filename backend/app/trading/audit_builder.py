from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..db.repositories import Repositories
from ..logging_config import logger

EXIT_REASON_LABELS: Dict[str, str] = {
    "HARD_TP_160": "硬止盈：价格超过 1.6x，撤仓50%",
    "HARD_TP_210": "硬止盈：价格超过 2.1x，全部撤仓",
    "HARD_SL_70": "硬止损：价格低于 0.7x，撤仓50%",
    "HARD_SL_45": "硬止损：价格低于 0.45x，全部撤仓",
    "COMPLETED": "池子 type 变为 completed，全部撤仓",
    "SMART_MONEY_SELL": "聪明钱卖出触发",
    "TOP3_SMART_DEGEN_DUMP": "TOP3聪明钱减仓超过25%",
    "RISK_RECHECK_FAILED": "持仓风控复查失败",
    "DUST_FORCE_EXIT": "尘埃仓强制清仓",
    "RISK_DATA_UNAVAILABLE_EXIT": "风控数据连续异常，撤仓",
}

ENTRY_AUDIT_REQUIRED_FIELDS = [
    "buy_time_utc", "buy_time_beijing", "token_mint", "symbol", "name",
    "pool_address", "pool_type", "launchpad", "launchpad_platform",
    "platform", "exchange",
    "rug_ratio", "entrapment_ratio", "insider_ratio", "bundler_rate",
    "liquidity", "top_holder_rate", "fresh_wallet_rate", "creator_balance_rate",
    "holder_count", "marketcap", "smart_degen_count", "volume_24h",
    "is_wash_trading", "rat_trader_amount_rate", "suspected_insider_hold_rate",
    "sell_tax", "socials", "burn_status", "sniper_count",
    "top1_addr_type0_address", "top1_addr_type0_holder_rate",
    "top1_addr_type0_usd_value", "swaps_1h", "volume_1h",
    "price_change_percent1h",
    "smart_degen_max_holder_address", "smart_degen_max_holder_pct",
    "smart_degen_max_holder_usd", "smart_degen_min_holder_address",
    "smart_degen_min_holder_pct", "smart_degen_min_holder_usd",
    "buy_price_usd", "buy_price_sol", "buy_token_amount", "buy_value_usd_net",
    "jupiter_quote_ok", "quote_json", "route_plan_json",
    "input_amount_raw", "output_amount_raw", "quote_out_amount_raw",
    "quote_price_impact_pct", "input_mint", "output_mint",
    "entry_data_sources", "entry_missing_fields",
]

EXIT_AUDIT_REQUIRED_FIELDS = [
    "sell_time_utc", "sell_time_beijing",
    "exit_reason_code", "exit_reason_label",
    "exit_pct", "sell_price_usd", "sell_price_usd_spot", "sell_price_usd_effective",
    "buy_price_usd", "sell_price_multiple",
    "sell_token_amount", "sell_value_usd_net", "gross_value_usd",
    "remaining_token_amount_before", "remaining_value_usd_before",
    "remaining_token_amount_after", "remaining_value_usd_after",
    "jupiter_quote_ok", "quote_json", "route_plan_json", "price_impact_pct",
    "risk_failed_rules", "dust_detail",
    "smart_money_trigger_detail", "top3_smart_degen_trigger_detail",
    "exit_data_sources", "exit_missing_fields",
]


def _to_float(v: Any, default: Any = None) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return f if math.isfinite(f) else default


def _safe_json(data: Any) -> Optional[str]:
    if data is None:
        return None
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_to_beijing(iso_str: Optional[str]) -> str:
    if not iso_str:
        return ""
    try:
        BJ = timezone(timedelta(hours=8))
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(BJ).isoformat()
    except Exception:
        return iso_str


def _first_present(data: Any, keys: List[str]) -> Any:
    if not isinstance(data, dict):
        return None
    for k in keys:
        if k in data and data[k] is not None and data[k] != "":
            return data[k]
    return None


def first_non_missing(*values: Any) -> Any:
    for v in values:
        if v is not None and v != "":
            return v
    return None


def _normalize_pool_type(v: Any) -> Any:
    if isinstance(v, str) and v.lower() == "pump":
        return "near_completion"
    return v


def _build_socials(snapshot: Optional[Dict[str, Any]],
                   token_info: Optional[Dict[str, Any]],
                   discovery_raw: Optional[Dict[str, Any]],
                   gmgn_raw_json: Optional[str] = None) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    SOCIAL_MAP: List[Tuple[str, str, str]] = [
        ("twitter", "twitter", "twitter_username"),
        ("telegram", "telegram", "telegram"),
        ("website", "website", "website"),
        ("discord", "discord", "discord"),
        ("instagram", "instagram", "instagram"),
        ("tiktok", "tiktok", "tiktok"),
        ("youtube", "youtube", "youtube"),
        ("gmgn", "gmgn", "gmgn"),
        ("geckoterminal", "geckoterminal", "geckoterminal"),
    ]
    seen: set = set()
    for source_priority in (discovery_raw, token_info, snapshot):
        if not isinstance(source_priority, dict):
            continue
        for stype, disc_key, info_key in SOCIAL_MAP:
            if stype in seen:
                continue
            val = source_priority.get(disc_key) or source_priority.get(info_key)
            if val and isinstance(val, str) and val.strip():
                stripped = val.strip()
                seen.add(stype)
                if stype == "twitter":
                    url = stripped if stripped.startswith("http") else f"https://x.com/{stripped}"
                elif stype == "telegram":
                    url = stripped if stripped.startswith("http") else f"https://t.me/{stripped}"
                elif stype == "website":
                    url = stripped if stripped.startswith("http") else f"https://{stripped}"
                else:
                    url = stripped
                out.append({"type": stype, "value": stripped, "url": url})

    # Enrich from GMGN raw_json → data.link
    if gmgn_raw_json:
        try:
            bundle = json.loads(gmgn_raw_json) if isinstance(gmgn_raw_json, str) else gmgn_raw_json
            info_raw = bundle.get("token_info", {})
            if isinstance(info_raw, dict):
                from ..providers.gmgn_real import GMGNProvider
                unwrapped = GMGNProvider._unwrap_data(info_raw)
                if isinstance(unwrapped, dict):
                    link = unwrapped.get("link", {})
                    if isinstance(link, dict):
                        for stype, disc_key, info_key in SOCIAL_MAP:
                            if stype in seen:
                                continue
                            val = link.get(info_key) or link.get(stype) or link.get(disc_key)
                            if val and isinstance(val, str) and val.strip():
                                stripped = val.strip()
                                seen.add(stype)
                                if stype == "twitter":
                                    url = stripped if stripped.startswith("http") else f"https://x.com/{stripped}"
                                elif stype == "telegram":
                                    url = stripped if stripped.startswith("http") else f"https://t.me/{stripped}"
                                elif stype == "website":
                                    url = stripped if stripped.startswith("http") else f"https://{stripped}"
                                else:
                                    url = stripped
                                out.append({"type": stype, "value": stripped, "url": url})
        except Exception:
            pass
    return out


def _build_entry_data_sources(
    fetch_token_snapshot_called: bool = False,
    token_info_extracted_from_raw_json: bool = False,
    token_security_extracted_from_raw_json: bool = False,
    holders_called: bool = False,
    smart_degen_holders_called: bool = False,
    kline_called: bool = False,
    snapshot_source: str = "snapshot_id",
    discovery_event_id: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "fetch_token_snapshot_called": fetch_token_snapshot_called,
        "token_info_extracted_from_raw_json": token_info_extracted_from_raw_json,
        "token_security_extracted_from_raw_json": token_security_extracted_from_raw_json,
        "holders_called": holders_called,
        "smart_degen_holders_called": smart_degen_holders_called,
        "kline_called": kline_called,
        "snapshot_source": snapshot_source,
        "discovery_event_id": discovery_event_id,
    }


def _is_addr_type0(h: Dict[str, Any]) -> bool:
    try:
        return int(h.get("addr_type", -1)) == 0
    except Exception:
        return False


def _compute_missing_fields(payload: Dict[str, Any], required: List[str]) -> List[str]:
    EXCLUDED = {"entry_missing_fields", "exit_missing_fields",
                "entry_data_sources", "exit_data_sources"}
    missing: List[str] = []
    for field in required:
        if field in EXCLUDED:
            continue
        val = payload.get(field)
        if val is None or val == "" or val == [] or val == {}:
            missing.append(field)
    return missing


async def build_entry_audit_payload(
    repo: Repositories,
    gmgn: Any,
    token_mint: str,
    position_id: int,
    account_type: str,
    strategy: Dict[str, Any],
    discovery_event_id: Optional[int],
    snapshot_id: Optional[int],
    buy_trade_event: Dict[str, Any],
    quote: Dict[str, Any],
    token_amount: float,
    price_usd: Optional[float],
    price_sol: Optional[float],
    size_usd: float,
    liquidity_usd: Optional[float] = None,
    sol_side_liquidity: Optional[float] = None,
    smart_degen_required: bool = True,
) -> Dict[str, Any]:
    data_sources = _build_entry_data_sources(
        fetch_token_snapshot_called=False,
        token_info_extracted_from_raw_json=False,
        token_security_extracted_from_raw_json=False,
        holders_called=False,
        smart_degen_holders_called=False,
        kline_called=False,
        snapshot_source="none",
        discovery_event_id=discovery_event_id,
    )

    payload: Dict[str, Any] = {k: None for k in ENTRY_AUDIT_REQUIRED_FIELDS}

    buy_time_utc = buy_trade_event.get("created_at") or _utc_now_iso()
    payload["buy_time_utc"] = buy_time_utc
    payload["buy_time_beijing"] = _utc_to_beijing(buy_time_utc)
    payload["token_mint"] = token_mint
    payload["buy_price_usd"] = price_usd
    payload["buy_price_sol"] = price_sol
    payload["buy_token_amount"] = token_amount
    payload["buy_value_usd_net"] = -abs(size_usd)
    payload["jupiter_quote_ok"] = bool(quote and not quote.get("error"))
    if quote and not quote.get("error"):
        payload["quote_json"] = _safe_json(quote)
        payload["route_plan_json"] = _safe_json((quote.get("routePlan") or [])[:3])
        payload["input_amount_raw"] = str(quote.get("inAmount") or "")
        payload["output_amount_raw"] = str(quote.get("outAmount") or "")
        payload["quote_out_amount_raw"] = str(quote.get("outAmount") or "")
        payload["quote_price_impact_pct"] = _to_float(quote.get("priceImpactPct"))
        payload["input_mint"] = _first_present(quote, ["inputMint", "input_mint"])
        payload["output_mint"] = _first_present(quote, ["outputMint", "output_mint"])

    token_info = None
    try:
        token_info = await repo.get_token(token_mint)
    except Exception:
        pass

    entry_snapshot = None
    snapshot_source_label = "none"
    if snapshot_id:
        try:
            entry_snapshot = await repo.get_token_metric_snapshot(snapshot_id)
            snapshot_source_label = "snapshot_id"
        except Exception:
            pass
    if not entry_snapshot:
        try:
            entry_snapshot = await repo.get_latest_token_metric_snapshot(token_mint)
            snapshot_source_label = "latest_fallback"
        except Exception:
            pass
    data_sources["snapshot_source"] = snapshot_source_label

    discovery_raw: Optional[Dict[str, Any]] = None
    if discovery_event_id:
        try:
            ev = await repo.get_discovery_event(discovery_event_id)
            if ev:
                raw = ev.get("raw_json") or ev.get("context_json") or ev.get("snapshot_json")
                if isinstance(raw, str):
                    raw = json.loads(raw)
                if isinstance(raw, dict):
                    discovery_raw = raw
        except Exception:
            pass

    token_info_data: Optional[Dict[str, Any]] = None
    try:
        ti = await gmgn.fetch_token_snapshot(token_mint)
        if isinstance(ti, dict) and ti:
            token_info_data = ti
            data_sources["fetch_token_snapshot_called"] = True
            data_sources["token_info_extracted_from_raw_json"] = True
    except Exception:
        pass

    security_data: Optional[Dict[str, Any]] = None
    try:
        if token_info_data and token_info_data.get("raw_json"):
            raw_bundle = json.loads(token_info_data["raw_json"])
            security_raw = raw_bundle.get("security") if isinstance(raw_bundle, dict) else None
            if isinstance(security_raw, dict) and not security_raw.get("error"):
                data_sources["token_security_extracted_from_raw_json"] = True
    except Exception:
        pass
    if token_info_data:
        security_fields = ["sell_tax", "burn_status", "suspected_insider_hold_rate",
                           "rat_trader_amount_rate", "max_bundler_rate",
                           "max_rug_ratio", "sniper_count", "is_wash_trading",
                           "creator_balance_rate"]
        security_data = {k: token_info_data.get(k) for k in security_fields}

    holders: List[Dict[str, Any]] = []
    try:
        holders = await gmgn.fetch_top_holders(token_mint, limit=50)
        data_sources["holders_called"] = True
    except Exception:
        pass

    smart_degen_holders: List[Dict[str, Any]] = []
    if smart_degen_required:
        try:
            smart_degen_holders = await gmgn.fetch_smart_degen_holders(token_mint, limit=100)
            data_sources["smart_degen_holders_called"] = True
        except Exception:
            pass

    klines: List[Dict[str, Any]] = []
    try:
        klines = await gmgn.fetch_kline(token_mint, interval="1h", limit=2)
        data_sources["kline_called"] = True
    except Exception:
        pass

    def _resolve_snapshot(key: str, alt_keys: Optional[List[str]] = None,
                          tier2: Optional[Dict] = None,
                          tier3: Optional[Dict] = None) -> Any:
        if entry_snapshot and entry_snapshot.get(key) is not None and entry_snapshot.get(key) != "":
            return entry_snapshot.get(key)
        if alt_keys and entry_snapshot:
            for ak in alt_keys:
                if entry_snapshot.get(ak) is not None and entry_snapshot.get(ak) != "":
                    return entry_snapshot.get(ak)
        if tier2 is not None:
            for v in ([tier2] if isinstance(tier2, dict) else tier2):
                if isinstance(v, dict):
                    for k in [key] + (alt_keys or []):
                        if v.get(k) is not None and v.get(k) != "":
                            return v.get(k)
        if tier3 is not None:
            for v in ([tier3] if isinstance(tier3, dict) else tier3):
                if isinstance(v, dict):
                    for k in [key] + (alt_keys or []):
                        if v.get(k) is not None and v.get(k) != "":
                            return v.get(k)
        return None

    snap_extra = entry_snapshot or {}
    tok_info = token_info or {}
    disc = discovery_raw or {}
    info_data = token_info_data or {}
    sec = security_data or {}

    payload["symbol"] = first_non_missing(
        _first_present(tok_info, ["symbol"]),
        _first_present(info_data, ["symbol"]),
        _first_present(disc, ["symbol"]),
    )
    payload["name"] = first_non_missing(
        _first_present(tok_info, ["name"]),
        _first_present(info_data, ["name"]),
        _first_present(disc, ["name"]),
    )
    payload["pool_address"] = first_non_missing(
        _first_present(tok_info, ["pool_address"]),
        _first_present(snap_extra, ["pool_address"]),
        _first_present(info_data, ["pool_address"]),
        _first_present(disc, ["pool_address"]),
    )
    payload["pool_type"] = _normalize_pool_type(first_non_missing(
        _first_present(tok_info, ["latest_type"]),
        _first_present(snap_extra, ["type"]),
        _first_present(disc, ["type"]),
    ))
    payload["launchpad"] = first_non_missing(
        _first_present(tok_info, ["launchpad"]),
        _first_present(info_data, ["launchpad"]),
        _first_present(snap_extra, ["launchpad"]),
    )
    payload["launchpad_platform"] = first_non_missing(
        _first_present(info_data, ["launchpad_platform"]),
        _first_present(disc, ["launchpad_platform"]),
        _first_present(snap_extra, ["platform"]),
        _first_present(tok_info, ["launchpad"]),
    )
    payload["platform"] = first_non_missing(
        _first_present(snap_extra, ["platform"]),
        _first_present(disc, ["launchpad_platform"]),
        _first_present(info_data, ["launchpad_platform"]),
    )
    exchange = info_data.get("pool", {}).get("exchange") if isinstance(info_data.get("pool"), dict) else None
    payload["exchange"] = first_non_missing(
        _first_present(info_data, ["exchange"]),
        exchange,
        _first_present(disc, ["exchange"]),
    )

    payload["rug_ratio"] = first_non_missing(
        _first_present(snap_extra, ["max_rug_ratio"]),
        _first_present(disc, ["rug_ratio"]),
        _first_present(sec, ["max_rug_ratio"]),
    )
    payload["entrapment_ratio"] = first_non_missing(
        _first_present(snap_extra, ["max_entrapment_ratio"]),
        _first_present(disc, ["entrapment_ratio"]),
    )
    payload["insider_ratio"] = first_non_missing(
        _first_present(snap_extra, ["max_insider_ratio"]),
        _first_present(disc, ["insider_ratio"]),
    )
    payload["bundler_rate"] = first_non_missing(
        _first_present(snap_extra, ["max_bundler_rate"]),
        _first_present(disc, ["bundler_trader_amount_rate"]),
        _first_present(disc, ["bundler_rate"]),
        _first_present(sec, ["max_bundler_rate"]),
    )
    pool_liquidity = _first_present(info_data.get("pool", {}), ["liquidity", "liquidity_usd"])
    payload["liquidity"] = first_non_missing(
        _first_present(snap_extra, ["liquidity_usd"]),
        _first_present(disc, ["liquidity"]),
        _first_present(info_data, ["liquidity_usd"]),
        pool_liquidity,
        liquidity_usd,
    )
    payload["top_holder_rate"] = first_non_missing(
        _first_present(snap_extra, ["top_10_holder_rate"]),
        _first_present(disc, ["top_10_holder_rate"]),
        _first_present(sec, ["top_10_holder_rate"]),
        _first_present(info_data, ["top_10_holder_rate"]),
    )
    payload["fresh_wallet_rate"] = first_non_missing(
        _first_present(snap_extra, ["fresh_wallet_rate"]),
        _first_present(info_data, ["fresh_wallet_rate"]),
        _first_present(disc, ["fresh_wallet_rate"]),
    )
    payload["creator_balance_rate"] = first_non_missing(
        _first_present(snap_extra, ["creator_balance_rate"]),
        _first_present(disc, ["creator_balance_rate"]),
        _first_present(sec, ["creator_balance_rate"]),
        _first_present(info_data, ["creator_balance_rate"]),
        _first_present(info_data.get("stat", {}), ["creator_hold_rate"]),
    )
    if payload["creator_balance_rate"] is None and info_data:
        try:
            bal = _to_float(info_data.get("creator_token_balance"))
            sup = _to_float(info_data.get("total_supply"))
            if bal is not None and sup is not None and sup > 0:
                payload["creator_balance_rate"] = bal / sup
        except Exception:
            pass
    stat_holder_count = _first_present(info_data.get("stat", {}), ["holder_count"])
    payload["holder_count"] = first_non_missing(
        _first_present(snap_extra, ["holder_count"]),
        _first_present(disc, ["holder_count"]),
        _first_present(info_data, ["holder_count"]),
        stat_holder_count,
    )
    price_market_cap = _first_present(info_data.get("price", {}), ["market_cap"])
    payload["marketcap"] = first_non_missing(
        _first_present(snap_extra, ["market_cap"]),
        _first_present(disc, ["usd_market_cap"]),
        _first_present(disc, ["market_cap"]),
        _first_present(info_data, ["market_cap"]),
        price_market_cap,
    )
    if payload["marketcap"] is None and info_data:
        try:
            p = _to_float(info_data.get("price_usd") or info_data.get("price"))
            cs = _to_float(info_data.get("circulating_supply"))
            if p is not None and cs is not None and cs > 0:
                payload["marketcap"] = p * cs
        except Exception:
            pass
    smart_wallets = _first_present(info_data.get("wallet_tags_stat", {}), ["smart_wallets"])
    payload["smart_degen_count"] = first_non_missing(
        _first_present(snap_extra, ["smart_degen_count"]),
        _first_present(disc, ["smart_degen_count"]),
        smart_wallets,
    )
    price_vol_24h = _first_present(info_data.get("price", {}), ["volume_24h"])
    payload["volume_24h"] = first_non_missing(
        _first_present(snap_extra, ["volume_usd"]),
        _first_present(disc, ["volume_24h"]),
        price_vol_24h,
        _first_present(info_data, ["volume_24h"]),
    )
    payload["is_wash_trading"] = first_non_missing(
        _first_present(snap_extra, ["is_wash_trading"]),
        _first_present(disc, ["is_wash_trading"]),
        _first_present(sec, ["is_wash_trading"]),
    )
    payload["rat_trader_amount_rate"] = first_non_missing(
        _first_present(snap_extra, ["rat_trader_amount_rate"]),
        _first_present(disc, ["rat_trader_amount_rate"]),
        _first_present(sec, ["rat_trader_amount_rate"]),
    )
    payload["suspected_insider_hold_rate"] = first_non_missing(
        _first_present(snap_extra, ["suspected_insider_hold_rate"]),
        _first_present(disc, ["suspected_insider_hold_rate"]),
        _first_present(sec, ["suspected_insider_hold_rate"]),
    )
    payload["sell_tax"] = first_non_missing(
        _first_present(snap_extra, ["sell_tax"]),
        _first_present(sec, ["sell_tax"]),
        _first_present(disc, ["sell_tax"]),
        _first_present(info_data, ["sell_tax"]),
    )
    payload["burn_status"] = first_non_missing(
        _first_present(snap_extra, ["burn_status"]),
        _first_present(disc, ["burn_status"]),
        _first_present(sec, ["burn_status"]),
    )
    sniper_wallets = _first_present(info_data.get("wallet_tags_stat", {}), ["sniper_wallets"])
    payload["sniper_count"] = first_non_missing(
        _first_present(snap_extra, ["sniper_count"]),
        _first_present(disc, ["sniper_count"]),
        _first_present(sec, ["sniper_count"]),
        sniper_wallets,
    )
    price_swaps_1h = _first_present(info_data.get("price", {}), ["swaps_1h"])
    payload["swaps_1h"] = first_non_missing(
        _first_present(snap_extra, ["swaps_1h"]),
        _first_present(disc, ["swaps_1h"]),
        price_swaps_1h,
        _first_present(info_data, ["swaps_1h"]),
    )
    payload["volume_1h"] = first_non_missing(
        _first_present(snap_extra, ["volume_1h"]),
        _first_present(disc, ["volume_1h"]),
        _first_present(info_data.get("price", {}), ["volume_1h"]),
    )
    if payload["volume_1h"] is None and klines:
        try:
            payload["volume_1h"] = sum(
                _to_float(k.get("volume_usd") if k.get("volume_usd") is not None else k.get("volume"), 0.0) or 0.0
                for k in klines
            )
        except Exception:
            pass
    payload["price_change_percent1h"] = first_non_missing(
        _first_present(snap_extra, ["price_change_percent_1h"]),
        _first_present(disc, ["price_change_percent1h"]),
    )
    if payload["price_change_percent1h"] is None and info_data:
        try:
            p_now = _to_float(info_data.get("price_usd") or info_data.get("price"))
            p_1h = _to_float(info_data.get("price_1h"))
            if p_now is not None and p_1h is not None and p_1h > 0:
                payload["price_change_percent1h"] = (p_now - p_1h) / p_1h * 100.0
        except Exception:
            pass
    if payload["price_change_percent1h"] is None and len(klines) >= 2:
        try:
            first_open = _to_float(klines[0].get("open"))
            last_close = _to_float(klines[-1].get("close"))
            if first_open is not None and last_close is not None and first_open > 0:
                payload["price_change_percent1h"] = (last_close - first_open) / first_open * 100.0
        except Exception:
            pass

    payload["socials"] = _build_socials(snap_extra, info_data, disc,
                                        token_info_data.get("raw_json") if token_info_data else None)

    addr_type0_holders = [h for h in holders if _is_addr_type0(h)]
    addr_type0_sorted = sorted(addr_type0_holders,
                               key=lambda h: _to_float(h.get("amount_percentage") or h.get("top1_holder_rate") or 0.0, 0.0) or 0.0,
                               reverse=True)
    if addr_type0_sorted:
        top1 = addr_type0_sorted[0]
        payload["top1_addr_type0_address"] = top1.get("address")
        payload["top1_addr_type0_holder_rate"] = _to_float(top1.get("amount_percentage") or top1.get("top1_holder_rate") or top1.get("rate"))
        payload["top1_addr_type0_usd_value"] = _to_float(top1.get("usd_value"))

    active_degen = [h for h in smart_degen_holders
                    if (_to_float(h.get("amount_percentage"), 0.0) or 0.0) > 0
                    or (_to_float(h.get("usd_value"), 0.0) or 0.0) > 0]
    if active_degen:
        max_h = max(active_degen, key=lambda h: _to_float(h.get("amount_percentage"), 0.0) or 0.0)
        min_h = min(active_degen, key=lambda h: _to_float(h.get("amount_percentage"), 0.0) or 0.0)
        payload["smart_degen_max_holder_address"] = max_h.get("address")
        payload["smart_degen_max_holder_pct"] = _to_float(max_h.get("amount_percentage"))
        payload["smart_degen_max_holder_usd"] = _to_float(max_h.get("usd_value"))
        payload["smart_degen_min_holder_address"] = min_h.get("address")
        payload["smart_degen_min_holder_pct"] = _to_float(min_h.get("amount_percentage"))
        payload["smart_degen_min_holder_usd"] = _to_float(min_h.get("usd_value"))

    payload["entry_data_sources"] = data_sources
    if smart_degen_required:
        required_fields = ENTRY_AUDIT_REQUIRED_FIELDS
    else:
        # 聪明钱不要求时，排除 6 个聪明钱审计字段
        excluded = {
            "smart_degen_max_holder_address", "smart_degen_max_holder_pct",
            "smart_degen_max_holder_usd", "smart_degen_min_holder_address",
            "smart_degen_min_holder_pct", "smart_degen_min_holder_usd",
        }
        required_fields = [f for f in ENTRY_AUDIT_REQUIRED_FIELDS if f not in excluded]
    payload["entry_missing_fields"] = _compute_missing_fields(payload, required_fields)

    return payload


async def build_exit_audit_payload(
    repo: Repositories,
    position: Dict[str, Any],
    sell_trade_event: Dict[str, Any],
    exit_reason: str,
    exit_pct: float,
    sell_amount_human: float,
    gross_value_usd: float,
    current_price_usd: Optional[float],
    current_price_sol: Optional[float] = None,
    quote: Optional[Dict[str, Any]] = None,
    risk_failed_rules: Optional[List[Dict[str, Any]]] = None,
    dust_detail: Optional[Dict[str, Any]] = None,
    smart_money_trigger_detail: Optional[Dict[str, Any]] = None,
    top3_smart_degen_trigger_detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {k: None for k in EXIT_AUDIT_REQUIRED_FIELDS}

    sell_time_utc = sell_trade_event.get("created_at") or _utc_now_iso()
    payload["sell_time_utc"] = sell_time_utc
    payload["sell_time_beijing"] = _utc_to_beijing(sell_time_utc)
    payload["exit_reason_code"] = exit_reason
    payload["exit_reason_label"] = EXIT_REASON_LABELS.get(exit_reason, exit_reason)
    payload["exit_pct"] = exit_pct
    payload["sell_price_usd"] = current_price_usd
    payload["sell_price_usd_spot"] = current_price_usd
    sell_effective = _to_float(sell_trade_event.get("sell_price_usd_effective"))
    payload["sell_price_usd_effective"] = sell_effective
    payload["sell_token_amount"] = sell_amount_human
    payload["sell_value_usd_net"] = abs(gross_value_usd)
    payload["gross_value_usd"] = gross_value_usd

    remaining_before = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0
    remaining_value_before = _to_float(position.get("remaining_value_usd"), 0.0) or 0.0
    payload["remaining_token_amount_before"] = remaining_before
    payload["remaining_value_usd_before"] = remaining_value_before
    payload["remaining_token_amount_after"] = max(0.0, remaining_before - sell_amount_human)
    payload["remaining_value_usd_after"] = payload["remaining_token_amount_after"] * (current_price_usd or 0.0) if current_price_usd else 0.0

    buy_price_usd = None
    try:
        pos_id = int(position["id"])
        token_mint = position["token_mint"]
        audits = await repo.get_position_audits(pos_id, audit_type="ENTRY")
        if audits:
            entry_json = audits[0].get("audit_json") or {}
            if isinstance(entry_json, str):
                entry_json = json.loads(entry_json)
            if isinstance(entry_json, dict):
                buy_price_usd = first_non_missing(
                    entry_json.get("buy_price_usd"),
                    entry_json.get("price_usd"),
                    position.get("entry_price_usd"),
                )
        if not buy_price_usd:
            buy_price_usd = position.get("entry_price_usd")
    except Exception:
        buy_price_usd = position.get("entry_price_usd")

    payload["buy_price_usd"] = buy_price_usd
    bp = _to_float(buy_price_usd, 0.0)
    if bp is not None and bp > 0:
        sell_price_for_multiple = sell_effective if sell_effective is not None else (
            abs(gross_value_usd) / sell_amount_human if sell_amount_human > 0 else None
        )
        if sell_price_for_multiple is not None:
            payload["sell_price_multiple"] = round(sell_price_for_multiple / bp, 2)
        else:
            payload["sell_price_multiple"] = None
    else:
        payload["sell_price_multiple"] = None

    if quote and isinstance(quote, dict) and not quote.get("error"):
        payload["jupiter_quote_ok"] = True
        payload["quote_json"] = _safe_json(quote)
        payload["route_plan_json"] = _safe_json((quote.get("routePlan") or [])[:3])
        payload["price_impact_pct"] = _to_float(quote.get("priceImpactPct"))
    else:
        payload["jupiter_quote_ok"] = bool(quote and isinstance(quote, dict) and not quote.get("error"))

    payload["risk_failed_rules"] = risk_failed_rules or []
    payload["dust_detail"] = dust_detail
    payload["smart_money_trigger_detail"] = smart_money_trigger_detail
    payload["top3_smart_degen_trigger_detail"] = top3_smart_degen_trigger_detail

    payload["exit_data_sources"] = {
        "entry_audit_found": buy_price_usd is not None,
        "position_id": position.get("id"),
        "trade_event_id": sell_trade_event.get("id"),
    }
    payload["exit_missing_fields"] = _compute_missing_fields(payload, EXIT_AUDIT_REQUIRED_FIELDS)

    return payload
