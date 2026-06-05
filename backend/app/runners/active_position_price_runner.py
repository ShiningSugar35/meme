"""Second-level price monitoring runner for open positions.

This runner polls all open positions every ACTIVE_POSITION_PRICE_POLL_SECONDS
(1 second by default) and evaluates hard TP/SL plus completed exits
independently of the slower risk-scan cycle.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from ..config import settings
from ..db.repositories import Repositories
from ..logging_config import logger
from ..services.event_bus import event_bus
from .discovery_runner import acquire_feature_slot


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


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


def _executed_exit_rules(position: Dict[str, Any]) -> Set[str]:
    raw = position.get("executed_exit_rules_json") or "[]"
    if isinstance(raw, list):
        return {str(x) for x in raw}
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return {str(x) for x in data}
    except Exception:
        pass
    return set()


def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(obj), ensure_ascii=False)


class ActivePositionPriceRunner:
    """Dedicated price-only runner for open positions.

    Polls every ACTIVE_POSITION_PRICE_POLL_SECONDS.  No risk recheck, no
    smart-degen fetch — only price-based exit rules.
    """

    def __init__(self, repo: Repositories, gmgn, trading_pipeline=None):
        self.repo = repo
        self.gmgn = gmgn
        self.trading_pipeline = trading_pipeline
        self._last_price_update: Dict[int, Dict[str, Any]] = {}

    def set_trading_pipeline(self, trading_pipeline):
        self.trading_pipeline = trading_pipeline

    async def run_once(self):
        now = _utc_now()

        positions = await self.repo.list_open_positions()

        for position in positions:
            try:
                await self._process_position(position, now)
            except Exception as e:
                logger.exception("ActivePositionPriceRunner failed", token=position.get("token_mint"), error=str(e))

    async def _process_position(self, position: Dict[str, Any], now: datetime):
        token = position["token_mint"]
        account_type = _account_type(position)

        # Fetch latest price
        latest = await self._fetch_latest_price(token, account_type)
        current_price = _to_float(
            latest.get("price_usd") or latest.get("latest_price_usd") or latest.get("price")
        )
        if current_price is None or current_price <= 0:
            return

        remaining_token = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0
        remaining_value_usd = remaining_token * current_price

        if remaining_token <= 0:
            return

        entry_price = _to_float(position.get("entry_price_usd"))
        if entry_price is None or entry_price <= 0:
            return

        executed_rules = _executed_exit_rules(position)
        multiple = current_price / entry_price

        # ---- Evaluate price rules ----
        reasons: list[tuple[str, float]] = []
        multiple = current_price / entry_price

        # HARD_TP_250: full exit
        if multiple >= 2.50 and "HARD_TP_250" not in executed_rules:
            reasons.append(("HARD_TP_250", 1.0))

        # HARD_TP_210: 50% exit
        if multiple >= 2.10 and "HARD_TP_210" not in executed_rules and "HARD_TP_250" not in executed_rules:
            reasons.append(("HARD_TP_210", 0.5))

        # HARD_TP_160: 50% exit
        if multiple >= 1.60 and "HARD_TP_160" not in executed_rules and "HARD_TP_250" not in executed_rules:
            reasons.append(("HARD_TP_160", 0.5))

        # HARD_SL_55: full exit
        if multiple <= 0.55 and "HARD_SL_55" not in executed_rules:
            reasons.append(("HARD_SL_55", 1.0))

        # HARD_SL_75: 50% exit
        if multiple <= 0.75 and "HARD_SL_75" not in executed_rules and "HARD_SL_55" not in executed_rules:
            reasons.append(("HARD_SL_75", 0.5))

        # Completed type — also try fetching latest token snapshot for up-to-date type
        token_type = position.get("latest_token_type") or position.get("type")
        if token_type != "completed":
            try:
                snap = await self.gmgn.fetch_token_snapshot(token)
                if snap:
                    snap_type = snap.get("type") or snap.get("token_type")
                    if snap_type:
                        token_type = snap_type
            except Exception:
                pass
        if token_type == "completed":
            reasons.append(("COMPLETED", 1.0))

        if not reasons:
            return

        # Pick the highest-priority reason
        full_reasons = [r for r in reasons if r[1] >= 1.0]
        if full_reasons:
            reason_code, exit_pct = full_reasons[0]
        else:
            reason_code, exit_pct = max(reasons, key=lambda r: r[1])

        await self._execute_exit(position, exit_pct, reason_code, current_price, now)

    async def _fetch_latest_price(self, token: str, account_type: str) -> Dict[str, Any]:
        slot = acquire_feature_slot("price_runner")
        try:
            return await self.gmgn.fetch_latest_price(token, credential_slot=slot)
        except Exception as e:
            return {}

    async def _execute_exit(
        self,
        position: Dict[str, Any],
        exit_pct: float,
        reason_code: str,
        current_price_usd: float,
        now: datetime,
    ):
        pos_id = int(position["id"])
        token = position["token_mint"]
        account_type = _account_type(position)
        is_live = bool(position.get("is_live"))

        remaining_token = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0
        sell_token_amount = remaining_token * min(max(exit_pct, 0.0), 1.0)
        new_remaining = max(0.0, remaining_token - sell_token_amount)
        executed_usd = sell_token_amount * current_price_usd

        if is_live and self.trading_pipeline is not None and hasattr(self.trading_pipeline, "execute_sell"):
            try:
                result = await self.trading_pipeline.execute_sell(
                    position=position,
                    exit_pct=exit_pct,
                    exit_reason=reason_code,
                )
                if result and (result.get("ok") is True or result.get("success") is True):
                    await self.repo.append_system_event(
                        "INFO", "PRICE",
                        f"Price runner live exit {reason_code} for {token}",
                        _safe_json({"position_id": pos_id, "exit_pct": exit_pct, "reason": reason_code}),
                        account_type=account_type,
                    )
                    if exit_pct < 1.0 and hasattr(self.repo, "update_position_remaining"):
                        await self.repo.update_position_remaining(
                            pos_id,
                            new_remaining,
                            new_remaining * current_price_usd,
                            last_fill_at=_iso(now),
                            last_fill_price_usd=current_price_usd,
                        )
                    if hasattr(self.repo, "mark_exit_rule_executed"):
                        await self.repo.mark_exit_rule_executed(pos_id, reason_code)
                return
            except Exception as e:
                logger.warning("Price runner live exit failed", error=str(e), token=token)

        if is_live:
            await self.repo.append_system_event(
                "WARN", "PRICE",
                f"Price runner LIVE exit skipped (no pipeline): {reason_code}",
                _safe_json({"position_id": pos_id, "token": token}),
                account_type=account_type,
            )
            return

        # SIM paper exit
        await self.repo.append_trade_event(
            f"SELL_PRICE:{pos_id}:{reason_code}",
            position_id=pos_id,
            token_mint=token,
            strategy_id=position.get("live_strategy_id"),
            is_live=0,
            account_type=account_type,
            side="SELL",
            event_type="SIM_SELL",
            status="CONFIRMED",
            requested_pct=exit_pct,
            executed_token_amount=sell_token_amount,
            price_usd=current_price_usd,
            exit_reason=reason_code,
            provider="PRICE_RUNNER",
        )

        if exit_pct >= 1.0 or new_remaining <= 0:
            await self.repo.close_position(pos_id, close_reason=reason_code)
        else:
            await self.repo.update_position_remaining(
                pos_id,
                new_remaining,
                new_remaining * current_price_usd,
                last_fill_at=_iso(now),
                last_fill_price_usd=current_price_usd,
            )

        if hasattr(self.repo, "mark_exit_rule_executed"):
            await self.repo.mark_exit_rule_executed(pos_id, reason_code)

        await event_bus.publish("system", {
            "level": "INFO",
            "category": "PRICE",
            "message": f"Price runner SIM exit {reason_code} for {token}",
        })
