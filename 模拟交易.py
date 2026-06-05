#!/usr/bin/env python
"""
模拟交易 - Solana Meme 币自动化模拟交易脚本
x=0.2, 每2分钟轮询GMGN trenches, 完整止盈止损, rich TUI看板
"""
import asyncio
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ["APP_ENV"] = "development"
os.environ["PROVIDER_MODE"] = "online_readonly"

import httpx
from rich.live import Live
from rich.table import Table
from rich import box

from backend.app.config import settings, ProviderMode
from backend.app.providers.gmgn_real import GMGNProvider, GMGNAPIError
from backend.app.providers.jupiter_real import JupiterProvider
from backend.app.strategy.thresholds import compute_thresholds, build_trench_filters_for_x
from backend.app.strategy.filters import (
    run_entry_local_risk_filter, evaluate_price_activity_rules,
    evaluate_smart_degen, evaluate_top1_holder, run_holding_risk_filter,
)

X = 0.2
SCREENING_INTERVAL = 120
PRICE_POLL_INTERVAL = 1.0
PRICE_CACHE_TTL = 2.0
SNAPSHOT_CACHE_TTL = 10.0
HOLDER_CACHE_TTL = 30.0
RETRY_ATTEMPTS = 3
RETRY_DELAY = 0.6

HARD_TP_160_MULTIPLE = 1.60
HARD_TP_210_MULTIPLE = 2.10
HARD_TP_250_MULTIPLE = 2.50
HARD_SL_75_MULTIPLE = 0.75
HARD_SL_55_MULTIPLE = 0.55

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

# ═══════════════════════════════════════════════════════════
#  Mock Repo
# ═══════════════════════════════════════════════════════════

class MockRepo:
    async def append_provider_request(self, *args, **kwargs): pass
    async def close(self): pass

# ═══════════════════════════════════════════════════════════
#  数据类型
# ═══════════════════════════════════════════════════════════

@dataclass
class SimPosition:
    token_mint: str
    symbol: str
    name: str
    entry_price_usd: float
    entry_size_usd: float
    entry_token_amount: float
    remaining_token_amount: float
    entry_time: float
    last_tx_time: float
    last_tx_price: float
    entry_liquidity_usd: float
    token_decimals: int
    decimals_verified: bool = False
    smart_money_initial: List[Dict] = field(default_factory=list)
    realized_pnl: float = 0.0
    executed_rules: Set[str] = field(default_factory=set)
    last_risk_check: float = 0.0
    last_smart_check: float = 0.0
    last_type_check: float = 0.0
    manual_entry: bool = False
    skipped_entry_rules: Set[str] = field(default_factory=set)

    @property
    def remaining_pct(self) -> float:
        if self.entry_token_amount <= 0: return 0.0
        return self.remaining_token_amount / self.entry_token_amount

    @property
    def cost_basis_remaining_usd(self) -> float:
        return self.entry_size_usd * self.remaining_pct

    def current_value_usd(self, price: float) -> float:
        return self.remaining_token_amount * price

@dataclass
class LogEntry:
    time_str: str
    tag: str
    symbol: str
    message: str

@dataclass
class ManualCandidate:
    token_mint: str
    symbol: str
    name: str
    token: Dict[str, Any]
    reason: str
    skipped_rules: Set[str] = field(default_factory=set)

# ═══════════════════════════════════════════════════════════
#  状态
# ═══════════════════════════════════════════════════════════

class SimState:
    def __init__(self):
        self.positions: Dict[str, SimPosition] = {}
        self.total_realized_pnl: float = 0.0
        self.total_invested: float = 0.0
        self.logs: List[LogEntry] = []
        self.trade_logs: List[LogEntry] = []
        self.latest_failed_candidates: List[ManualCandidate] = []
        self._candidate_seen: Set[str] = set()
        self.closed_trades: List[dict] = []
        self.shutdown_requested: bool = False
        self._price_cache: Dict[str, list] = {}     # mint -> [ts, dict]
        self._snap_cache: Dict[str, list] = {}
        self._sd_cache: Dict[str, list] = {}
        self._holder_cache: Dict[str, list] = {}

    def has_position(self, mint: str) -> bool:
        return mint in self.positions and self.positions[mint].remaining_token_amount > 0

    def get_active_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.remaining_token_amount > 0)

    def add_log(self, tag: str, symbol: str, message: str):
        entry = LogEntry(
            time_str=datetime.now().strftime("%H:%M:%S"), tag=tag,
            symbol=symbol or "??", message=message,
        )
        if tag in {"buy", "sell", "close"}:
            self.trade_logs.insert(0, entry)
        else:
            self.logs.insert(0, entry)

    def begin_candidate_round(self):
        self.latest_failed_candidates = []
        self._candidate_seen = set()

    def add_manual_candidate(self, token: Dict[str, Any], reason: str, skipped_rules: Set[str]):
        mint = token.get("token_mint", "")
        if not mint or mint in self._candidate_seen or self.has_position(mint):
            return
        self._candidate_seen.add(mint)
        symbol = token.get("symbol", "??") or "??"
        self.latest_failed_candidates.append(ManualCandidate(
            token_mint=mint,
            symbol=symbol,
            name=token.get("name", symbol) or symbol,
            token=dict(token),
            reason=reason,
            skipped_rules=set(skipped_rules or set()),
        ))

