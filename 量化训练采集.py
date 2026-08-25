#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LEGACY GMGN meme-token training data collector (reference only).

This standalone H2 collector is intentionally disabled as an executable after
the 2026-08-12 H1/no-completed migration. Production collection must go through
backend.app.collector so completed lifecycle samples and H2 labels cannot be
reintroduced into the dataset.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import random
import re
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import httpx
except Exception as exc:  # pragma: no cover
    httpx = None
    HTTPX_IMPORT_ERROR = exc
else:
    HTTPX_IMPORT_ERROR = None


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
PARENT_ENV_PATH = ROOT.parent / ".env"
CSV_PATH = ROOT / "meme数据.csv"
STATE_PATH = ROOT / ".meme数据.state.json"
LOG_PATH = ROOT / "logs" / "meme_training_collector.log"

UTC = timezone.utc
BJT = timezone(timedelta(hours=8))

DISCOVERY_TYPES = ["new_creation", "near_completion", "completed"]
LAUNCHPADS = [
    "Pump.fun",
    "Moonshot",
    "moonshot_app",
    "letsbonk",
    "jup_studio",
    "bags",
    "believe",
    "heaven",
]

LAUNCHPAD_KEYS = {"pump": "pumpfun"}


def launchpad_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)
    return LAUNCHPAD_KEYS.get(compact, compact)


ALLOWED_LAUNCHPAD_KEYS = {launchpad_key(item) for item in LAUNCHPADS}


def is_allowed_launchpad(value: Any) -> bool:
    return launchpad_key(value) in ALLOWED_LAUNCHPAD_KEYS


def canonical_launchpad(value: Any) -> str:
    key = launchpad_key(value)
    for item in LAUNCHPADS:
        if launchpad_key(item) == key:
            return item
    return str(value or "")


def best_launchpad_value(mapping: Dict[str, Any]) -> Any:
    candidates: List[Any] = []
    for key in (
        "launchpad_platform",
        "launchpad",
        "platform",
        "source_platform",
        "pool_platform",
        "exchange",
        "migrated_pool_exchange",
    ):
        value = mapping.get(key)
        if value not in (None, ""):
            candidates.append(value)
    for value in candidates:
        if is_allowed_launchpad(value):
            return canonical_launchpad(value)
    return candidates[0] if candidates else ""


CSV_COLUMNS = [
    "address",
    "name",
    "symbol",
    "type",
    "北京时间",
    "time",
    "ln(age+1)",
    "launchpad",
    "price",
    "ln(price+1)",
    "price_2h_max/price",
    "price_2h_min/price",
    "liquidity/holder_count",
    "volume_1h/swaps_1h",
    "has_twitter",
    "has_website",
    "ln(image_dup+1)",
    "dexscr_update_link",
    "cto_flag",
    "ln(twitter_rename_count+1)",
    "ln(twitter_del_post_token_count+1)",
    "ln(twitter_create_token_count+1)",
    "top_10_holder_rate",
    "top_bot_degen_percentage",
    "fresh_wallet_rate",
    "bot_degen_rate",
    "price/ath_price",
    "stat.holder_count/market_cap",
    "ln(smart_degen_count+1)",
    "ln(renowned_count+1)",
    "entrapment_ratio",
    "dev_team_hold_rate",
    "top70_sniper_hold_rate",
    "ln(twitter_dup+1)",
    "ln(website_dup+1)",
    "ln(visiting_count+1)",
    "price_change_1h",
    "price_change_5m",
    "ln(creator_open_count+1)",
    "creator_open_ratio",
    "ln(top_wallets+1)",
    "tag",
]

LEGACY_COLUMN_MAP = {
    # Do not migrate old 4h/6h window values into 2h columns; those horizons are not comparable.
    "top_10_holder_rate": "stat.top_10_holder_rate",
    "top_bot_degen_percentage": "stat.top_bot_degen_percentage",
    "fresh_wallet_rate": "stat.fresh_wallet_rate",
    "bot_degen_rate": "stat.bot_degen_rate",
    "creator_open_ratio": "dev.creator_open_ratio",
    "ln(smart_degen_count+1)": "smart_degen_count",
    "ln(renowned_count+1)": "renowned_count",
    "ln(twitter_dup+1)": "twitter_dup",
    "ln(website_dup+1)": "website_dup",
    "ln(visiting_count+1)": "visiting_count",
    "ln(creator_open_count+1)": "creator_open_count",
    "ln(top_wallets+1)": "top_wallets",
}

ALLOWED_EMPTY_OUTPUT = {
    "name",
    "symbol",
    "launchpad",
    "price_2h_max/price",
    "price_2h_min/price",
    "ln(image_dup+1)",
    "dexscr_update_link",
    "cto_flag",
    "ln(twitter_rename_count+1)",
    "ln(twitter_del_post_token_count+1)",
    "ln(twitter_create_token_count+1)",
    "top_10_holder_rate",
    "top_bot_degen_percentage",
    "fresh_wallet_rate",
    "bot_degen_rate",
    "price/ath_price",
    "stat.holder_count/market_cap",
    "ln(smart_degen_count+1)",
    "ln(renowned_count+1)",
    "entrapment_ratio",
    "dev_team_hold_rate",
    "top70_sniper_hold_rate",
    "ln(twitter_dup+1)",
    "ln(website_dup+1)",
    "ln(visiting_count+1)",
    "price_change_1h",
    "price_change_5m",
    "ln(creator_open_count+1)",
    "creator_open_ratio",
    "ln(top_wallets+1)",
    "tag",
}

PREFILTERS = {
    "max_rug_ratio": 0.2,
    "max_insider_ratio": 0.2,
    "max_bundler_rate": 0.2,
    "min_liquidity": 4800,
    "min_top_holder_rate": 0.14,
    "max_top_holder_rate": 0.25,
    "max_fresh_wallet_rate": 0.2,
    "renounced_mint": 1,
    "renounced_freeze_account": 1,
    "min_holder_count": 30,
    "max_holder_count": 999,
    "min_marketcap": 5000,
}

GMGN_LEAKY_BUCKET_RATE = 20.0
GMGN_DOC_SAFETY_MULTIPLIER = 2.0
GMGN_DATA_API_IP_RPS = 2.0
GMGN_RATE_LIMIT_BUFFER_SECONDS = 15.0
DEFAULT_KLINE_BATCH_COOLDOWN_SECONDS = 4.0
DEFAULT_KLINE_MAX_ATTEMPTS_PER_ROW = 3
DEFAULT_KLINE_POLL_SECONDS = 120
PRICE_WINDOW_HOURS = 2
PRICE_WINDOW_SECONDS = PRICE_WINDOW_HOURS * 3600
PRICE_TAKE_PROFIT_X = 1.6
PRICE_STOP_LOSS_X = 0.9
PRICE_FINAL_WIN_X = 1.25
PRICE_SAME_BAR_CONFLICT_X = 1.25
PRICE_WINDOW_OUTPUT_COLUMNS = ("price_2h_max/price", "price_2h_min/price", "price_change_1h", "price_change_5m", "tag")


class CollectorError(RuntimeError):
    pass


class RateLimitError(CollectorError):
    def __init__(self, message: str, reset_at: Optional[int] = None):
        super().__init__(message)
        self.reset_at = reset_at


def now_bjt() -> datetime:
    return datetime.now(BJT)


def bjt_string(dt: Optional[datetime] = None) -> str:
    return (dt or now_bjt()).strftime("%Y/%m/%d %H:%M:%S")


def bjt_display_string(dt: Optional[datetime] = None) -> str:
    """CSV-facing Beijing time display: MM/DD HH:MM."""
    return (dt or now_bjt()).strftime("%m/%d %H:%M")


def bjt_display_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), BJT).strftime("%m/%d %H:%M")


def bjt_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(ts, BJT).strftime("%Y/%m/%d %H:%M:%S")


