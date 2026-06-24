"""Soft stop runner: dull-drop and low-activity exits.

DULL_DROP_SL: every 60s, exits if 1h AND 5m price change < 1%.
LOW_ACTIVITY_SL: every 300s, exits if swaps_1h < 7 AND 1h price change < 5%.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

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


_PCT_1H_ALIASES = [
    "price_change_percent1h", "price_change_1h", "price_change_percent_1h",
    "price_change1h", "change_1h", "price_change_1h_percent",
]
_PCT_5M_ALIASES = [
    "price_change_percent5m", "price_change_5m", "price_change_percent_5m",
    "price_change5m", "change_5m", "price_change_5m_percent",
]


class PositionSoftStopRunner:
    """Periodic soft-stop evaluation.

    - Dull-drop: 60s cadence, check 1h+5m price change.
    - Low-activity: 300s cadence, check swaps_1h + 1h price change.
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

        # Fetch latest price
        slot = acquire_holding_slot("soft_stop")
        try:
            latest = await self.gmgn.fetch_latest_price(token, credential_slot=slot)
        except Exception:
            return
        current_price = _to_float(latest.get("price_usd") or latest.get("latest_price_usd") or latest.get("price"))
        if current_price is None or current_price <= 0:
            return

        # ---- Dull-drop evaluation (every cycle = 60s) ----
        if await self._check_dull_drop(position, latest, current_price, now):
            # Write last_soft_stop_check_at
            await self._write_soft_stop_check(pos_id, now)
            return

        # ---- Low-activity evaluation (every 300s via DB field) ----
        last_activity_str = position.get("last_activity_stop_check_at")
        due = True
        if last_activity_str:
            try:
                last_activity_dt = datetime.fromisoformat(str(last_activity_str).replace("Z", "+00:00"))
                if last_activity_dt.tzinfo is None:
                    last_activity_dt = last_activity_dt.replace(tzinfo=timezone.utc)
                if (now - last_activity_dt).total_seconds() < 300:
                    due = False
            except Exception:
                pass
        if due:
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
    # Dull-drop: 1h change < 1% AND 5m change < 1%
    # ------------------------------------------------------------------
    async def _check_dull_drop(
        self, position, latest, current_price, now
    ) -> bool:
        token = position["token_mint"]
        pos_id = int(position["id"])
        account_type = _account_type(position)

        pct_1h = await self._get_price_change_with_fallback(token, latest, current_price, account_type, 3600, _PCT_1H_ALIASES)
        pct_5m = await self._get_price_change_with_fallback(token, latest, current_price, account_type, 300, _PCT_5M_ALIASES)

        if pct_1h is None or pct_5m is None:
            return False

        if pct_1h < 0.01 and pct_5m < 0.01:
            logger.info(
                "DULL_DROP_SL triggered",
                token=token,
                position_id=pos_id,
                pct_1h=pct_1h,
                pct_5m=pct_5m,
            )
            await self.exit_service.exit_position(
                position=position,
                exit_pct=1.0,
                reason_code="DULL_DROP_SL",
                current_price_usd=current_price,
                source="SOFT_STOP",
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Low-activity: swaps_1h < 7 AND 1h change < 5%
    # ------------------------------------------------------------------
    async def _check_low_activity(
        self, position, latest, current_price, now
    ) -> bool:
        token = position["token_mint"]
        pos_id = int(position["id"])
        account_type = _account_type(position)

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

        if swaps_1h < 7.0 and pct_1h < 0.05:
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
        """Try latest price-change fields, then tick_snapshots fallback, then WARN."""
        pos_id = None  # only for logging

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

        # C. Tick snapshots fallback — find tick at approximately window start
        #    (e.g. ~3600s ago for 1h window, ~300s ago for 5m window).
        #    Cannot use `get_recent_ticks` because a 2-min-old position would
        #    return only 2 min of data, misrepresenting "1h change".
        try:
            tolerance = min(120, max(30, window_seconds // 30))
            ref_tick = await self.repo.get_reference_tick_before(token, window_seconds, tolerance_seconds=tolerance)
            if ref_tick:
                ref_price = _to_float(ref_tick.get("price_usd") or ref_tick.get("price_sol"))
                if ref_price and ref_price > 0:
                    return (current_price / ref_price) - 1.0
        except Exception:
            pass

        # D. Missing — write WARN system event
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