state = SimState()

# ═══════════════════════════════════════════════════════════
#  Provider
# ═══════════════════════════════════════════════════════════

mock_repo = MockRepo()
gmgn = GMGNProvider(mock_repo, mode=ProviderMode.ONLINE_READONLY)
jupiter = JupiterProvider(mock_repo, mode=ProviderMode.ONLINE_READONLY)

# ═══════════════════════════════════════════════════════════
#  工具
# ═══════════════════════════════════════════════════════════

def _short_mint(mint: str) -> str:
    return f"{mint[:4]}...{mint[-4:]}" if len(mint) > 12 else mint

def _to_float(v: Any, d: Optional[float] = None) -> Optional[float]:
    if v is None: return d
    try: return float(v)
    except: return d

def _now() -> float: return time.time()

def _cache_get(cache: dict, key: str, ttl: float) -> Optional[Any]:
    """Return cached value if valid, else None.  Works for falsy values too."""
    entry = cache.get(key)
    if entry is not None and _now() - entry[0] < ttl:
        return entry[1]
    return None

def _cache_set(cache: dict, key: str, value):
    cache[key] = [_now(), value]

async def _retry_call(label: str, op, attempts: int = RETRY_ATTEMPTS, delay: float = RETRY_DELAY):
    last_exc: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return await op()
        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                await asyncio.sleep(delay * (i + 1))
    raise last_exc or RuntimeError(f"{label} failed")

# ═══════════════════════════════════════════════════════════
#  API 包装
# ═══════════════════════════════════════════════════════════

async def fetch_price(mint: str) -> Optional[Dict[str, Any]]:
    c = _cache_get(state._price_cache, mint, PRICE_CACHE_TTL)
    if c is not None: return c
    try:
        info = await gmgn.fetch_latest_price(mint)
        if info:
            _cache_set(state._price_cache, mint, info)
        return info
    except Exception:
        return _cache_get(state._price_cache, mint, 9999) if mint in state._price_cache else None

async def fetch_snapshot_cached(mint: str) -> Dict[str, Any]:
    c = _cache_get(state._snap_cache, mint, SNAPSHOT_CACHE_TTL)
    if c is not None: return c
    for i in range(RETRY_ATTEMPTS):
        try:
            snap = await gmgn.fetch_token_snapshot(mint)
            _cache_set(state._snap_cache, mint, snap or {})
            return snap or {}
        except Exception:
            if i < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_DELAY * (i + 1))
    cached = _cache_get(state._snap_cache, mint, 9999)
    return cached if cached is not None else {}

async def fetch_holders_cached(mint: str, limit: int = 20) -> List[Dict]:
    c = _cache_get(state._holder_cache, mint, HOLDER_CACHE_TTL)
    if c is not None: return c
    for i in range(RETRY_ATTEMPTS):
        try:
            h = await gmgn.fetch_top_holders(mint, limit=limit)
            _cache_set(state._holder_cache, mint, h)
            return h
        except Exception:
            if i < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_DELAY * (i + 1))
    return _cache_get(state._holder_cache, mint, 9999) if mint in state._holder_cache else []

async def fetch_smart_degen_cached(mint: str, limit: int = 20) -> List[Dict]:
    c = _cache_get(state._sd_cache, mint, HOLDER_CACHE_TTL)
    if c is not None: return c
    for i in range(RETRY_ATTEMPTS):
        try:
            h = await gmgn.fetch_smart_degen_holders(mint, limit=limit)
            _cache_set(state._sd_cache, mint, h)
            return h
        except Exception:
            if i < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_DELAY * (i + 1))
    return _cache_get(state._sd_cache, mint, 9999) if mint in state._sd_cache else []

async def fetch_klines_for_fallback(mint: str, creation_ts: float) -> List[Dict]:
    try:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        from_ts = max(int(creation_ts), now_ts - 86400) if creation_ts else now_ts - 86400
        return await gmgn.fetch_kline(
            mint, "1m", 1440,
            from_ts=from_ts,
            to_ts=now_ts,
        )
    except Exception:
        return []

async def get_jupiter_buy_quote(mint: str, amount_usd: float) -> Optional[Dict]:
    for i in range(RETRY_ATTEMPTS):
        try:
            q = await jupiter.quote_exact_in(USDC_MINT, mint, int(amount_usd * 10**USDC_DECIMALS), slippage_bps=500)
            if q is not None:
                return q
        except Exception:
            pass
        if i < RETRY_ATTEMPTS - 1:
            await asyncio.sleep(RETRY_DELAY * (i + 1))
    return None

async def get_jupiter_sell_quote(mint: str, token_amount: float, decimals: int) -> Optional[Dict]:
    for i in range(RETRY_ATTEMPTS):
        try:
            q = await jupiter.quote_exact_in(mint, USDC_MINT, int(token_amount * (10**decimals)), slippage_bps=1000)
            if q is not None:
                return q
        except Exception:
            pass
        if i < RETRY_ATTEMPTS - 1:
            await asyncio.sleep(RETRY_DELAY * (i + 1))
    return None

# ═══════════════════════════════════════════════════════════
#  decimals 解析
# ═══════════════════════════════════════════════════════════

