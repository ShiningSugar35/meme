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
from ..strategy.exit_rules import normalize_pct_change, EXIT_REASON_LABELS
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


class PositionSoftStopRunner:
    """Periodic soft-stop evaluation.

    - Dull-drop: 60s cadence, check 1h+5m price change.
    - Low-activity: 300s cadence, check swaps_1h + 1h price change.
    """

    def __init__(self, repo: Repositories, gmgn, trading_pipeline=None):
        self.repo = repo
        self.gmgn = gmgn
        self.trading_pipeline = trading_pipeline
        self.exit_service = PositionExitService(repo, trading_pipeline=trading_pipeline)
        # In-memory last-check times for activity cadence
        self._last_activity_check: Dict[int, datetime] = {}

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

        # ---- Dull-drop evaluation (every 60s) ----
        if await self._check_dull_drop(position, latest, current_price, entry_price, now):
            return

        # ---- Low-activity evaluation (every 300s) ----
        last_activity = self._last_activity_check.get(pos_id)
        if last_activity is None or (now - last_activity).total_seconds() >= 300:
            self._last_activity_check[pos_id] = now
            if await self._check_low_activity(position, latest, current_price, entry_price, now):
                return

    # ------------------------------------------------------------------
    # Dull-drop: 1h change < 1% AND 5m change < 1%
    # ------------------------------------------------------------------
    async def _check_dull_drop(
        self, position, latest, current_price, entry_price, now
    ) -> bool:
        token = position["token_mint"]
        pos_id = int(position["id"])

        pct_1h = self._get_price_change(latest, ["price_change_percent1h", "price_change_1h", "price_change_percent_1h"])
        pct_5m_raw = _to_float(latest.get("price_change_percent5m") or latest.get("price_change_5m") or latest.get("change_5m"))
        if pct_5m_raw is None:
            price_5m = _to_float(latest.get("price_5m") or latest.get("price5m"))
            if price_5m and price_5m > 0:
                pct_5m_raw = (current_price / price_5m - 1.0) * 100.0
        pct_5m = normalize_pct_change(pct_5m_raw)
        pct_1h = normalize_pct_change(pct_1h)

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
        self, position, latest, current_price, entry_price, now
    ) -> bool:
        token = position["token_mint"]
        pos_id = int(position["id"])

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

        pct_1h = self._get_price_change(latest, ["price_change_percent1h", "price_change_1h", "price_change_percent_1h"])
        pct_1h = normalize_pct_change(pct_1h)

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