def log(message: str) -> None:
    stamp = bjt_string()
    text = f"[{stamp}] {message}"
    print(text, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def parse_env(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        env[key] = value
        os.environ.setdefault(key, value)
    return env


def load_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    for path in (PARENT_ENV_PATH, ENV_PATH):
        env.update(parse_env(path))
    return env


def scan_gmgn_keys(env: Dict[str, str]) -> List[str]:
    keys: Dict[int, str] = {}
    csv_value = env.get("GMGN_API_KEY") or os.environ.get("GMGN_API_KEY") or ""
    for idx, value in enumerate([v.strip() for v in csv_value.split(",") if v.strip()], start=1):
        keys[idx] = value
    for key, value in {**os.environ, **env}.items():
        if not key.startswith("GMGN_API_KEY_"):
            continue
        try:
            idx = int(key.rsplit("_", 1)[1])
        except Exception:
            continue
        if value:
            keys[idx] = str(value).strip()
    return [keys[i] for i in sorted(keys)]


def to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except Exception:
        return None


def to_int(value: Any) -> Optional[int]:
    f = to_float(value)
    if f is None:
        return None
    try:
        return int(f)
    except Exception:
        return None


def is_blank(value: Any) -> bool:
    return value is None or value == ""


def format_float(value: Any, digits: int = 10) -> str:
    f = to_float(value)
    if f is None:
        return ""
    return f"{f:.{digits}g}"


def safe_ln_pos(value: Any) -> str:
    """ln(x). None/'' stay empty; x <= 0 stays empty because ln is undefined/non-useful here."""
    f = to_float(value)
    if f is None or f <= 0:
        return ""
    return f"{math.log(f):.10g}"


def safe_ln1p_nonnegative(value: Any) -> str:
    """ln(x+1). None/'' stay empty; true 0 becomes 0; negative values stay empty."""
    f = to_float(value)
    if f is None or f < 0:
        return ""
    return f"{math.log1p(f):.10g}"


def ratio_value(numerator: Any, denominator: Any) -> Optional[float]:
    n = to_float(numerator)
    d = to_float(denominator)
    if n is None or d in (None, 0):
        return None
    return n / d


def ratio_ln(numerator: Any, denominator: Any) -> str:
    return safe_ln_pos(ratio_value(numerator, denominator))


def nested_first_present(obj: Any, parent_keys: Sequence[str], child_keys: Sequence[str]) -> Any:
    """Find child key under a named parent dict; fallback to recursive child search."""
    if isinstance(obj, dict):
        for parent in parent_keys:
            value = obj.get(parent)
            if isinstance(value, dict):
                child = first_present(value, child_keys)
                if child not in (None, ""):
                    return child
        for value in obj.values():
            found = nested_first_present(value, parent_keys, child_keys)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = nested_first_present(item, parent_keys, child_keys)
            if found not in (None, ""):
                return found
    return recursive_find(obj, child_keys)


def twitter_rename_count_from_source(source: Dict[str, Any]) -> Any:
    direct = nested_first_present(source, ["dev", "developer"], ["twitter_rename_count", "twitterRenameCount"])
    if direct not in (None, ""):
        return to_int(direct) if to_int(direct) is not None else direct
    history = nested_first_present(source, ["dev", "developer"], ["twitter_name_change_history", "twitterNameChangeHistory"])
    if isinstance(history, list):
        return len(history)
    return ""


def price_change_from_price(current_price: Any, source: Dict[str, Any], price_keys: Sequence[str]) -> str:
    """Compute current/old - 1 from price_5m or price_1h style fields only; no percent fields."""
    current = to_float(current_price)
    old = to_float(recursive_find(source, price_keys))
    if current is None or not old:
        return ""
    return f"{(current / old) - 1.0:.10g}"


def historical_change_from_klines(entry_price: float, klines: List[Dict[str, Any]], target_ts: int) -> str:
    """Use the last close at or before target_ts to compute entry/old - 1."""
    best_ts: Optional[int] = None
    best_close: Optional[float] = None
    for item in klines:
        ts = kline_time(item)
        if ts is None or ts > target_ts:
            continue
        _, _, close = kline_high_low_close(item)
        if close is None:
            continue
        if best_ts is None or ts > best_ts:
            best_ts = ts
            best_close = close
    if not best_close:
        return ""
    return f"{(entry_price / best_close) - 1.0:.10g}"


def truthy_int(value: Any) -> int:
    if value is None or value == "" or value is False:
        return 0
    if isinstance(value, str):
        if value.strip().lower() in {"0", "false", "none", "null", "no"}:
            return 0
    return 1


def recursive_find(obj: Any, keys: Sequence[str], depth: int = 0) -> Any:
    if obj is None or depth > 8:
        return None
    if isinstance(obj, dict):
        for key in keys:
            if key in obj and obj[key] not in (None, ""):
                return obj[key]
        for value in obj.values():
            found = recursive_find(value, keys, depth + 1)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = recursive_find(item, keys, depth + 1)
            if found not in (None, ""):
                return found
    return None


def recursive_find_all_dicts(obj: Any, depth: int = 0) -> List[Dict[str, Any]]:
    if depth > 8:
        return []
    if isinstance(obj, dict):
        out = [obj]
        for value in obj.values():
            out.extend(recursive_find_all_dicts(value, depth + 1))
        return out
    if isinstance(obj, list):
        out: List[Dict[str, Any]] = []
        for item in obj:
            out.extend(recursive_find_all_dicts(item, depth + 1))
        return out
    return []


def first_present(mapping: Dict[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


def merge_dicts(*items: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        for d in recursive_find_all_dicts(item):
            for k, v in d.items():
                if isinstance(v, (dict, list)):
                    continue
                if k not in merged or merged[k] in (None, ""):
                    merged[k] = v
        for k, v in item.items():
            if k not in merged:
                merged[k] = v
    return merged


def extract_items(data: Any, preferred: Sequence[str] = ("items", "list", "rows", "tokens", "data")) -> List[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in preferred:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_items(value, preferred)
            if nested:
                return nested
    inner = data.get("data")
    if inner is not data:
        nested = extract_items(inner, preferred)
        if nested:
            return nested
    return []


def route_weight(path: str) -> int:
    p = (path or "").lower()
    if "token_kline" in p:
        return 2
    if "market/rank" in p:
        return 1
    if "trenches" in p:
        return 3
    if "token_top_holders" in p or "token_top_traders" in p:
        return 5
    if "created_tokens" in p:
        return 2
    if "token/info" in p or "token/security" in p or "token/pool_info" in p:
        return 1
    return 1


def route_doc_cooldown_seconds(path: str) -> float:
    return route_weight(path) / GMGN_LEAKY_BUCKET_RATE * GMGN_DOC_SAFETY_MULTIPLIER


def extract_reset_at(data: Any, headers: Any = None) -> Optional[int]:
    candidates: List[Any] = []
    if headers is not None:
        for key in ("X-RateLimit-Reset", "x-ratelimit-reset"):
            try:
                value = headers.get(key)
            except Exception:
                value = None
            if value:
                candidates.append(value)
    if isinstance(data, dict):
        candidates.extend([
            data.get("reset_at"),
            data.get("resetAt"),
            data.get("rate_limit_reset"),
            data.get("retry_after"),
        ])
        for key in ("error", "data"):
            nested = data.get(key)
            if isinstance(nested, dict):
                candidates.extend([nested.get("reset_at"), nested.get("resetAt")])
    text = str(data)
    match = re.search(r"reset_at['\"]?\s*[:=]\s*([0-9]{10})", text)
    if match:
        candidates.append(match.group(1))
    for value in candidates:
        try:
            ts = int(float(value))
        except Exception:
            continue
        if ts > 10_000_000:
            return ts
    return None


def is_rate_limit_payload(status_code: int, data: Any) -> bool:
    if status_code == 429:
        return True
    text = str(data).lower()
    if any(mark in text for mark in ("rate_limit", "rate limit", "temporarily banned", "too many requests")):
        return True
    if isinstance(data, dict) and str(data.get("code")) == "429":
        return True
    return False


def extract_trench_items_by_type(data: Any) -> List[Tuple[str, Dict[str, Any]]]:
    if not isinstance(data, dict):
        return []
    inner = data.get("data", data)
    if not isinstance(inner, dict):
        return []
    out: List[Tuple[str, Dict[str, Any]]] = []
    type_keys = [
        ("new_creation", "new_creation"),
        ("pump", "near_completion"),
        ("near_completion", "near_completion"),
        ("completed", "completed"),
    ]
    for raw_key, token_type in type_keys:
        section = inner.get(raw_key)
        if isinstance(section, dict):
            items = extract_items(section, ("items", "list", "rows", "tokens"))
        elif isinstance(section, list):
            items = section
        else:
            items = []
        for item in items:
            if isinstance(item, dict):
                out.append((token_type, item))
    if not out:
        for item in extract_items(inner):
            if isinstance(item, dict):
                out.append((str(item.get("type") or ""), item))
    return out


def parse_time_to_seconds(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return int(num / 1000 if num > 10_000_000_000 else num)
    s = str(value).strip()
    if s.isdigit():
        return parse_time_to_seconds(int(s))
    try:
        cleaned = s.replace("Z", "+00:00")
        return int(datetime.fromisoformat(cleaned).timestamp())
    except Exception:
        return None


def age_minutes(raw: Dict[str, Any]) -> str:
    ts = parse_time_to_seconds(first_present(raw, [
        "pool_created_at",
        "creation_time",
        "created_at",
        "open_time",
        "launch_time",
        "created_timestamp",
        "creation_timestamp",
    ]))
    if ts is None:
        return ""
    return f"{max(0, int(time.time()) - ts) / 60.0:.2f}"


def normalize_token(raw: Dict[str, Any], token_type: str = "") -> Dict[str, Any]:
    merged = dict(raw or {})
    out = {
        "address": first_present(merged, ["token_mint", "token_address", "address", "mint", "base_address"]),
        "pool_address": first_present(merged, ["pool_address", "pair_address", "pair", "address_pair"]),
        "name": first_present(merged, ["name", "base_name"]),
        "symbol": first_present(merged, ["symbol", "base_symbol"]),
        "type": token_type or first_present(merged, ["type", "trench_type", "category"], ""),
        "launchpad": best_launchpad_value(merged),
        "price": to_float(first_present(merged, ["price", "price_usd", "usd_price"])),
        "liquidity": to_float(first_present(merged, ["liquidity", "liquidity_usd", "pool_liquidity_usd", "reserve_usd"])),
        "holder_count": to_float(first_present(merged, ["holder_count", "holders", "total_holders", "holder"])),
        "marketcap": to_float(first_present(merged, ["market_cap", "marketcap", "fdv", "fully_diluted_valuation", "usd_market_cap"])),
        "top_10_holder_rate": to_float(first_present(merged, ["top_10_holder_rate", "top10_holder_rate", "top10_holder_percent", "top_10_rate"])),
        "top1_holder_rate": to_float(first_present(merged, ["top1_holder_rate", "top_1_holder_rate", "top_holder_rate"])),
        "fresh_wallet_rate": to_float(first_present(merged, ["fresh_wallet_rate", "fresh_wallets_rate", "fresh_rate"])),
        "rug_ratio": to_float(first_present(merged, ["rug_ratio", "max_rug_ratio", "max_rugged_ratio", "top_rug_percentage"])),
        "insider_ratio": to_float(first_present(merged, [
            "max_insider_ratio",
            "max-insider-ratio",
            "insider_ratio",
            "insider_ratio_max",
            "max_insider_rate",
            "insider_rate",
            "suspected_insider_hold_rate",
            "suspected_insider_rate",
            "top_insider_percentage",
            "top_insider_trader_percentage",
            "insider_trader_amount_rate",
            "insider_amount_rate",
        ])),
        "bundler_rate": to_float(first_present(merged, [
            "bundler_rate",
            "bundler_trader_amount_rate",
            "max_bundler_rate",
            "top_bundler_trader_percentage",
        ])),
        "burn_status": first_present(merged, ["burn_status", "lp_burn_status", "burnt_status"]),
        "renounced_mint": first_present(merged, ["renounced_mint", "mint_renounced", "is_mint_renounced"]),
        "renounced_freeze_account": first_present(merged, [
            "renounced_freeze_account",
            "freeze_renounced",
            "is_freeze_renounced",
            "freeze_authority_renounced",
        ]),
        "is_wash_trading": first_present(merged, ["is_wash_trading", "wash_trading", "wash_trading_detected", "is_wash"]),
        "rat_trader_amount_rate": to_float(first_present(merged, [
            "rat_trader_amount_rate",
            "rat_trader_rate",
            "top_rat_trader_percentage",
        ])),
        "sell_tax": to_float(first_present(merged, ["sell_tax", "sell_tax_rate", "sell_tax_percent"])),
        "buy_tax": to_float(first_present(merged, ["buy_tax", "buy_tax_rate", "buy_tax_percent"])),
        "sniper_count": to_float(first_present(merged, ["sniper_count", "snipers", "sniper_trader_count"])),
        "swaps_1h": to_float(first_present(merged, ["swaps_1h", "swaps1h", "trade_1h", "trades_1h"])),
        "volume_1h": to_float(first_present(merged, ["volume_1h", "volume1h", "volume_1h_usd", "volume_h1"])),
        "volume": to_float(first_present(merged, ["volume", "volume_usd", "volume_24h", "volume_h24"])),
        "smart_degen_count": to_float(first_present(merged, ["smart_degen_count", "smartDegenCount"])),
        "renowned_count": to_float(first_present(merged, ["renowned_count", "renownedCount"])),
        "entrapment_ratio": to_float(first_present(merged, [
            "entrapment_ratio",
            "max_entrapment_ratio",
            "top_entrapment_trader_percentage",
        ])),
        "age": age_minutes(merged),
    }
    return out


def is_false(value: Any) -> bool:
    if value is False:
        return True
    if value in (0, "0"):
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "no", "none", "null"}:
        return True
    return False


def passes_basic_filters(t: Dict[str, Any]) -> Tuple[bool, List[str]]:
    fail: List[str] = []
    token_type = str(t.get("type") or "")

    def gt(name: str, value: Optional[float], limit: float) -> None:
        if value is None or not value > limit:
            fail.append(f"{name}>{limit}")

    def lt(name: str, value: Optional[float], limit: float) -> None:
        if value is None or not value < limit:
            fail.append(f"{name}<{limit}")

    platform = str(t.get("launchpad") or "")
    if platform and not is_allowed_launchpad(platform):
        fail.append("launchpad")
    lt("rug_ratio", t.get("rug_ratio"), 0.2)
    lt("insider_ratio", t.get("insider_ratio"), 0.2)
    lt("bundler_rate", t.get("bundler_rate"), 0.2)
    gt("liquidity", t.get("liquidity"), 4800)
    top10 = t.get("top_10_holder_rate")
    if top10 is None or not (0.14 < top10 < 0.25):
        fail.append("top_10_holder_rate")
    lt("fresh_wallet_rate", t.get("fresh_wallet_rate"), 0.2)
    if str(t.get("burn_status") or "").lower() != "burn":
        fail.append("burn_status")
    if token_type != "completed":
        if str(t.get("renounced_mint")) not in {"1", "True", "true"} and t.get("renounced_mint") != 1:
            fail.append("renounced_mint")
        if str(t.get("renounced_freeze_account")) not in {"1", "True", "true"} and t.get("renounced_freeze_account") != 1:
            fail.append("renounced_freeze_account")
    if not is_false(t.get("is_wash_trading")):
        fail.append("is_wash_trading")
    lt("rat_trader_amount_rate", t.get("rat_trader_amount_rate"), 0.2)
    holder = t.get("holder_count")
    if holder is None or not (29 < holder < 1000):
        fail.append("holder_count")
    gt("marketcap", t.get("marketcap"), 5000)
    lt("sell_tax", t.get("sell_tax"), 0.025)
    lt("buy_tax", t.get("buy_tax"), 0.025)
    lt("sniper_count", t.get("sniper_count"), 10)
    gt("age", to_float(t.get("age")), 3)
    if not t.get("liquidity") or not holder or t["liquidity"] / holder <= 50:
        fail.append("liquidity/holder_count")
    swaps_1h = t.get("swaps_1h")
    volume_1h = t.get("volume_1h")
    gt("swaps_1h", swaps_1h, 19)
    if not swaps_1h or not volume_1h or volume_1h / swaps_1h <= 31:
        fail.append("volume_1h/swaps_1h")
    smart = t.get("smart_degen_count") or 0
    renowned = t.get("renowned_count") or 0
    volume = t.get("volume") or volume_1h or 0
    if (0.5 + smart + renowned) * volume <= 5000:
        fail.append("(0.5+smart+renowned)*volume")
    return not fail, fail


def top1_addr_type0_rate(holders: List[Dict[str, Any]]) -> Optional[float]:
    for item in holders:
        addr_type = first_present(item, ["addr_type", "address_type", "type"], 0)
        try:
            addr_type_i = int(addr_type)
        except Exception:
            addr_type_i = 0
        if addr_type_i != 0:
            continue
        return to_float(first_present(item, ["top1_holder_rate", "rate", "amount_percentage", "percentage", "hold_rate"]))
    return None


def passes_top_holder_filter(holders: List[Dict[str, Any]]) -> Tuple[bool, Optional[float]]:
    rate = top1_addr_type0_rate(holders)
    return rate is not None and 0.028 < rate < 0.056, rate


@dataclass
class ApiSlot:
    index: int
    key: str


@dataclass
class ApiConfig:
    env: Dict[str, str]
    base_url: str
    trenches_path: str
    token_info_path: str
    token_security_path: str
    token_pool_path: str
    top_holders_path: str
    kline_path: str
    trending_path: str
    created_tokens_path: str
    timeout: float
    slots: List[ApiSlot]


@dataclass
class ActiveToken:
    address: str
    entry_time_utc: int
    entry_price: float
    row_time: str
    max_ratio: float = 1.0
    min_ratio: float = 1.0
    first_2x_at: Optional[int] = None
    first_075x_at: Optional[int] = None
    finalized: bool = False


class GMGNClient:
    def __init__(self, config: ApiConfig):
        if httpx is None:
            raise CollectorError(f"httpx is required: {HTTPX_IMPORT_ERROR}")
        self.config = config
        self.client = httpx.AsyncClient(timeout=config.timeout)
        self.rate_limit_pause_until = 0.0
        self.global_rps = float(config.env.get("GMGN_GLOBAL_RPS") or os.environ.get("GMGN_GLOBAL_RPS") or GMGN_DATA_API_IP_RPS)
        self._global_rate_lock = asyncio.Lock()
        self._last_request_monotonic = 0.0
        self._slot_locks: Dict[int, asyncio.Lock] = {slot.index: asyncio.Lock() for slot in config.slots}
        self._realtime_rr = 0

    def slot_busy(self, slot: ApiSlot) -> bool:
        lock = self._slot_locks.setdefault(slot.index, asyncio.Lock())
        return lock.locked()

    async def acquire_global_rate_slot(self) -> None:
        """GMGN data crawling is treated as a shared per-IP limit; default 2 requests/sec."""
        if self.global_rps <= 0:
            return
        async with self._global_rate_lock:
            interval = 1.0 / self.global_rps
            now = time.monotonic()
            wait_seconds = self._last_request_monotonic + interval - now
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            self._last_request_monotonic = time.monotonic()

    async def close(self) -> None:
        await self.client.aclose()

    def build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"

    async def wait_for_global_pause(self) -> None:
        while True:
            remain = self.rate_limit_pause_until - time.time()
            if remain <= 0:
                return
            log(f"GMGN rate limit 冷却中，等待 {remain:.1f}s，预计 {bjt_from_unix(int(self.rate_limit_pause_until))} 恢复")
            await asyncio.sleep(min(remain, 30.0))

    def note_rate_limit(self, reset_at: Optional[int], reason: str = "") -> None:
        target = float(reset_at or (int(time.time()) + 300)) + GMGN_RATE_LIMIT_BUFFER_SECONDS
        if target > self.rate_limit_pause_until:
            self.rate_limit_pause_until = target
        when = bjt_from_unix(int(self.rate_limit_pause_until))
        log(f"GMGN 触发限流/封禁，暂停所有 GMGN 请求到 {when}: {reason[:220]}")

    async def request(self, slot: ApiSlot, path: str, params: Optional[Dict[str, Any]] = None,
                      method: str = "GET", json_body: Optional[Dict[str, Any]] = None,
                      timeout: Optional[float] = None) -> Dict[str, Any]:
        lock = self._slot_locks.setdefault(slot.index, asyncio.Lock())
        async with lock:
            await self.wait_for_global_pause()
            await self.acquire_global_rate_slot()
            clean = {k: v for k, v in dict(params or {}).items() if v is not None and v != ""}
            auth = {"timestamp": int(time.time()), "client_id": str(uuid.uuid4())}
            headers = {"X-APIKEY": slot.key, "x-api-key": slot.key, "Content-Type": "application/json"}
            url = self.build_url(path)
            method = method.upper()
            try:
                if method == "POST":
                    response = await self.client.post(url, params={**clean, **auth}, json=json_body or {}, headers=headers, timeout=timeout)
                else:
                    response = await self.client.get(url, params={**clean, **auth}, headers=headers, timeout=timeout)
            except Exception as exc:
                detail = str(exc) or repr(exc) or type(exc).__name__
                raise CollectorError(f"request failed slot={slot.index} path={path}: {detail}") from exc
            try:
                data = response.json()
            except Exception:
                data = {"raw_text": response.text[:1000]}
            if is_rate_limit_payload(response.status_code, data):
                reset_at = extract_reset_at(data, response.headers)
                message = f"GMGN rate limit slot={slot.index} path={path}: {str(data)[:500]}"
                self.note_rate_limit(reset_at, message)
                raise RateLimitError(message, reset_at=reset_at)
            if response.status_code >= 400:
                raise CollectorError(f"GMGN HTTP {response.status_code} slot={slot.index} path={path}: {str(data)[:500]}")
            if isinstance(data, dict):
                code = data.get("code")
                if code not in (None, 0, "0", "success", "SUCCESS"):
                    message = str(data.get("message") or data.get("msg") or data.get("error") or "")
                    if message and "success" not in message.lower():
                        if is_rate_limit_payload(response.status_code, data):
                            reset_at = extract_reset_at(data, response.headers)
                            self.note_rate_limit(reset_at, message)
                            raise RateLimitError(message, reset_at=reset_at)
                        raise CollectorError(f"GMGN response code={code} slot={slot.index} path={path}: {message[:300]}")
                return data
            return {"data": data}

    async def request_same_slot_with_retries(self, slot: ApiSlot, path: str,
                                             params: Optional[Dict[str, Any]] = None,
                                             method: str = "GET",
                                             json_body: Optional[Dict[str, Any]] = None,
                                             attempts: int = 1,
                                             delay_seconds: float = 0.0,
                                             timeout: Optional[float] = None) -> Dict[str, Any]:
        last: Optional[BaseException] = None
        for attempt in range(max(1, attempts)):
            try:
                return await self.request(slot, path, params=params, method=method, json_body=json_body, timeout=timeout)
            except RateLimitError as exc:
                last = exc
                await self.wait_for_global_pause()
            except Exception as exc:
                last = exc
                if attempt < attempts - 1 and delay_seconds > 0:
                    log(f"request retry slot={slot.index} path={path} after {delay_seconds:.1f}s: {str(exc)[:180]}")
                    await asyncio.sleep(delay_seconds)
        raise CollectorError(f"request failed slot={slot.index} path={path} after {attempts} attempt(s): {str(last)[:300]}")

    async def request_realtime_with_fallback(self, primary_slots: Sequence[ApiSlot], fallback_slots: Sequence[ApiSlot],
                                             path: str, params: Optional[Dict[str, Any]] = None,
                                             method: str = "GET", json_body: Optional[Dict[str, Any]] = None,
                                             timeout: Optional[float] = None) -> Dict[str, Any]:
        if not primary_slots:
            raise CollectorError("no realtime primary API slots available")
        primary = primary_slots[self._realtime_rr % len(primary_slots)]
        self._realtime_rr += 1
        try:
            # Initial call + 3 retries on the same API, with 5s intervals.
            return await self.request_same_slot_with_retries(
                primary, path, params=params, method=method, json_body=json_body,
                attempts=4, delay_seconds=5.0, timeout=timeout,
            )
        except Exception as primary_exc:
            last: BaseException = primary_exc
            for i, fallback in enumerate(fallback_slots):
                if i > 0:
                    await asyncio.sleep(10.0)
                try:
                    return await self.request_same_slot_with_retries(
                        fallback, path, params=params, method=method, json_body=json_body,
                        attempts=1, delay_seconds=0.0, timeout=timeout,
                    )
                except Exception as exc:
                    last = exc
                    log(f"realtime fallback failed slot={fallback.index} path={path}: {str(exc)[:180]}")
            raise CollectorError(f"realtime request failed after primary+fallback path={path}: {str(last)[:300]}")

    async def request_kline_with_policy(self, primary_slot: ApiSlot, fallback_slots: Sequence[ApiSlot],
                                        address: str, from_ts: int, to_ts: int, limit: int = 500) -> List[Dict[str, Any]]:
        params = {
            "chain": "sol",
            "address": address,
            "resolution": "1m",
            "from": int(from_ts) * 1000,
            "to": int(to_ts) * 1000,
            "limit": int(limit),
        }
        try:
            data = await self.request_same_slot_with_retries(
                primary_slot,
                self.config.kline_path,
                params=params,
                attempts=3,
                delay_seconds=10.0,
                timeout=max(self.config.timeout, 30.0),
            )
            return [x for x in extract_items(data, ("klines", "list", "items", "rows", "data")) if isinstance(x, dict)]
        except Exception as primary_exc:
            last: BaseException = primary_exc
            idle_fallbacks = [slot for slot in fallback_slots if not self.slot_busy(slot)]
            if not idle_fallbacks:
                raise CollectorError(f"kline primary failed and fallback slots are busy: {str(last)[:260]}") from primary_exc
            for fallback in idle_fallbacks:
                try:
                    data = await self.request_same_slot_with_retries(
                        fallback,
                        self.config.kline_path,
                        params=params,
                        attempts=1,
                        timeout=max(self.config.timeout, 30.0),
                    )
                    return [x for x in extract_items(data, ("klines", "list", "items", "rows", "data")) if isinstance(x, dict)]
                except Exception as exc:
                    last = exc
                    log(f"kline idle fallback failed slot={fallback.index}: {str(exc)[:180]}")
            raise CollectorError(f"kline request failed after primary+idle fallback: {str(last)[:300]}")

    async def request_with_slots_until_success(self, slots: Sequence[ApiSlot], path: str,
                                               params: Optional[Dict[str, Any]] = None,
                                               method: str = "GET",
                                               json_body: Optional[Dict[str, Any]] = None,
                                               sleep_seconds: float = 3.0,
                                               max_attempts: Optional[int] = None) -> Dict[str, Any]:
        if not slots:
            raise CollectorError("no feature API slots available")
        attempt = 0
        last = ""
        while True:
            slot = slots[attempt % len(slots)]
            try:
                return await self.request(slot, path, params=params, method=method, json_body=json_body)
            except RateLimitError as exc:
                last = str(exc)
                attempt += 1
                if max_attempts is not None and attempt >= max_attempts:
                    raise CollectorError(f"feature request rate limited after {attempt} attempt(s): {last[:300]}") from exc
                await self.wait_for_global_pause()
            except Exception as exc:
                last = str(exc)
                attempt += 1
                if max_attempts is not None and attempt >= max_attempts:
                    raise CollectorError(f"feature request failed after {attempt} attempt(s): {last[:300]}") from exc
                log(f"feature request failed, retrying in {sleep_seconds}s: slot={slot.index}, path={path}, err={last[:180]}")
                await asyncio.sleep(sleep_seconds)

    async def discover_type(self, token_type: str, primary: ApiSlot, reserve: ApiSlot,
                            limit: int, prefilter: bool = True) -> List[Tuple[str, Dict[str, Any]]]:
        section: Dict[str, Any] = {
            "filters": ["offchain", "onchain"],
            "launchpad_platform_v2": True,
            "quote_address_type": [4, 5, 3, 1, 13, 0],
            "limit": limit,
            "launchpad_platform": LAUNCHPADS,
        }
        if prefilter:
            section.update(PREFILTERS)
        body = {"version": "v2", token_type: section}

        last_err = ""
        for i in range(3):  # initial call + 2 retries on API0/1/2
            try:
                data = await self.request(primary, self.config.trenches_path, {"chain": "sol"}, method="POST", json_body=body)
                return extract_trench_items_by_type(data)
            except Exception as exc:
                last_err = str(exc)
                if i < 2:
                    log(f"{token_type} discovery failed on primary slot={primary.index}; retry after 10s")
                    await asyncio.sleep(10)
        try:
            data = await self.request(reserve, self.config.trenches_path, {"chain": "sol"}, method="POST", json_body=body)
            return extract_trench_items_by_type(data)
        except Exception as exc:
            last_err = str(exc)
        raise CollectorError(f"{token_type} discovery failed after primary retries + one fallback: {last_err[:300]}")

    async def token_info_bundle(self, address: str, realtime_slots: Sequence[ApiSlot],
                                realtime_fallback_slots: Sequence[ApiSlot],
                                max_attempts: Optional[int] = None) -> Dict[str, Any]:
        params = {"chain": "sol", "address": address}
        bundle: Dict[str, Any] = {}
        for label, path in [
            ("token_info", self.config.token_info_path),
            ("security", self.config.token_security_path),
            ("pool", self.config.token_pool_path),
        ]:
            if not path:
                continue
            try:
                bundle[label] = await self.request_realtime_with_fallback(realtime_slots, realtime_fallback_slots, path, params=params)
            except Exception as exc:
                # Preserve missing fields as empty downstream; do not convert API failure to zeros.
                bundle[label] = {}
                log(f"{label} realtime fetch failed for {address[:8]}, fields may stay empty: {str(exc)[:180]}")
        return bundle

    async def top_holders(self, address: str, realtime_slots: Sequence[ApiSlot], realtime_fallback_slots: Sequence[ApiSlot],
                          limit: int = 20, max_attempts: Optional[int] = None) -> List[Dict[str, Any]]:
        data = await self.request_realtime_with_fallback(
            realtime_slots,
            realtime_fallback_slots,
            self.config.top_holders_path,
            params={"chain": "sol", "address": address, "limit": limit},
        )
        return [x for x in extract_items(data, ("holders", "list", "items", "rows", "data")) if isinstance(x, dict)]

    async def kline(self, address: str, feature_slots: Sequence[ApiSlot], from_ts: int, to_ts: int,
                    limit: int = 500) -> List[Dict[str, Any]]:
        data = await self.request_with_slots_until_success(
            feature_slots,
            self.config.kline_path,
            params={
                "chain": "sol",
                "address": address,
                "resolution": "1m",
                "from": int(from_ts) * 1000,
                "to": int(to_ts) * 1000,
                "limit": limit,
            },
        )
        return [x for x in extract_items(data, ("klines", "list", "items", "rows", "data")) if isinstance(x, dict)]

    async def kline_with_slot(self, slot: ApiSlot, address: str, from_ts: int, to_ts: int,
                              limit: int = 500) -> List[Dict[str, Any]]:
        data = await self.request(
            slot,
            self.config.kline_path,
            params={
                "chain": "sol",
                "address": address,
                "resolution": "1m",
                "from": int(from_ts) * 1000,
                "to": int(to_ts) * 1000,
                "limit": int(limit),
            },
            timeout=max(self.config.timeout, 30.0),
        )
        return [x for x in extract_items(data, ("klines", "list", "items", "rows", "data")) if isinstance(x, dict)]

    async def trending_optional(self, feature_slots: Sequence[ApiSlot], limit: int = 100) -> Dict[str, Any]:
        if not self.config.trending_path:
            return {}
        merged: Dict[str, Dict[str, Any]] = {}
        # /v1/market/rank is capped at 100 rows per request. Pull all supported
        # orderings to recover rank-only fields for more candidates.
        for order_by in (
            "default",
            "swaps",
            "marketcap",
            "history_highest_market_cap",
            "liquidity",
            "volume",
            "holder_count",
            "smart_degen_count",
            "renowned_count",
            "gas_fee",
            "price",
            "change1m",
            "change5m",
            "change1h",
            "creation_timestamp",
        ):
            try:
                data = await self.request_with_slots_until_success(
                    feature_slots,
                    self.config.trending_path,
                    params={"chain": "sol", "interval": "1h", "limit": limit, "order_by": order_by},
                    max_attempts=max(1, min(len(feature_slots), 3)),
                )
                for item in extract_items(data, ("rank", "items", "list", "rows", "tokens", "data")):
                    if not isinstance(item, dict):
                        continue
                    address = address_from_mapping(item)
                    if not address:
                        continue
                    existing = merged.setdefault(address, {})
                    for k, v in item.items():
                        if existing.get(k) in (None, "") and v not in (None, ""):
                            existing[k] = v
            except Exception as exc:
                log(f"optional rank fetch skipped order_by={order_by}: {str(exc)[:180]}")
        return {"data": {"rank": list(merged.values())}}

    async def created_tokens_optional(self, creator: str, realtime_slots: Sequence[ApiSlot],
                                      realtime_fallback_slots: Sequence[ApiSlot]) -> Dict[str, Any]:
        if not creator or not self.config.created_tokens_path:
            return {}
        try:
            return await self.request_realtime_with_fallback(
                realtime_slots,
                realtime_fallback_slots,
                self.config.created_tokens_path,
                params={"chain": "sol", "wallet_address": creator, "order_by": "token_ath_mc", "direction": "desc"},
            )
        except Exception as exc:
            log(f"optional created-tokens fetch skipped for {creator[:8]}: {str(exc)[:180]}")
            return {}


def build_config(args: argparse.Namespace) -> ApiConfig:
    env = load_env()
    keys = scan_gmgn_keys(env)
    if len(keys) < 12:
        raise CollectorError("Need at least 12 GMGN API keys: API0-2 discovery, API3 discovery fallback, API4-7 realtime, API8-9 realtime fallback, API10-11 kline.")
    base_url = env.get("GMGN_API_BASE_URL") or os.environ.get("GMGN_API_BASE_URL") or ""
    if not base_url:
        raise CollectorError("GMGN_API_BASE_URL is missing in .env")
    slots = [ApiSlot(i, key) for i, key in enumerate(keys)]
    return ApiConfig(
        env=env,
        base_url=base_url,
        trenches_path=env.get("GMGN_TRENCHES_PATH", "/v1/trenches"),
        token_info_path=env.get("GMGN_TOKEN_INFO_PATH", "/v1/token/info"),
        token_security_path=env.get("GMGN_TOKEN_SECURITY_PATH", "/v1/token/security"),
        token_pool_path=env.get("GMGN_TOKEN_POOL_INFO_PATH", "/v1/token/pool_info"),
        top_holders_path=env.get("GMGN_TOKEN_HOLDERS_PATH", "/v1/market/token_top_holders"),
        kline_path=env.get("GMGN_KLINE_PATH") or env.get("GMGN_TOKEN_KLINE_PATH", "/v1/market/token_kline"),
        trending_path=env.get("GMGN_TRENDING_PATH", "/v1/market/rank"),
        created_tokens_path=env.get("GMGN_PORTFOLIO_CREATED_TOKENS_PATH", "/v1/user/created_tokens"),
        timeout=float(env.get("GMGN_TIMEOUT_SECONDS") or args.timeout),
        slots=slots,
    )


def read_csv_rows() -> List[Dict[str, str]]:
    if not CSV_PATH.exists():
        return []
    last_exc: Optional[BaseException] = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with CSV_PATH.open("r", encoding=encoding, newline="") as f:
                reader = csv.DictReader(f)
                return [dict(row) for row in reader]
        except UnicodeDecodeError as exc:
            last_exc = exc
            continue
    raise CollectorError(f"cannot decode CSV {CSV_PATH}: {last_exc}")


def parse_bjt_datetime_string(raw: str) -> Optional[datetime]:
    raw = str(raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            if fmt.endswith("%H:%M"):
                dt = dt.replace(second=30)
            return dt.replace(tzinfo=BJT)
        except Exception:
            continue
    # CSV display-only format. Assume current BJT year for legacy recovery only.
    for fmt in ("%m/%d %H:%M:%S", "%m/%d %H:%M", "%m-%d %H:%M:%S", "%m-%d %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            if fmt.endswith("%H:%M"):
                dt = dt.replace(second=30)
            return dt.replace(year=now_bjt().year, tzinfo=BJT)
        except Exception:
            continue
    return None


def normalize_csv_timestamp(value: Any, display_value: Any = "") -> str:
    ts = parse_time_to_seconds(value)
    if ts is not None:
        return str(ts)
    dt = parse_bjt_datetime_string(str(value or "")) or parse_bjt_datetime_string(str(display_value or ""))
    if dt is not None:
        return str(int(dt.timestamp()))
    return ""


def normalize_bjt_display(value: Any, fallback_ts: Any = "") -> str:
    raw = str(value or "").strip()
    dt = parse_bjt_datetime_string(raw)
    if dt is not None:
        return dt.strftime("%m/%d %H:%M")
    ts = parse_time_to_seconds(fallback_ts)
    if ts is not None:
        return bjt_display_from_unix(ts)
    return raw


def normalize_existing_csv_time_to_30(value: Any) -> str:
    ts = parse_time_to_seconds(value)
    if ts is not None:
        dt = datetime.fromtimestamp(ts, BJT).replace(second=30)
        return str(int(dt.timestamp()))
    dt = parse_bjt_datetime_string(str(value or ""))
    if dt is not None:
        return str(int(dt.replace(second=30).timestamp()))
    return str(value or "")


def write_csv_rows(rows: List[Dict[str, Any]]) -> None:
    tmp = CSV_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            normalized: Dict[str, Any] = {}
            legacy_time = row.get("time", "")
            for col in CSV_COLUMNS:
                value = row.get(col, "")
                if value == "" and col == "ln(age+1)":
                    legacy_age = to_float(row.get("age"))
                    if legacy_age is not None:
                        try:
                            value = safe_ln1p_nonnegative(math.exp(legacy_age))
                        except OverflowError:
                            value = ""
                if value == "" and col == "ln(price+1)":
                    value = safe_ln1p_nonnegative(row.get("price"))
                if value == "" and col in LEGACY_COLUMN_MAP:
                    value = row.get(LEGACY_COLUMN_MAP[col], "")
                normalized[col] = value
            normalized["time"] = normalize_csv_timestamp(normalized.get("time"), row.get("北京时间") or legacy_time)
            normalized["北京时间"] = normalize_bjt_display(normalized.get("北京时间"), normalized.get("time") or legacy_time)
            writer.writerow(normalized)
    tmp.replace(CSV_PATH)


def append_or_update_row(row: Dict[str, Any]) -> None:
    rows = read_csv_rows()
    address = str(row.get("address") or "")
    found = False
    for idx, existing in enumerate(rows):
        if existing.get("address") == address:
            merged = dict(existing)
            merged.update({k: v for k, v in row.items() if k in CSV_COLUMNS})
            rows[idx] = merged
            found = True
            break
    if not found:
        rows.append({col: row.get(col, "") for col in CSV_COLUMNS})
    write_csv_rows(rows)


def active_address_set(active: Dict[str, ActiveToken]) -> set[str]:
    return {item.address for item in active.values() if not item.finalized}


def active_key(address: str, row_time: str) -> str:
    return f"{address}|{row_time}"


def append_new_row(row: Dict[str, Any]) -> bool:
    if not str(row.get("address") or ""):
        return False
    rows = read_csv_rows()
    rows.append({col: row.get(col, "") for col in CSV_COLUMNS})
    write_csv_rows(rows)
    return True


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"active": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"active": {}}


def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def active_from_state() -> Dict[str, ActiveToken]:
    state = load_state()
    active: Dict[str, ActiveToken] = {}
    for key, raw in (state.get("active") or {}).items():
        try:
            address = str(raw.get("address") or key.split("|", 1)[0])
            row_time = str(raw.get("row_time") or raw.get("time") or "")
            if not row_time and "|" in key:
                row_time = key.split("|", 1)[1]
            active[key] = ActiveToken(
                address=address,
                entry_time_utc=int(raw["entry_time_utc"]),
                entry_price=float(raw["entry_price"]),
                row_time=row_time,
                max_ratio=float(raw.get("max_ratio", 1)),
                min_ratio=float(raw.get("min_ratio", 1)),
                first_2x_at=raw.get("first_2x_at"),
                first_075x_at=raw.get("first_075x_at"),
                finalized=bool(raw.get("finalized", False)),
            )
        except Exception:
            continue
    return active


def save_active(active: Dict[str, ActiveToken]) -> None:
    state = {"active": {}}
    for key, item in active.items():
        if item.finalized:
            continue
        state["active"][key] = {
            "address": item.address,
            "row_time": item.row_time,
            "entry_time_utc": item.entry_time_utc,
            "entry_price": item.entry_price,
            "max_ratio": item.max_ratio,
            "min_ratio": item.min_ratio,
            "first_2x_at": item.first_2x_at,
            "first_075x_at": item.first_075x_at,
            "finalized": item.finalized,
        }
    save_state(state)


def latest_dashboard() -> None:
    rows = read_csv_rows()
    cutoff_ts = int((now_bjt() - timedelta(hours=6)).timestamp())
    recent: List[Tuple[int, str, str]] = []
    for row in rows:
        ts = row_entry_ts(row)
        if ts is None or ts < cutoff_ts:
            continue
        display = row.get("北京时间", "") or bjt_display_from_unix(ts)
        recent.append((ts, display, row.get("name", "") or row.get("symbol", "") or row.get("address", "")))
    recent = sorted(recent, reverse=True)[:30]
    log(f"当前已写入表格总池子数: {len(rows)}")
    if recent:
        log("最近6小时拉入的池子:")
        for _, display, name in recent:
            print(f"  - {display}  {name}", flush=True)


def output_ratio(numerator: Optional[float], denominator: Optional[float]) -> str:
    if numerator is None or denominator in (None, 0):
        return ""
    return f"{numerator / denominator:.10g}"


def output_value(value: Any) -> Any:
    return "" if value is None else value


def bool01(value: Any) -> Any:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"true", "1", "yes"}:
            return 1
        if s in {"false", "0", "no"}:
            return 0
    return 1 if bool(value) else 0


def decimal_change(current_price: float, source: Dict[str, Any], direct_keys: Sequence[str],
                   fallback_price_keys: Sequence[str]) -> Any:
    # Deprecated compatibility wrapper: only compute from actual historical prices.
    return price_change_from_price(current_price, source, fallback_price_keys)


def flatten_source(bundle: Dict[str, Any], trench: Dict[str, Any], trending: Dict[str, Any]) -> Dict[str, Any]:
    # Reuse the token_info/security/pool data already fetched during filtering; no second fetch at CSV write time.
    return merge_dicts(trench, bundle.get("token_info", {}), bundle.get("security", {}), bundle.get("pool", {}), trending or {})


def address_from_mapping(item: Dict[str, Any]) -> str:
    return str(first_present(item, ["address", "token_mint", "token_address", "mint", "base_address"], "") or "")


def trending_by_address(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for item in extract_items(data, ("rank", "items", "list", "rows", "tokens", "data")):
        if not isinstance(item, dict):
            continue
        address = address_from_mapping(item)
        if address:
            out[address] = item
    return out


def extract_link_value(source: Dict[str, Any], keys: Sequence[str]) -> Any:
    link = source.get("link")
    if isinstance(link, dict):
        val = first_present(link, list(keys))
        if val not in (None, ""):
            return val
    return recursive_find(source, keys)


def compute_feature_row(address: str, token_type: str, source: Dict[str, Any],
                        normalized: Dict[str, Any], created_tokens: Dict[str, Any]) -> Dict[str, Any]:
    now_ts = int(time.time())
    now_dt = datetime.fromtimestamp(now_ts, BJT)
    price = normalized.get("price") or to_float(first_present(source, ["price", "price_usd", "usd_price"])) or 0.0
    holder_count = normalized.get("holder_count")
    marketcap = normalized.get("marketcap")
    liquidity = normalized.get("liquidity")
    swaps_1h = normalized.get("swaps_1h")
    volume_1h = normalized.get("volume_1h")
    twitter = extract_link_value(source, ["twitter_username", "twitter", "twitter_url", "x"])
    website = extract_link_value(source, ["website", "web", "homepage"])
    price_change_1h = price_change_from_price(price, source, ["price_1h", "price1h", "price_h1"])
    price_change_5m = price_change_from_price(price, source, ["price_5m", "price5m", "price_m5"])
    creator_open_ratio = recursive_find(source, ["creator_open_ratio", "open_ratio"])
    if creator_open_ratio in (None, ""):
        creator_open_ratio = recursive_find(created_tokens, ["open_ratio", "creator_open_ratio"])
    ath_price = recursive_find(source, ["ath_price", "athPrice", "all_time_high_price", "history_highest_price", "highest_price"])
    price_ath_ratio = output_ratio(price, to_float(ath_price))
    twitter_create_count = first_present(source, ["twitter_create_token_count", "twitterCreateTokenCount"], "")
    stat_top10 = nested_first_present(source, ["stat", "stats"], ["top_10_holder_rate", "top10_holder_rate", "top10HolderRate"])
    stat_top_bot = nested_first_present(source, ["stat", "stats"], ["top_bot_degen_percentage", "topBotDegenPercentage"])
    stat_fresh = nested_first_present(source, ["stat", "stats"], ["fresh_wallet_rate", "freshWalletRate"])
    stat_bot = nested_first_present(source, ["stat", "stats"], ["bot_degen_rate", "botDegenRate"])
    bot_degen = stat_bot if stat_bot not in (None, "") else first_present(source, ["bot_degen_rate", "botDegenRate"], "")

    row = {
        "address": address,
        "name": normalized.get("name") or first_present(source, ["name", "base_name"], ""),
        "symbol": normalized.get("symbol") or first_present(source, ["symbol", "base_symbol"], ""),
        "type": token_type,
        "北京时间": bjt_display_string(now_dt),
        "time": str(now_ts),
        "ln(age+1)": safe_ln1p_nonnegative(normalized.get("age") or age_minutes(source)),
        "launchpad": normalized.get("launchpad") or best_launchpad_value(source),
        "price": f"{price:.16g}" if price else "",
        "ln(price+1)": safe_ln1p_nonnegative(price),
        "price_2h_max/price": "",
        "price_2h_min/price": "",
        "liquidity/holder_count": ratio_ln(liquidity, holder_count),
        "volume_1h/swaps_1h": ratio_ln(volume_1h, swaps_1h),
        "has_twitter": truthy_int(twitter),
        "has_website": truthy_int(website),
        "ln(image_dup+1)": safe_ln1p_nonnegative(recursive_find(source, ["image_dup", "image_duplicate", "imageDup"])),
        "dexscr_update_link": bool01(first_present(source, ["dexscr_update_link", "dexscreener_update_link", "dexscrUpdateLink"], "")),
        "cto_flag": bool01(first_present(source, ["cto_flag", "ctoFlag"], "")),
        "ln(twitter_rename_count+1)": safe_ln1p_nonnegative(twitter_rename_count_from_source(source)),
        "ln(twitter_del_post_token_count+1)": safe_ln1p_nonnegative(nested_first_present(source, ["dev", "developer"], ["twitter_del_post_token_count", "twitterDelPostTokenCount"])),
        "ln(twitter_create_token_count+1)": safe_ln1p_nonnegative(twitter_create_count),
        "top_10_holder_rate": output_value(stat_top10),
        "top_bot_degen_percentage": output_value(stat_top_bot),
        "fresh_wallet_rate": output_value(stat_fresh),
        "bot_degen_rate": output_value(bot_degen),
        "price/ath_price": price_ath_ratio,
        "stat.holder_count/market_cap": output_ratio(holder_count, marketcap),
        "ln(smart_degen_count+1)": safe_ln1p_nonnegative(normalized.get("smart_degen_count") if normalized.get("smart_degen_count") is not None else first_present(source, ["smart_degen_count"], "")),
        "ln(renowned_count+1)": safe_ln1p_nonnegative(normalized.get("renowned_count") if normalized.get("renowned_count") is not None else first_present(source, ["renowned_count"], "")),
        "entrapment_ratio": normalized.get("entrapment_ratio") if normalized.get("entrapment_ratio") is not None else first_present(source, ["entrapment_ratio"], ""),
        "dev_team_hold_rate": first_present(source, ["dev_team_hold_rate", "dev_hold_rate", "creator_hold_rate"], ""),
        "top70_sniper_hold_rate": first_present(source, ["top70_sniper_hold_rate", "top_70_sniper_hold_rate"], ""),
        "ln(twitter_dup+1)": safe_ln1p_nonnegative(first_present(source, ["twitter_dup", "twitter_duplicate"], "")),
        "ln(website_dup+1)": safe_ln1p_nonnegative(first_present(source, ["website_dup", "website_duplicate"], "")),
        "ln(visiting_count+1)": safe_ln1p_nonnegative(first_present(source, ["visiting_count", "visitingCount"], "")),
        "price_change_1h": price_change_1h,
        "price_change_5m": price_change_5m,
        "ln(creator_open_count+1)": safe_ln1p_nonnegative(recursive_find(source, ["creator_open_count", "open_count"])),
        "creator_open_ratio": output_value(creator_open_ratio),
        "ln(top_wallets+1)": safe_ln1p_nonnegative(recursive_find(source, ["top_wallets", "topWallets"])),
        "tag": "",
    }
    return row


async def enrich_and_filter(client: GMGNClient, raw_token: Dict[str, Any], token_type: str,
                            realtime_slots: Sequence[ApiSlot], realtime_fallback_slots: Sequence[ApiSlot],
                            trending_items: Dict[str, Dict[str, Any]],
                            max_feature_attempts: Optional[int] = None) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    trench_norm = normalize_token(raw_token, token_type)
    address = trench_norm.get("address")
    if not address:
        return None, ["missing_address"]

    bundle = await client.token_info_bundle(address, realtime_slots, realtime_fallback_slots, max_attempts=max_feature_attempts)
    source = flatten_source(bundle, raw_token, trending_items.get(address, {}))
    norm = normalize_token(source, token_type)
    if not norm.get("address"):
        norm["address"] = address

    ok, reasons = passes_basic_filters(norm)
    if not ok:
        return None, reasons

    holders = await client.top_holders(address, realtime_slots, realtime_fallback_slots, 20, max_attempts=max_feature_attempts)
    holders_ok, top1_rate = passes_top_holder_filter(holders)
    if not holders_ok:
        return None, [f"top1_addr_type0={top1_rate}"]

    creator = recursive_find(source, ["creator_address"]) or first_present(source, ["creator", "owner"], "")
    created_tokens = await client.created_tokens_optional(str(creator), realtime_slots, realtime_fallback_slots)
    row = compute_feature_row(address, token_type, source, norm, created_tokens)
    missing = [col for col in CSV_COLUMNS if row.get(col, "") in (None, "") and col not in ALLOWED_EMPTY_OUTPUT]
    if missing:
        return None, [f"output_missing:{','.join(missing)}"]
    return row, []


def kline_time(item: Dict[str, Any]) -> Optional[int]:
    return parse_time_to_seconds(first_present(item, ["open_time", "time", "timestamp", "t"]))


def kline_high_low_close(item: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    high = to_float(first_present(item, ["high", "h"]))
    low = to_float(first_present(item, ["low", "l"]))
    close = to_float(first_present(item, ["close", "c"]))
    return high, low, close


def row_entry_ts(row: Dict[str, Any]) -> Optional[int]:
    ts = parse_time_to_seconds(row.get("time"))
    if ts is not None:
        return ts
    dt = parse_bjt_datetime_string(str(row.get("北京时间") or ""))
    if dt is not None:
        return int(dt.timestamp())
    return None


def row_needs_price_window_finalize(row: Dict[str, Any], now_ts: Optional[int] = None) -> bool:
    entry_ts = row_entry_ts(row)
    if entry_ts is None:
        return False
    now_ts = now_ts or int(time.time())
    if now_ts < entry_ts + PRICE_WINDOW_SECONDS:
        return False
    return any(str(row.get(col, "")).strip() == "" for col in PRICE_WINDOW_OUTPUT_COLUMNS)


def due_price_window_rows(rows: List[Dict[str, Any]], now_ts: Optional[int] = None) -> List[Tuple[int, Dict[str, Any]]]:
    now_ts = now_ts or int(time.time())
    return [(idx, row) for idx, row in enumerate(rows) if row_needs_price_window_finalize(row, now_ts)]


def active_address_set_from_csv(rows: Optional[List[Dict[str, Any]]] = None) -> set[str]:
    rows = rows if rows is not None else read_csv_rows()
    out: set[str] = set()
    for row in rows:
        if str(row.get("tag") or "").strip() == "":
            address = str(row.get("address") or "")
            if address:
                out.add(address)
    return out


def compute_price_window_result_from_klines(row: Dict[str, Any], klines: List[Dict[str, Any]]) -> Dict[str, str]:
    entry_ts = row_entry_ts(row)
    if entry_ts is None:
        raise CollectorError(f"invalid row time: {row.get('time')}")
    to_ts = entry_ts + PRICE_WINDOW_SECONDS
    entry_price = to_float(row.get("price"))
    if not entry_price:
        raise CollectorError(f"invalid entry price for {row.get('address')}: {row.get('price')}")

    selected = []
    for item in klines:
        ts = kline_time(item)
        if ts is None or ts < entry_ts or ts > to_ts:
            continue
        selected.append(item)
    selected.sort(key=lambda x: kline_time(x) or 0)
    if not selected:
        raise CollectorError(f"empty kline range for {row.get('address')} from={entry_ts} to={to_ts}")

    max_ratio = 0.0
    min_ratio = float("inf")
    first_tp_at: Optional[int] = None
    first_sl_at: Optional[int] = None
    last_close: Optional[float] = None

    for item in selected:
        ts = kline_time(item)
        high, low, close = kline_high_low_close(item)
        high_v = high if high is not None else close
        low_v = low if low is not None else close
        if high_v is not None:
            max_ratio = max(max_ratio, high_v / entry_price)
        if low_v is not None:
            min_ratio = min(min_ratio, low_v / entry_price)
        if close is not None:
            last_close = close
        if ts is None:
            continue
        if first_sl_at is None and low_v is not None and low_v <= entry_price * PRICE_STOP_LOSS_X:
            first_sl_at = ts
        if first_tp_at is None and high_v is not None and high_v >= entry_price * PRICE_TAKE_PROFIT_X:
            first_tp_at = ts

    if max_ratio <= 0 or min_ratio == float("inf"):
        raise CollectorError(f"kline has no usable high/low for {row.get('address')}")

    if first_sl_at is not None and (first_tp_at is None or first_sl_at <= first_tp_at):
        tag = "0"
    elif first_tp_at is not None:
        tag = "1"
    else:
        final_ratio = (last_close / entry_price) if last_close is not None else max_ratio
        tag = "1" if final_ratio > PRICE_FINAL_WIN_X else "0"

    return {
        "address": str(row.get("address") or ""),
        "time": str(row.get("time") or ""),
        "price_2h_max/price": f"{max_ratio:.10g}",
        "price_2h_min/price": f"{min_ratio:.10g}",
        "price_change_5m": historical_change_from_klines(entry_price, klines, entry_ts - 5 * 60),
        "price_change_1h": historical_change_from_klines(entry_price, klines, entry_ts - 60 * 60),
        "tag": tag,
    }


async def fetch_price_window_result_once(client: GMGNClient, slot: ApiSlot, fallback_slots: Sequence[ApiSlot], row: Dict[str, Any]) -> Dict[str, str]:
    address = str(row.get("address") or "")
    entry_ts = row_entry_ts(row)
    if not address or entry_ts is None:
        raise CollectorError(f"cannot finalize row without address/time: {row}")
    from_ts = entry_ts - 60 * 60
    to_ts = entry_ts + PRICE_WINDOW_SECONDS
    klines = await client.request_kline_with_policy(slot, fallback_slots, address, from_ts, to_ts, limit=500)
    result = compute_price_window_result_from_klines(row, klines)
    log(f"{PRICE_WINDOW_HOURS}h K线补齐成功 slot={slot.index} {row.get('name') or address[:8]}")
    return result


def apply_price_window_results(results: List[Dict[str, str]]) -> int:
    if not results:
        return 0
    rows = read_csv_rows()
    applied = 0
    for result in results:
        row = next((r for r in rows if r.get("address") == result.get("address") and str(r.get("time")) == str(result.get("time"))), None)
        if not row:
            continue
        changed = False
        for col in PRICE_WINDOW_OUTPUT_COLUMNS:
            value = result.get(col, "")
            if value == "":
                continue
            # Backfill only empty price_change fields; max/min/tag are also written when empty/recomputed.
            if str(row.get(col, "")).strip() == "":
                row[col] = value
                changed = True
        if changed:
            applied += 1
    if applied:
        write_csv_rows(rows)
    return applied


async def finalize_due_price_window_rows_once(
    client: GMGNClient,
    kline_slots: Sequence[ApiSlot],
    kline_fallback_slots: Sequence[ApiSlot],
    batch_cooldown_seconds: float = DEFAULT_KLINE_BATCH_COOLDOWN_SECONDS,
    max_attempts_per_row: int = DEFAULT_KLINE_MAX_ATTEMPTS_PER_ROW,
    max_rows: Optional[int] = None,
) -> int:
    rows = read_csv_rows()
    due = due_price_window_rows(rows)
    if max_rows is not None and max_rows > 0:
        due = due[:max_rows]
    if not due:
        return 0
    slots = list(kline_slots)
    if not slots:
        raise CollectorError("no kline slots available for price-window kline finalization")
    results: List[Dict[str, str]] = []

    log(f"发现 {len(due)} 条超过{PRICE_WINDOW_HOURS}小时且未完成的记录，开始补齐K线")
    batch_size = len(slots)
    unresolved_total = 0
    for start in range(0, len(due), batch_size):
        batch = due[start:start + batch_size]
        pending: List[Tuple[int, Dict[str, Any], int]] = [(idx, row, 0) for idx, row in batch]
        batch_results: List[Dict[str, str]] = []
        round_no = 0
        while pending:
            assigned = pending[:batch_size]
            assigned_slots = [slots[(round_no + pos) % len(slots)].index for pos in range(len(assigned))]
            log(
                f"K线补齐批次 {start // batch_size + 1}: "
                f"{len(assigned)} 个池子并发拉取，slot={assigned_slots}"
            )

            async def run_one(pos: int, item: Tuple[int, Dict[str, Any], int]) -> Tuple[Tuple[int, Dict[str, Any], int], Any]:
                _, row, _ = item
                slot = slots[(round_no + pos) % len(slots)]
                try:
                    return item, await fetch_price_window_result_once(client, slot, kline_fallback_slots, row)
                except Exception as exc:
                    return item, exc

            gathered = await asyncio.gather(*(run_one(pos, item) for pos, item in enumerate(assigned)))
            failed: List[Tuple[int, Dict[str, Any], int]] = pending[batch_size:]
            rate_limit_seen = False
            for item, outcome in gathered:
                idx, row, attempts = item
                if isinstance(outcome, dict):
                    batch_results.append(outcome)
                    continue
                err = outcome
                attempts += 1
                if isinstance(err, RateLimitError):
                    rate_limit_seen = True
                if max_attempts_per_row > 0 and attempts >= max_attempts_per_row:
                    unresolved_total += 1
                    log(
                        f"{PRICE_WINDOW_HOURS}h K线补齐暂时放弃，本轮保留空值等待下轮断点续跑 "
                        f"{str(row.get('address') or '')[:8]} attempts={attempts} err={str(err)[:220]}"
                    )
                else:
                    failed.append((idx, row, attempts))
                    log(
                        f"{PRICE_WINDOW_HOURS}h K线补齐失败，将换 slot 重试 "
                        f"{str(row.get('address') or '')[:8]} attempts={attempts} err={str(err)[:220]}"
                    )
            pending = failed
            if pending:
                if rate_limit_seen:
                    await client.wait_for_global_pause()
                log(f"K线补齐批次仍有 {len(pending)} 个池子未完成，冷却 {batch_cooldown_seconds:.1f}s 后继续")
                await asyncio.sleep(batch_cooldown_seconds)
            round_no += 1
        if batch_results:
            results.extend(batch_results)
            applied_now = apply_price_window_results(batch_results)
            log(f"K线补齐批次写回完成: {applied_now}/{len(batch)}")
        if start + batch_size < len(due):
            log(f"K线补齐进入下一批前冷却 {batch_cooldown_seconds:.1f}s")
            await asyncio.sleep(batch_cooldown_seconds)
    applied = len(results)
    log(f"本轮{PRICE_WINDOW_HOURS}小时K线补齐完成: {applied}/{len(due)}，保留空值等待下轮={unresolved_total}")
    return applied


async def kline_finalizer_loop(
    client: GMGNClient,
    kline_slots: Sequence[ApiSlot],
    kline_fallback_slots: Sequence[ApiSlot],
    interval_seconds: int = DEFAULT_KLINE_POLL_SECONDS,
    batch_cooldown_seconds: float = DEFAULT_KLINE_BATCH_COOLDOWN_SECONDS,
    max_attempts_per_row: int = DEFAULT_KLINE_MAX_ATTEMPTS_PER_ROW,
) -> None:
    while True:
        try:
            await finalize_due_price_window_rows_once(
                client,
                kline_slots,
                kline_fallback_slots,
                batch_cooldown_seconds=batch_cooldown_seconds,
                max_attempts_per_row=max_attempts_per_row,
            )
        except Exception as exc:
            log(f"{PRICE_WINDOW_HOURS}小时K线补齐循环异常，不影响发现池子: {str(exc)[:240]}")
        await asyncio.sleep(interval_seconds)


async def startup_test(client: GMGNClient, discovery_slots: Sequence[ApiSlot],
                       reserve_slot: ApiSlot, realtime_slots: Sequence[ApiSlot], realtime_fallback_slots: Sequence[ApiSlot],
                       limit: int) -> Dict[str, Any]:
    log("启动前完整测试开始：发现池子 -> 二筛 -> 补齐录表字段。")
    returned_any = False
    trending_items: Dict[str, Dict[str, Any]] = {}
    diagnostics: Dict[str, Any] = {"types": {}, "candidate_failures": []}
    for idx, token_type in enumerate(DISCOVERY_TYPES):
        primary = discovery_slots[idx]
        items = await client.discover_type(token_type, primary, reserve_slot, limit=limit, prefilter=True)
        typed = [(typ or token_type, item) for typ, item in items if (typ == token_type or not typ)]
        if items:
            returned_any = True
        diagnostics["types"][token_type] = {"returned": len(items), "typed": len(typed)}
        for typ, item in typed:
            row, reasons = await enrich_and_filter(
                client,
                item,
                token_type,
                realtime_slots,
                realtime_fallback_slots,
                trending_items,
                max_feature_attempts=max(3, len(realtime_slots) * 2),
            )
            if row:
                if row["address"] in active_address_set_from_csv():
                    log(f"启动测试成功，合格样本仍在{PRICE_WINDOW_HOURS}小时记录中，未重复录入: {row.get('name') or row.get('symbol') or row['address']}")
                    latest_dashboard()
                    diagnostics["success"] = True
                    diagnostics["deduped_active"] = row.get("address", "")
                    return diagnostics
                append_new_row(row)
                log(f"启动测试成功，已录入样本池子: {row.get('name') or row.get('symbol') or row['address']}")
                latest_dashboard()
                diagnostics["success"] = True
                return diagnostics
            diagnostics["candidate_failures"].append({
                "type": token_type,
                "address": first_present(item, ["token_mint", "address", "mint"], ""),
                "reasons": reasons[:8],
            })
    if not returned_any:
        raise CollectorError("启动测试失败：第一次拉取没有任何池子返回，按要求视为拉取流程异常。")
    hard_failures = [
        item for item in diagnostics["candidate_failures"]
        if any(str(reason).startswith("output_missing") for reason in item.get("reasons", []))
    ]
    if not hard_failures and diagnostics["candidate_failures"]:
        log(
            "启动测试未遇到字段缺失，但当前返回池子没有通过全套策略筛选；"
            "按策略空窗处理，继续正式轮询。最近失败原因: "
            f"{json.dumps(diagnostics['candidate_failures'][:10], ensure_ascii=False)}"
        )
        diagnostics["success"] = True
        diagnostics["strategy_empty_window"] = True
        return diagnostics
    raise CollectorError(
        "启动测试失败：API 返回了池子，但没有池子能完整通过筛选并录入。"
        f" 最近失败原因: {json.dumps(diagnostics['candidate_failures'][:10], ensure_ascii=False)}"
    )


async def discovery_loop(client: GMGNClient, discovery_slots: Sequence[ApiSlot], reserve_slot: ApiSlot,
                         realtime_slots: Sequence[ApiSlot], realtime_fallback_slots: Sequence[ApiSlot], args: argparse.Namespace) -> None:
    while True:
        started = time.time()
        trending_items: Dict[str, Dict[str, Any]] = {}
        seen = active_address_set_from_csv()
        for idx, token_type in enumerate(DISCOVERY_TYPES):
            primary = discovery_slots[idx]
            try:
                items = await client.discover_type(token_type, primary, reserve_slot, limit=args.limit, prefilter=True)
            except Exception as exc:
                log(f"{token_type} discovery cycle failed: {str(exc)[:300]}")
                continue
            log(f"{token_type} returned {len(items)} pools")
            for typ, item in items:
                address = first_present(item, ["token_mint", "token_address", "address", "mint", "base_address"], "")
                if not address or address in seen:
                    continue
                try:
                    row, reasons = await enrich_and_filter(client, item, token_type, realtime_slots, realtime_fallback_slots, trending_items)
                except Exception as exc:
                    log(f"candidate enrich failed {str(address)[:8]}: {str(exc)[:220]}")
                    continue
                if not row:
                    if args.verbose_rejects:
                        log(f"candidate rejected {str(address)[:8]}: {', '.join(reasons[:6])}")
                    continue
                append_new_row(row)
                seen.add(row["address"])
                log(f"录入新池子: {row.get('name') or row.get('symbol') or row['address']} ({token_type})")
                latest_dashboard()
        latest_dashboard()
        if args.once:
            log("--once 模式完成一个正式发现周期，退出。")
            return
        elapsed = time.time() - started
        await asyncio.sleep(max(1, args.poll_seconds - elapsed))


async def diagnose_rejections(client: GMGNClient, discovery_slots: Sequence[ApiSlot], reserve_slot: ApiSlot,
                              realtime_slots: Sequence[ApiSlot], realtime_fallback_slots: Sequence[ApiSlot], args: argparse.Namespace) -> None:
    log("拒绝原因诊断开始：按当前三类池子拉取并统计本地二筛淘汰率。")
    trending_items: Dict[str, Dict[str, Any]] = {}
    active_seen = active_address_set_from_csv()
    summary: Dict[str, Dict[str, Any]] = {}
    for idx, token_type in enumerate(DISCOVERY_TYPES):
        primary = discovery_slots[idx]
        items = await client.discover_type(token_type, primary, reserve_slot, limit=args.limit, prefilter=True)
        typed = [(typ or token_type, item) for typ, item in items if (typ == token_type or not typ)]
        counters: Counter[str] = Counter()
        accepted = 0
        active_duplicates = 0
        errors = 0
        for typ, item in typed:
            address = str(first_present(item, ["token_mint", "token_address", "address", "mint", "base_address"], ""))
            if address and address in active_seen:
                active_duplicates += 1
                counters["active_duplicate_unfinished"] += 1
                continue
            try:
                row, reasons = await enrich_and_filter(
                    client,
                    item,
                    token_type,
                    realtime_slots,
                    realtime_fallback_slots,
                    trending_items,
                    max_feature_attempts=max(3, len(realtime_slots)),
                )
            except Exception as exc:
                errors += 1
                counters[f"exception:{type(exc).__name__}"] += 1
                log(f"diagnose enrich exception {token_type} {address[:8]}: {str(exc)[:180]}")
                continue
            if row:
                accepted += 1
                continue
            if not reasons:
                counters["unknown_reject"] += 1
            for reason in reasons:
                counters[str(reason)] += 1
        total = len(typed)
        rows = []
        for reason, count in counters.most_common():
            rate = (count / total) if total else 0.0
            rows.append({"reason": reason, "count": count, "rate": rate})
        summary[token_type] = {
            "returned": len(items),
            "typed": total,
            "accepted": accepted,
            "active_duplicates": active_duplicates,
            "errors": errors,
            "rejects": rows,
        }
        log(f"诊断 {token_type}: returned={len(items)} typed={total} accepted={accepted} errors={errors}")
        for row in rows[:20]:
            log(f"  {token_type} 淘汰 {row['reason']}: {row['count']}/{total} = {row['rate']:.2%}")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GMGN meme training data collector")
    parser.add_argument("--limit", type=int, default=80, help="trenches limit per type; GMGN CLI docs list max 80")
    parser.add_argument("--poll-seconds", type=int, default=240, help="discovery poll interval; default 240s")
    parser.add_argument("--timeout", type=float, default=8.0, help="HTTP timeout")
    parser.add_argument("--skip-startup-test", action="store_true", help="skip required startup test; not recommended")
    parser.add_argument("--once", action="store_true", help="run one formal discovery cycle after startup test")
    parser.add_argument("--backfill-due-once", action="store_true", help="only finalize CSV rows whose price window is due, then exit")
    parser.add_argument("--kline-poll-seconds", type=int, default=DEFAULT_KLINE_POLL_SECONDS, help="kline backfill scan interval; default 120s")
    parser.add_argument("--kline-batch-cooldown", type=float, default=DEFAULT_KLINE_BATCH_COOLDOWN_SECONDS,
                        help="seconds to wait between price-window kline batches/retries; default follows GMGN docs plus safety floor")
    parser.add_argument("--kline-max-attempts-per-row", type=int, default=DEFAULT_KLINE_MAX_ATTEMPTS_PER_ROW,
                        help="max attempts per due row in one pass; 0 means infinite")
    parser.add_argument("--backfill-limit", type=int, default=0,
                        help="limit due price-window rows processed in this run; for cautious API recovery tests")
    parser.add_argument("--recompute-all-windows", action="store_true",
                        help="clear price-window columns and tag for all CSV rows, then recompute due rows")
    parser.add_argument("--diagnose-rejections", action="store_true",
                        help="fetch current pools and print rejection-rate ranking by token type")
    parser.add_argument("--verbose-rejects", action="store_true", help="print candidate rejection reasons")
    return parser.parse_args()


async def amain() -> None:
    args = parse_args()
    config = build_config(args)
    discovery_slots = config.slots[:3]
    reserve_slot = config.slots[3]
    realtime_slots = config.slots[4:8]
    realtime_fallback_slots = config.slots[8:10]
    kline_slots = config.slots[10:12]
    kline_fallback_slots = [config.slots[3], config.slots[8], config.slots[9]]
    log(
        "GMGN slots: "
        f"new_creation={discovery_slots[0].index}, "
        f"near_completion={discovery_slots[1].index}, "
        f"completed={discovery_slots[2].index}, "
        f"discovery_fallback={reserve_slot.index}, "
        f"realtime_primary={[s.index for s in realtime_slots]}, "
        f"realtime_fallback={[s.index for s in realtime_fallback_slots]}, "
        f"kline_primary={[s.index for s in kline_slots]}, "
        f"kline_idle_fallback={[s.index for s in kline_fallback_slots]}, "
        f"global_rps={GMGN_DATA_API_IP_RPS}"
    )
    client = GMGNClient(config)
    finalizer_task: Optional[asyncio.Task] = None
    try:
        if args.diagnose_rejections:
            await diagnose_rejections(client, discovery_slots, reserve_slot, realtime_slots, realtime_fallback_slots, args)
            return
        if args.recompute_all_windows:
            rows = read_csv_rows()
            for row in rows:
                row["time"] = normalize_existing_csv_time_to_30(row.get("time"))
                row["price_2h_max/price"] = ""
                row["price_2h_min/price"] = ""
                row["price_change_1h"] = ""
                row["price_change_5m"] = ""
                row["tag"] = ""
            write_csv_rows(rows)
            log(f"已清空 {len(rows)} 行的{PRICE_WINDOW_HOURS}小时价格窗口列和 tag，准备按新规则重算。")
        if args.backfill_due_once:
            await finalize_due_price_window_rows_once(
                client,
                kline_slots,
                kline_fallback_slots,
                batch_cooldown_seconds=args.kline_batch_cooldown,
                max_attempts_per_row=args.kline_max_attempts_per_row,
                max_rows=args.backfill_limit or None,
            )
            return
        await finalize_due_price_window_rows_once(
            client,
            kline_slots,
            kline_fallback_slots,
            batch_cooldown_seconds=args.kline_batch_cooldown,
            max_attempts_per_row=args.kline_max_attempts_per_row,
            max_rows=args.backfill_limit or None,
        )
        if not args.skip_startup_test:
            await startup_test(client, discovery_slots, reserve_slot, realtime_slots, realtime_fallback_slots, limit=args.limit)
        log(f"启动测试通过，开始正式 {args.poll_seconds} 秒轮询。")
        finalizer_task = asyncio.create_task(kline_finalizer_loop(
            client,
            kline_slots,
            kline_fallback_slots,
            args.kline_poll_seconds,
            batch_cooldown_seconds=args.kline_batch_cooldown,
            max_attempts_per_row=args.kline_max_attempts_per_row,
        ))
        await discovery_loop(client, discovery_slots, reserve_slot, realtime_slots, realtime_fallback_slots, args)
    finally:
        if finalizer_task is not None:
            finalizer_task.cancel()
            try:
                await finalizer_task
            except asyncio.CancelledError:
                pass
        await client.close()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log("收到 Ctrl+C，已停止。")
    except Exception as exc:
        log(f"脚本退出：{exc}")
        raise


if __name__ == "__main__":
    raise SystemExit(
        "量化训练采集.py is legacy H2 reference code and is disabled. "
        "Use backend.app.collector (H1, new_creation/near_completion only)."
    )