STAGE0_REQUIRED_ALIASES = {
    "renounced_mint": ["renounced_mint", "mint_renounced", "is_mint_renounced"],
    "renounced_freeze_account": ["renounced_freeze_account", "freeze_renounced", "is_freeze_renounced", "freeze_authority_renounced"],
    "is_wash_trading": ["is_wash_trading", "wash_trading", "wash_trading_detected"],
    "rat_trader_amount_rate": ["rat_trader_amount_rate", "rat_trader_rate"],
    "suspected_insider_hold_rate": ["suspected_insider_hold_rate", "insider_hold_rate", "max_insider_ratio"],
    "sell_tax": ["sell_tax", "sell_tax_rate"],
    "burn_status": ["burn_status", "lp_burn_status", "burnt_status"],
    "sniper_count": ["sniper_count", "snipers", "sniper_trader_count"],
}

def _first_dec(container: dict, *keys: str) -> Optional[int]:
    for k in keys:
        v = container.get(k)
        if v is not None:
            try:
                d = int(float(v))
                if 0 <= d <= 18:
                    return d
            except (TypeError, ValueError):
                pass
    return None

async def resolve_token_decimals(mint: str, snapshot: Dict, quote: Optional[Dict], entry_usd: float) -> Tuple[int, bool]:
    """从多种来源获取真实 decimals，返回 (decimals, verified)"""
    # 1) snapshot raw_json -> GMGN response -> decimals
    raw = snapshot.get("raw_json")
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                for container in (parsed, parsed.get("data", {}), parsed.get("token_info", {}).get("data", {})):
                    if isinstance(container, dict):
                        d = _first_dec(container, "decimals", "token_decimals", "mint_decimals")
                        if d is not None:
                            return d, True
        except Exception:
            pass

    # 2) snapshot top level
    d = _first_dec(snapshot, "decimals", "token_decimals")
    if d is not None:
        return d, True

    # 3) Jupiter quote + GMGN price: decimals = log10(outAmount * price / entry_usd)
    if quote and entry_usd > 0:
        out_raw = _to_float(quote.get("outAmount"))
        price = _to_float(snapshot.get("price_usd") or snapshot.get("price"))
        if out_raw and out_raw > 0 and price and price > 0:
            implied = math.log10(out_raw * price / entry_usd)
            if -2 < implied < 18:
                dec = max(0, min(round(implied), 18))
                return dec, False  # unverified

    return 6, False  # final fallback

# ═══════════════════════════════════════════════════════════
#  字段补齐 (snapshot enrich)
# ═══════════════════════════════════════════════════════════

async def enrich_token(token: Dict[str, Any]) -> Dict[str, Any]:
    """trenches 字段不全时，从 snapshot 按别名表补全"""
    mint = token.get("token_mint", "")
    if not mint:
        return token

    missing = []
    for canonical, aliases in STAGE0_REQUIRED_ALIASES.items():
        found = any(token.get(a) is not None and token.get(a) != "" for a in aliases)
        if not found:
            missing.append(canonical)

    if not missing:
        return token

    snap = await fetch_snapshot_cached(mint)
    if not snap:
        return token

    merged = dict(token)
    for canonical in missing:
        aliases = STAGE0_REQUIRED_ALIASES[canonical]
        for a in aliases:
            if a in snap and snap[a] is not None and snap[a] != "":
                merged[canonical] = snap[a]
                break

    return merged

# ═══════════════════════════════════════════════════════════
#  筛选 & 买入
# ═══════════════════════════════════════════════════════════

def _flatten_details(details) -> List[Dict]:
    flat = []
    for d in (details or []):
        if isinstance(d, dict):
            flat.append(d)
        elif hasattr(d, "__dict__"):
            flat.append({"rule": getattr(d, "name", str(d)),
                         "passed": getattr(d, "passed", False),
                         "value": getattr(d, "value", None),
                         "threshold": getattr(d, "threshold", None)})
    return flat

def _detail_rule(d: Dict[str, Any]) -> str:
    return str(d.get("rule") or d.get("name") or "?")

def _detail_value(d: Dict[str, Any]) -> Any:
    for key in ("value", "vps", "pct_change", "swaps_1h", "current_price"):
        if d.get(key) is not None:
            return d.get(key)
    return "?"

def _detail_threshold(d: Dict[str, Any]) -> Any:
    if d.get("threshold") is not None:
        return d.get("threshold")
    if d.get("lower_threshold") is not None or d.get("upper_threshold") is not None:
        return f"{d.get('lower_threshold')}~{d.get('upper_threshold')}"
    return "?"

def _format_fail_details(fails: List[Dict[str, Any]]) -> str:
    return " ❌ ".join(
        f"{_detail_rule(f)}={_detail_value(f)}(阈:{_detail_threshold(f)})" for f in fails
    )

def _failed_rules(fails: List[Dict[str, Any]]) -> Set[str]:
    return {_detail_rule(f) for f in fails if _detail_rule(f) != "?"}

def _record_fail_candidate(token: Dict[str, Any], message: str, rules: Set[str]) -> None:
    state.add_manual_candidate(token, message, rules)

