"""Soft stop runner: dull-drop and low-activity exits.

DULL_DROP_SL: every 30min when holding >= 4h, exits if 4h price growth < 4%.
LOW_ACTIVITY_SL: every 300s, exits if swaps_1h < 12 AND 1h growth < 5%.
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from ..config import settings
from ..db.repositories import Repositories
from ..logging_config import logger
from ..strategy.exit_rules import normalize_gmgn_percent_change, EXIT_REASON_LABELS
from ..services.position_exit_service import PositionExitService
from .discovery_runner import acquire_holding_slot


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _account_type(position: Dict[str, Any]) -> str:
    return position.get("account_type") or ("LIVE" if position.get("is_live") else "SIM")


def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(obj), ensure_ascii=False)


def _position_age_hours(position: Dict[str, Any], now: datetime) -> Optional[float]:
    opened = position.get("opened_at")
    if not opened:
        return None
    try:
        dt = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() / 3600.0
    except Exception:
        return None


def _due_by_field(position: Dict[str, Any], field: str, interval_seconds: int, now: datetime) -> bool:
    raw = position.get(field)
    if not raw:
        return True
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() >= interval_seconds
    except Exception:
        return True


_PCT_1H_ALIASES = [
    "price_change_percent1h", "price_change_1h", "price_change_percent_1h",
    "price_change1h", "change_1h", "price_change_1h_percent",
]
_PCT_5M_ALIASES = [
    "price_change_percent5m", "price_change_5m", "price_change_percent_5m",
    "price_change5m", "change_5m", "price_change_5m_percent",
]

DULL_DROP_COOLDOWN_SECONDS = 1800     # 30 min
DULL_DROP_MIN_AGE_HOURS = 4.0
LOW_ACT_COOLDOWN_SECONDS = 300         # 5 min
LOW_ACT_MIN_AGE_HOURS = 1.0             # start from 1st hour


class PositionSoftStopRunner:
    """Periodic soft-stop evaluation.

    - Dull-drop: 30min cadence (from 4th hour), 4h kline growth < 4%.
    - Low-activity: 5min cadence, swaps_1h < 12 + 1h growth < 5%.
    """

    def __init__(self, repo: Repositories, gmgn, trading_pipeline=None):
        self.repo = repo
        self.gmgn = gmgn
        self.trading_pipeline = trading_pipeline
        self.exit_service = PositionExitService(repo, trading_pipeline=trading_pipeline, gmgn=gmgn)

    def set_trading_pipeline(self, trading_pipeline):
        self.trading_pipeline = trading_pipeline
        self.exit_service.trading_pipeline = trading_pipeline

    async def run_once(self):
        now = _utc_now()
        positions = await self.repo.list_open_positions()

        for pos in positions:
            try:
                await self._process_position(pos, now)
            except Exception:
                logger.exception("SoftStopRunner failed", token=pos.get("token_mint"))

    async def _process_position(self, position: Dict[str, Any], now: datetime):
        token = position["token_mint"]
        pos_id = int(position["id"])
        account_type = _account_type(position)

        entry_price = _to_float(position.get("entry_price_usd"))
        if entry_price is None or entry_price <= 0:
            return

        slot = acquire_holding_slot("soft_stop")
        try:
            latest = await self.gmgn.fetch_latest_price(token, credential_slot=slot)
        except Exception:
            return
        current_price = _to_float(latest.get("price_usd") or latest.get("latest_price_usd") or latest.get("price"))
        if current_price is None or current_price <= 0:
            return

        # ---- Dull-drop (30min cooldown, min 4h age) ----
        age_h = _position_age_hours(position, now)
        dull_due = age_h is not None and age_h >= DULL_DROP_MIN_AGE_HOURS and _due_by_field(
            position, "last_soft_stop_check_at", DULL_DROP_COOLDOWN_SECONDS, now,
        )
        if dull_due:
            if await self._check_dull_drop(position, current_price, now):
                await self._write_soft_stop_check(pos_id, now)
                return

        # ---- Low-activity (300s cooldown, min 1h age) ----
        act_due = age_h is not None and age_h >= LOW_ACT_MIN_AGE_HOURS and _due_by_field(
            position, "last_activity_stop_check_at", LOW_ACT_COOLDOWN_SECONDS, now,
        )
        if act_due:
            triggered = await self._check_low_activity(position, latest, current_price, now)
            await self._write_activity_check(pos_id, now)
            if triggered:
                return

    async def _write_soft_stop_check(self, pos_id: int, now: datetime):
        if hasattr(self.repo, "update_position_soft_stop_schedule"):
            await self.repo.update_position_soft_stop_schedule(
                position_id=pos_id,
                last_soft_stop_check_at=_iso(now),
            )

    async def _write_activity_check(self, pos_id: int, now: datetime):
        if hasattr(self.repo, "update_position_soft_stop_schedule"):
            await self.repo.update_position_soft_stop_schedule(
                position_id=pos_id,
                last_activity_stop_check_at=_iso(now),
            )

    # ------------------------------------------------------------------
    # Retry helper — 2 quick retries on same slot, then rotate slots
    # ------------------------------------------------------------------
    async def _fetch_4h_kline_with_retry(self, token: str) -> Optional[float]:
        """Fetch 4h kline open price with retry.

        Returns the open price of the first (oldest) 4h kline,
        or None if all retries fail.
        """
        first_retries = 2

        for attempt in range(first_retries):
            slot = acquire_holding_slot("soft_stop")
            try:
                klines = await self.gmgn.fetch_kline(token, interval="4h", limit=1, credential_slot=slot)
                if klines and klines[0].get("open") and float(klines[0]["open"]) > 0:
                    return float(klines[0]["open"])
            except Exception:
                pass
            await asyncio.sleep(5)

        # Rotate through all holding slots, 1 minute between attempts
        holding_slots = list(settings.get_holding_slots())
        if not holding_slots:
            holding_slots = [0]
        while True:
            for slot in holding_slots:
                try:
                    klines = await self.gmgn.fetch_kline(token, interval="4h", limit=1, credential_slot=slot)
                    if klines and klines[0].get("open") and float(klines[0]["open"]) > 0:
                        return float(klines[0]["open"])
                except Exception:
                    pass
                await asyncio.sleep(60)
            # If all slots fail, keep looping (the cycle check will still allow
            # the next normal run to attempt a fresh eval)

    async def _fetch_latest_price_with_retry(self, token: str) -> Optional[Dict[str, Any]]:
        """Fetch latest price with retry, same pattern."""
        first_retries = 2
        for attempt in range(first_retries):
            slot = acquire_holding_slot("soft_stop")
            try:
                data = await self.gmgn.fetch_latest_price(token, credential_slot=slot)
                price = _to_float(data.get("price_usd") or data.get("price"))
                if price and price > 0:
                    return data
            except Exception:
                pass
            await asyncio.sleep(5)

        holding_slots = list(settings.get_holding_slots())
        if not holding_slots:
            holding_slots = [0]
        while True:
            for slot in holding_slots:
                try:
                    data = await self.gmgn.fetch_latest_price(token, credential_slot=slot)
                    price = _to_float(data.get("price_usd") or data.get("price"))
                    if price and price > 0:
                        return data
                except Exception:
                    pass
                await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # Dull-drop: 4h kline growth < 4% (from 4th holding hour)
    # ------------------------------------------------------------------
    async def _check_dull_drop(
        self, position, current_price, now
    ) -> bool:
        token = position["token_mint"]
        pos_id = int(position["id"])
        account_type = _account_type(position)

        # 1. Fetch current price with retry
        latest = await self._fetch_latest_price_with_retry(token)
        if latest is None:
            return False
        price_now = _to_float(latest.get("price_usd") or latest.get("latest_price_usd") or latest.get("price"))
        if price_now is None or price_now <= 0:
            return False

        # 2. Fetch 4h kline open price with retry
        kline_open = await self._fetch_4h_kline_with_retry(token)
        if kline_open is None or kline_open <= 0:
            logger.warning("DULL_DROP_SL kline unavailable after retry", token=token, position_id=pos_id)
            return False

        # Compute 4h growth as decimal (e.g., 0.04 = 4%)
        growth_4h = (price_now / kline_open) - 1.0

        if growth_4h < 0.04:
            logger.info(
                "DULL_DROP_SL triggered",
                token=token,
                position_id=pos_id,
                growth_4h=growth_4h,
                threshold=0.04,
            )
            await self.exit_service.exit_position(
                position=position,
                exit_pct=1.0,
                reason_code="DULL_DROP_SL",
                current_price_usd=price_now,
                source="SOFT_STOP",
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Low-activity: swaps_1h < 12 AND 1h growth < 5%
    # ------------------------------------------------------------------
    async def _check_low_activity(
        self, position, latest, current_price, now
    ) -> bool:
        token = position["token_mint"]
        pos_id = int(position["id"])
        account_type = _account_type(position)

        # Retry-fetch latest price if needed
        if latest is None or _to_float(latest.get("price_usd") or latest.get("price")) is None:
            latest = await self._fetch_latest_price_with_retry(token)
            if latest is None:
                return False
            current_price = _to_float(latest.get("price_usd") or latest.get("latest_price_usd") or latest.get("price"))
            if current_price is None or current_price <= 0:
                return False

        swaps_1h = _to_float(
            latest.get("swaps_1h") or latest.get("swaps1h") or latest.get("swaps_60m")
            or latest.get("txns_1h") or latest.get("transactions_1h")
        )
        if swaps_1h is None:
            buys_1h = _to_float(latest.get("buys_1h"))
            sells_1h = _to_float(latest.get("sells_1h"))
            if buys_1h is not None and sells_1h is not None:
                swaps_1h = buys_1h + sells_1h
        if swaps_1h is None:
            return False

        pct_1h = await self._get_price_change_with_fallback(token, latest, current_price, account_type, 3600, _PCT_1H_ALIASES)
        if pct_1h is None:
            return False

        if swaps_1h < 12.0 and pct_1h < 0.05:
            logger.info(
                "LOW_ACTIVITY_SL triggered",
                token=token,
                position_id=pos_id,
                swaps_1h=swaps_1h,
                pct_1h=pct_1h,
            )
            await self.exit_service.exit_position(
                position=position,
                exit_pct=1.0,
                reason_code="LOW_ACTIVITY_SL",
                current_price_usd=current_price,
                source="SOFT_STOP",
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _get_price_change(latest: Dict[str, Any], keys: List[str]) -> Optional[float]:
        for k in keys:
            v = _to_float(latest.get(k))
            if v is not None:
                return v
        for nest_key in ("price", "pool", "token", "info"):
            nested = latest.get(nest_key)
            if isinstance(nested, dict):
                for k in keys:
                    v = _to_float(nested.get(k))
                    if v is not None:
                        return v
        return None

    async def _get_price_change_with_fallback(
        self,
        token: str,
        latest: Dict[str, Any],
        current_price: float,
        account_type: str,
        window_seconds: int,
        aliases: List[str],
    ) -> Optional[float]:
        pos_id = None

        # A. Direct API fields
        pct = self._get_price_change(latest, aliases)
        if pct is not None:
            return normalize_gmgn_percent_change(pct)

        # B. Compute from price_5m / price_1h in latest
        if window_seconds == 300:
            price_ref = _to_float(latest.get("price_5m") or latest.get("price5m"))
        elif window_seconds == 3600:
            price_ref = _to_float(latest.get("price_1h") or latest.get("price1h") or latest.get("price_h1"))
        else:
            price_ref = None
        if price_ref and price_ref > 0:
            return (current_price / price_ref) - 1.0

        # C. Tick snapshots fallback
        try:
            tolerance = min(120, max(30, window_seconds // 30))
            ref_tick = await self.repo.get_reference_tick_before(token, window_seconds, tolerance_seconds=tolerance)
            if ref_tick:
                ref_price = _to_float(ref_tick.get("price_usd") or ref_tick.get("price_sol"))
                if ref_price and ref_price > 0:
                    return (current_price / ref_price) - 1.0
        except Exception:
            pass

        # D. Missing
        logger.warning(
            "Soft stop price change unavailable",
            token=token,
            window_seconds=window_seconds,
            wildcard="no_field_or_fallback",
        )
        try:
            await self.repo.append_system_event(
                "WARN", "SOFT_STOP",
                f"Price change data unavailable for {token} ({window_seconds}s window); no soft-stop triggered",
                _safe_json({"token": token, "window_seconds": window_seconds, "action": "SKIP_SOFT_STOP"}),
                account_type=account_type,
            )
        except Exception:
            pass
        return None


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