async def open_sim_position(
    token: Dict[str, Any],
    price_info: Dict[str, Any],
    degens: Optional[List[Dict[str, Any]]] = None,
    *,
    manual: bool = False,
    skipped_rules: Optional[Set[str]] = None,
) -> bool:
    mint = token.get("token_mint", "")
    symbol = token.get("symbol", "??") or "??"
    if not mint:
        state.add_log("fail", symbol, "缺 mint,无法买入")
        return False
    if state.has_position(mint):
        state.add_log("system", symbol, "已有持仓,跳过买入")
        return False

    skipped_rules = set(skipped_rules or set())
    liq = _to_float(token.get("liquidity_usd") or price_info.get("liquidity_usd")) or 0
    entry_usd = min(liq * 0.01, 100.0) if liq > 0 else 100.0

    quote = await get_jupiter_buy_quote(mint, entry_usd)
    if not quote:
        state.add_log("fail", symbol, "Jupiter quote 失败,不买入")
        return False
    if quote.get("error") == "HIGH_PRICE_IMPACT":
        state.add_log("fail", symbol, "Jupiter high price impact,不买入")
        return False

    out_amount = _to_float(quote.get("outAmount"))
    if not out_amount or out_amount <= 0:
        state.add_log("fail", symbol, "Jupiter outAmount=0,不买入")
        return False

    snap = await fetch_snapshot_cached(mint)
    decimals, verified = await resolve_token_decimals(mint, snap, quote, entry_usd)
    if not verified:
        state.add_log("fail", symbol, f"decimals 未验证(推测={decimals}),跳过买入")
        return False

    if degens is None and "smart_degen" not in skipped_rules:
        degens = await fetch_smart_degen_cached(mint, 20)
    degens = degens or []

    token_amount = out_amount / (10 ** decimals)
    entry_price = entry_usd / token_amount if token_amount > 0 else 0

    pos = SimPosition(
        token_mint=mint, symbol=symbol, name=token.get("name", symbol) or symbol,
        entry_price_usd=entry_price, entry_size_usd=entry_usd,
        entry_token_amount=token_amount, remaining_token_amount=token_amount,
        entry_time=_now(), last_tx_time=_now(), last_tx_price=entry_price,
        entry_liquidity_usd=liq, token_decimals=decimals, decimals_verified=True,
        smart_money_initial=[
            {"address": h.get("address"), "amount_percentage": h.get("amount_percentage"),
             "usd_value": h.get("usd_value"), "sell_volume_cur": h.get("sell_volume_cur", 0)}
            for h in degens[:3]
        ],
        manual_entry=manual,
        skipped_entry_rules=skipped_rules,
    )
    state.positions[mint] = pos
    state.total_invested += entry_usd
    prefix = "手动买入" if manual else "买入"
    state.add_log("buy", symbol,
                  f"{prefix} ${entry_usd:.2f} → {token_amount:.6g} tokens @ ${entry_price:.8g}  decimals={decimals}")
    return True

async def run_screening() -> None:
    try:
        state.begin_candidate_round()
        filters = build_trench_filters_for_x(X)
        params = {
            "chain": "sol", "trench_filters": filters,
            "platforms": ["Pump.fun", "Moonshot", "moonshot_app", "letsbonk",
                          "memoo", "token_mill", "jup_studio", "bags", "believe", "heaven"],
        }
        tokens = await _retry_call("GMGN trenches", lambda: gmgn.fetch_trenches(params))
        if not tokens:
            return

        sg = {"x": X}

        for token in tokens:
            mint = token.get("token_mint", "")
            symbol = token.get("symbol", "??") or "??"
            if not mint:
                await asyncio.sleep(RETRY_DELAY)
                await asyncio.sleep(RETRY_DELAY * 2)
                state.add_log("fail", symbol, "缺 mint(重试2次后跳过)")
                continue
            if state.has_position(mint):
                continue

            token = await enrich_token(token)

            # ── Stage0+1 风控 ──
            try:
                risk_r = await _retry_call("entry_risk", lambda: run_entry_local_risk_filter(token, sg))
            except Exception as e:
                msg = f"风控异常(重试2次后跳过): {e}"
                state.add_log("fail", symbol, msg)
                _record_fail_candidate(token, msg, {"risk_exception"})
                continue
            if not all(d.get("passed") for d in _flatten_details(risk_r.details)):
                fails = [d for d in _flatten_details(risk_r.details) if not d.get("passed", False)]
                msg = _format_fail_details(fails) or "风控未通过"
                state.add_log("fail", symbol, msg)
                _record_fail_candidate(token, msg, _failed_rules(fails))
                continue

            # ── Stage2 价格面 (首次, 无 klines) ──
            price_info = await _retry_call("latest_price", lambda: gmgn.fetch_latest_price(mint))
            if not price_info:
                msg = "价格获取失败(重试2次后跳过)"
                state.add_log("fail", symbol, msg)
                _record_fail_candidate(token, msg, {"latest_price_present"})
                continue

            try:
                price_r = await _retry_call("price_rules", lambda: evaluate_price_activity_rules(token, sg, price_info, klines=None))
            except Exception as e:
                msg = f"价格异常(重试2次后跳过): {e}"
                state.add_log("fail", symbol, msg)
                _record_fail_candidate(token, msg, {"price_exception"})
                continue

            price_d = _flatten_details(price_r.details)
            price_ok = all(d.get("passed") for d in price_d)

            # ── kline fallback (年轻池) ──
            pct_detail = next((d for d in price_r.details if isinstance(d, dict) and d.get("rule") == "price_change_1h"), None)
            age_info = price_r.feature_vector if hasattr(price_r, "feature_vector") else {}
            age_min = _to_float(age_info.get("age_minutes"))
            range_detail = next((d for d in price_r.details if isinstance(d, dict) and d.get("rule") == "price_range_24h_percentile"), None)
            price_change_need_kline = (age_min is not None and age_min < 60 and
                                       pct_detail and pct_detail.get("age_mode") == "young_no_kline_fallback" and
                                       str(pct_detail.get("source")) == "missing")
            range_need_kline = bool(range_detail and range_detail.get("data_unavailable"))
            young_need_kline = price_change_need_kline or range_need_kline
            kline_fallback_used = False
            if young_need_kline:
                creation_ts = _to_float(age_info.get("creation_ts"))
                klines = await fetch_klines_for_fallback(mint, creation_ts or 0)
                if klines:
                    try:
                        price_r2 = await evaluate_price_activity_rules(token, sg, price_info, klines=klines)
                        price_d2 = _flatten_details(price_r2.details)
                        price_ok2 = all(d.get("passed") for d in price_d2)
                        if price_ok2:
                            price_ok = True
                            price_d = price_d2
                            kline_fallback_used = True
                    except Exception:
                        pass

            if not price_ok:
                fails = [d for d in price_d if not d.get("passed", False)]
                fb_info = " [kline fallback:通过]" if kline_fallback_used else (" [kline fallback:未通过]" if young_need_kline else "")
                msg = (_format_fail_details(fails) or "价格面未通过") + fb_info
                state.add_log("fail", symbol, msg)
                _record_fail_candidate(token, msg, _failed_rules(fails))
                continue

            # ── Top1 holder ──
            holders = await fetch_holders_cached(mint, 20)
            top1_candidate = next((h for h in holders if h.get("addr_type") == 0), None)
            top1_r = evaluate_top1_holder(top1_candidate, X)
            if not all(d.get("passed") for d in _flatten_details(top1_r.details)):
                msg = "Top1 holder 不满足"
                state.add_log("fail", symbol, msg)
                _record_fail_candidate(token, msg, {"top1_holder_addr_type0"})
                continue

            # ── 聪明钱 ──
            degens = await fetch_smart_degen_cached(mint, 20)
            try:
                degen_r = await _retry_call("smart_degen", lambda: evaluate_smart_degen(sg, degens))
            except Exception as e:
                msg = f"聪明钱异常(重试2次后跳过): {e}"
                state.add_log("fail", symbol, msg)
                _record_fail_candidate(token, msg, {"smart_degen_exception"})
                continue
            if not all(d.get("passed") for d in _flatten_details(degen_r.details)):
                msg = "聪明钱不满足"
                state.add_log("fail", symbol, msg)
                _record_fail_candidate(token, msg, {"smart_degen"})
                continue

            if not await open_sim_position(token, price_info, degens, manual=False):
                _record_fail_candidate(token, "买入执行失败", {"buy_execution"})

    except GMGNAPIError as e:
        state.add_log("system", "API", f"GMGN错误: {e}")
    except Exception as e:
        state.add_log("system", "筛选", f"异常: {e}")

# ═══════════════════════════════════════════════════════════
#  止盈止损（按优先级）
# ═══════════════════════════════════════════════════════════

@dataclass
class Trigger:
    rule_code: str
    text: str
    sell_ratio: float
    detail: Dict

async def monitor_positions() -> None:
    for mint, pos in list(state.positions.items()):
        if pos.remaining_token_amount <= 0:
            continue

        price_info = await fetch_price(mint)
        if not price_info:
            continue
        cur_price = _to_float(price_info.get("price_usd") or price_info.get("price"))
        if not cur_price or cur_price <= 0:
            continue

        multiple = cur_price / pos.entry_price_usd if pos.entry_price_usd > 0 else 0
        cur_val = pos.current_value_usd(cur_price)
        now = _now()

        triggers: List[Trigger] = []

        # 扫描间隔（按当前市值）
        if cur_val >= 150: ri = 2
        elif cur_val >= 100: ri = 4
        elif cur_val >= 50: ri = 8
        elif cur_val >= 25: ri = 16
        else: ri = 32

        # ── 类型检查 (completed) ──
        if "COMPLETED" not in pos.executed_rules and now - pos.last_type_check >= max(ri, 10):
            pos.last_type_check = now
            snap = await fetch_snapshot_cached(mint)
            if snap and str(snap.get("type", "")).lower() == "completed":
                triggers.append(Trigger("COMPLETED", "类型完成全清", 1.0, {"type": "completed"}))

        # ── 风险止损 ──
        if "RISK_STOP" not in pos.executed_rules and now - pos.last_risk_check >= ri:
            pos.last_risk_check = now
            snap = await fetch_snapshot_cached(mint)
            if snap:
                try:
                    hr = await run_holding_risk_filter(snap, {"x": X})
                    unskipped_fails = [
                        d for d in hr.details
                        if not d.passed and d.name not in pos.skipped_entry_rules
                    ]
                    if unskipped_fails:
                        fails = [f"{d.name}={d.value}" for d in unskipped_fails]
                        triggers.append(Trigger("RISK_STOP", "风险止损全清", 1.0, {"f": fails}))
                except Exception as e:
                    state.add_log("system", pos.symbol, f"持仓风控异常: {e}")

        # ── 硬止损55 (full) ──
        if multiple <= HARD_SL_55_MULTIPLE and "HARD_SL_55" not in pos.executed_rules:
            triggers.append(Trigger("HARD_SL_55", f"硬止损{HARD_SL_55_MULTIPLE}x全清", 1.0, {"m": multiple}))

        # ── 硬止盈250 (full) ──
        if multiple >= HARD_TP_250_MULTIPLE and "HARD_TP_250" not in pos.executed_rules:
            triggers.append(Trigger("HARD_TP_250", f"硬止盈{HARD_TP_250_MULTIPLE}x全清", 1.0, {"m": multiple}))

        # ── 硬止损75 (partial) ──
        if multiple <= HARD_SL_75_MULTIPLE and "HARD_SL_75" not in pos.executed_rules:
            triggers.append(Trigger("HARD_SL_75", f"硬止损{HARD_SL_75_MULTIPLE}x减半", 0.5, {"m": multiple}))

        # ── 聪明钱跟随 ──
        if pos.smart_money_initial and "SMART_EXIT" not in pos.executed_rules and now - pos.last_smart_check >= ri:
            pos.last_smart_check = now
            cur_d = await fetch_smart_degen_cached(mint, 20)
            if cur_d:
                cur_map = {h.get("address"): h for h in cur_d if h.get("address")}
                for init_h in pos.smart_money_initial:
                    addr = init_h.get("address")
                    if not addr or addr not in cur_map: continue
                    ip = _to_float(init_h.get("amount_percentage")) or 0
                    cp = _to_float(cur_map[addr].get("amount_percentage")) or 0
                    if ip > 0 and cp < ip * 0.75:
                        triggers.append(Trigger("SMART_EXIT", "聪明钱减仓>25%", 0.5, {"addr": addr[:8], "was": ip, "now": cp}))
                        break

        # ── 硬止盈210/160 (partial, 最后) ──
        if multiple >= HARD_TP_210_MULTIPLE and "HARD_TP_210" not in pos.executed_rules:
            triggers.append(Trigger("HARD_TP_210", f"硬止盈{HARD_TP_210_MULTIPLE}x减半", 0.5, {"m": multiple}))
        elif multiple >= HARD_TP_160_MULTIPLE and "HARD_TP_160" not in pos.executed_rules:
            triggers.append(Trigger("HARD_TP_160", f"硬止盈{HARD_TP_160_MULTIPLE}x减半", 0.5, {"m": multiple}))

        # ── 按优先级选择 ──
        if not triggers:
            continue

        priority = {"COMPLETED": 0, "RISK_STOP": 1, "HARD_SL_55": 2, "HARD_TP_250": 3,
                     "HARD_SL_75": 4, "SMART_EXIT": 5, "HARD_TP_210": 6, "HARD_TP_160": 7}
        triggers.sort(key=lambda t: priority.get(t.rule_code, 99))
        chosen = triggers[0]

        await sell_position(pos, chosen.sell_ratio, chosen.rule_code, chosen.text, chosen.detail)

    for mint in list(state.positions.keys()):
        if state.positions[mint].remaining_token_amount <= 0:
            state.positions.pop(mint)

# ═══════════════════════════════════════════════════════════
#  卖出
# ═══════════════════════════════════════════════════════════

async def sell_position(pos: SimPosition, sell_ratio: float,
                        rule_code: str, reason_text: str, detail: Dict) -> bool:
    """return True if sell executed, False if skipped/failed"""
    if pos.remaining_token_amount <= 0 or rule_code in pos.executed_rules:
        return False
    sell_ratio = min(sell_ratio, 1.0)
    sell_tokens = pos.remaining_token_amount * sell_ratio
    if sell_tokens <= 0:
        return False

    quote = await get_jupiter_sell_quote(pos.token_mint, sell_tokens, pos.token_decimals)
    if quote and not quote.get("error") and _to_float(quote.get("outAmount"), 0) > 0:
        sell_value_usd = _to_float(quote.get("outAmount"), 0) / 10**USDC_DECIMALS
    else:
        pi = state._price_cache.get(pos.token_mint)
        cp = pi[1].get("price_usd") or pi[1].get("price") if pi else None
        if not cp:
            state.add_log("system", pos.symbol, f"卖出失败({rule_code}): 无价格")
            return False
        sell_value_usd = sell_tokens * cp

    cost_basis_sold = pos.cost_basis_remaining_usd * sell_ratio if sell_ratio > 0 else 0
    pnl = sell_value_usd - cost_basis_sold

    pos.realized_pnl += pnl
    state.total_realized_pnl += pnl
    pos.remaining_token_amount -= sell_tokens
    pos.last_tx_time = _now()
    pos.last_tx_price = sell_value_usd / sell_tokens if sell_tokens > 0 else 0

    pos.executed_rules.add(rule_code)

    detail_text = ""
    if rule_code == "RISK_STOP":
        fails = detail.get("f") if isinstance(detail, dict) else None
        if fails:
            detail_text = " 指标不满足: " + ", ".join(str(x) for x in fails)[:300]

    if pos.remaining_token_amount <= 0:
        state.closed_trades.append({
            "symbol": pos.symbol, "mint": pos.token_mint,
            "entry_usd": pos.entry_size_usd, "total_pnl": pos.realized_pnl,
            "close_time": datetime.now().isoformat(),
        })
        state.add_log("close", pos.symbol,
                      f"清仓 {reason_text}{detail_text}  本轮盈亏:${pnl:+.2f}")
    else:
        state.add_log("sell", pos.symbol,
                      f"减仓{sell_ratio*100:.0f}% {reason_text}{detail_text}  盈亏:${pnl:+.2f}  剩{pos.remaining_pct*100:.0f}%")
    return True

# ═══════════════════════════════════════════════════════════
#  看板
# ═══════════════════════════════════════════════════════════

def render_dashboard() -> Table:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active = state.get_active_count()

    unrealized = 0.0
    for m, p in state.positions.items():
        if p.remaining_token_amount <= 0: continue
        pi_entry = state._price_cache.get(m)
        cp = pi_entry[1].get("price_usd") or pi_entry[1].get("price") if pi_entry else None
        if cp:
            unrealized += p.current_value_usd(cp) - p.cost_basis_remaining_usd
    total_pnl = state.total_realized_pnl + unrealized

    t = Table(title="模拟交易 Dashboard", box=box.HEAVY,
              border_style="cyan", title_style="bold cyan", padding=(0, 1))
    t.add_column("指标", style="bold yellow", no_wrap=True)
    t.add_column("数值", style="bold white")

    def c(v): return "green" if v >= 0 else "red"
    t.add_row("更新时间", now_str)
    t.add_row("总利润", f"[{c(total_pnl)}]${total_pnl:+.2f}[/]")
    t.add_row("  已实现", f"[{c(state.total_realized_pnl)}]${state.total_realized_pnl:+.2f}[/]")
    t.add_row("  未实现", f"[{c(unrealized)}]${unrealized:+.2f}[/]")
    t.add_row("总投入", f"${state.total_invested:.2f}")
    t.add_row("持仓数", f"{active}  已完结: {len(state.closed_trades)}")

    t.add_row("", "")
    t.add_row("[bold]当前持仓看板[/]", "", end_section=True)

    if active == 0:
        t.add_row("  (无持仓)", "等待开仓...")
    else:
        h = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        h.add_column("代币", style="bold cyan", width=10)
        h.add_column("买入$", justify="right", width=8)
        h.add_column("当前$", justify="right", width=8)
        h.add_column("涨跌%", justify="right", width=8)
        h.add_column("仓位", justify="right", width=6)
        h.add_column("状态", style="bold", width=12)

        for m, p in sorted(state.positions.items(), key=lambda x: x[1].entry_time, reverse=True):
            if p.remaining_token_amount <= 0: continue
            pi_entry = state._price_cache.get(m)
            cp = pi_entry[1].get("price_usd") or pi_entry[1].get("price") if pi_entry else None
            cv = p.current_value_usd(cp) if cp else 0
            pc = ((cp / p.entry_price_usd) - 1) * 100 if cp and p.entry_price_usd > 0 else 0
            pcs = "green" if pc >= 0 else "red"

            status = "持仓中"
            for rn in ["HARD_TP_160", "HARD_TP_210", "HARD_SL_75", "HARD_SL_55"]:
                if rn in p.executed_rules:
                    d = rn.replace("HARD_TP", "TP").replace("HARD_SL", "SL").replace("_", "")
                    status = f"已触{d}"
                    break

            h.add_row(p.symbol[:8] or _short_mint(m),
                      f"${p.entry_size_usd:.2f}", f"${cv:.2f}",
                      f"[{pcs}]{pc:+.1f}%[/]",
                      f"{p.remaining_pct*100:.0f}%", status)
        t.add_row(h)

    t.add_row("", "")
    t.add_row("[bold]最近记录[/]", "", end_section=True)
    lt = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    lt.add_column("时间", width=8)
    lt.add_column("", width=4)
    lt.add_column("代币", width=8)
    lt.add_column("内容", width=56)

    tag_icon = {"fail": "❌", "system": "⚠️"}
    for i in range(len(state.logs)):
        log = state.logs[i]
        icon = tag_icon.get(log.tag, "▪")
        lt.add_row(log.time_str, icon, log.symbol[:7], log.message[:54])
    t.add_row(lt)

    t.add_row("", "")
    t.add_row("[bold]交易记录[/]", "", end_section=True)
    tt = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    tt.add_column("时间", width=8)
    tt.add_column("", width=4)
    tt.add_column("代币", width=8)
    tt.add_column("内容", width=64)
    trade_icon = {"buy": "🟢", "sell": "🔶", "close": "🔴"}
    if not state.trade_logs:
        tt.add_row("-", "", "", "暂无交易")
    else:
        for log in state.trade_logs:
            tt.add_row(log.time_str, trade_icon.get(log.tag, "▪"), log.symbol[:7], log.message[:62])
    t.add_row(tt)

    t.add_row("", "")
    t.add_row("[bold]自选模拟买入[/]", "输入编号并回车买入", end_section=True)
    ct = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    ct.add_column("编号", justify="right", width=4)
    ct.add_column("代币", style="bold cyan", width=10)
    ct.add_column("地址", width=13)
    ct.add_column("流动性", justify="right", width=10)
    ct.add_column("市值", justify="right", width=10)
    ct.add_column("状态", width=8)
    ct.add_column("最近失败原因", width=36)

    if not state.latest_failed_candidates:
        ct.add_row("-", "(无)", "", "", "", "", "等待下一轮失败记录")
    else:
        for idx, cand in enumerate(state.latest_failed_candidates, start=1):
            tok = cand.token
            liq = _to_float(tok.get("liquidity_usd"))
            mc = _to_float(tok.get("market_cap"))
            status = "已持仓" if state.has_position(cand.token_mint) else "可买"
            ct.add_row(
                str(idx), cand.symbol[:9], _short_mint(cand.token_mint),
                f"${liq:.0f}" if liq is not None else "?",
                f"${mc:.0f}" if mc is not None else "?",
                status, cand.reason[:34],
            )
    t.add_row(ct)
    t.add_row("", "")
    t.add_row("[bold yellow]请输入手动交易编号，0表示全部清仓并结束系统运行：[/]", "")
    return t

async def buy_manual_candidate(index: int) -> None:
    if index < 1 or index > len(state.latest_failed_candidates):
        state.add_log("system", "手动", f"编号无效: {index}")
        return

    cand = state.latest_failed_candidates[index - 1]
    if state.has_position(cand.token_mint):
        state.add_log("system", cand.symbol, "已有持仓,无需重复买入")
        return

    try:
        price_info = await _retry_call("manual_latest_price", lambda: gmgn.fetch_latest_price(cand.token_mint))
    except Exception as e:
        state.add_log("fail", cand.symbol, f"手动买入失败: 价格获取异常(重试2次后): {e}")
        return

    if not price_info:
        state.add_log("fail", cand.symbol, "手动买入失败: 价格获取失败")
        return

    try:
        ok = await open_sim_position(
            cand.token, price_info, None,
            manual=True,
            skipped_rules=cand.skipped_rules,
        )
        if ok:
            skipped = ",".join(sorted(cand.skipped_rules)) or "无"
            state.add_log("system", cand.symbol, f"手动仓位跳过入场失败项: {skipped[:36]}")
    except Exception as e:
        state.add_log("fail", cand.symbol, f"手动买入异常(重试2次后): {e}")

async def close_all_and_shutdown() -> None:
    state.add_log("system", "系统", "收到0: 全部清仓并结束系统运行")
    for pos in list(state.positions.values()):
        if pos.remaining_token_amount > 0:
            ok = await sell_position(pos, 1.0, "MANUAL_EXIT", "手动全部清仓", {})
            if not ok and pos.remaining_token_amount > 0:
                fallback_price = pos.last_tx_price or pos.entry_price_usd
                forced_value = pos.remaining_token_amount * fallback_price
                pnl = forced_value - pos.cost_basis_remaining_usd
                pos.realized_pnl += pnl
                state.total_realized_pnl += pnl
                pos.remaining_token_amount = 0
                pos.executed_rules.add("MANUAL_EXIT")
                state.closed_trades.append({
                    "symbol": pos.symbol, "mint": pos.token_mint,
                    "entry_usd": pos.entry_size_usd, "total_pnl": pos.realized_pnl,
                    "close_time": datetime.now().isoformat(),
                })
                state.add_log("close", pos.symbol, f"强制纸面清仓 手动全部清仓  本轮盈亏:${pnl:+.2f}")
    state.shutdown_requested = True

async def manual_input_loop() -> None:
    while not state.shutdown_requested:
        try:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                await asyncio.sleep(1)
                continue
            text = line.strip()
            if not text:
                continue
            try:
                index = int(text)
            except ValueError:
                state.add_log("system", "手动", f"请输入数字编号: {text[:12]}")
                continue
            if index == 0:
                await close_all_and_shutdown()
                break
            await buy_manual_candidate(index)
        except Exception as e:
            state.add_log("system", "手动", f"输入循环异常: {e}")
            await asyncio.sleep(1)

# ═══════════════════════════════════════════════════════════
#  循环
# ═══════════════════════════════════════════════════════════

async def screening_loop():
    while not state.shutdown_requested:
        try:
            await run_screening()
        except Exception as e:
            state.add_log("system", "筛选", f"异常: {e}")
        await asyncio.sleep(SCREENING_INTERVAL)

async def price_monitor_loop():
    while not state.shutdown_requested:
        try:
            await monitor_positions()
        except Exception as e:
            state.add_log("system", "监控", f"异常: {e}")
        await asyncio.sleep(PRICE_POLL_INTERVAL)

async def main():
    from rich.console import Console
    c = Console()
    c.print("[bold cyan]启动模拟交易系统...[/]")
    c.print(f"  x={X}  轮询:{SCREENING_INTERVAL}s  监控:{PRICE_POLL_INTERVAL}s")
    c.print(f"  硬止损:{HARD_SL_75_MULTIPLE}x/{HARD_SL_55_MULTIPLE}x")
    c.print()

    tasks = [
        asyncio.create_task(screening_loop()),
        asyncio.create_task(price_monitor_loop()),
        asyncio.create_task(manual_input_loop()),
    ]

    with Live(render_dashboard(), refresh_per_second=2, screen=True) as live:
        while not state.shutdown_requested:
            live.update(render_dashboard())
            await asyncio.sleep(1)
    for task in tasks:
        task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n模拟交易已停止")
        sys.exit(0)
