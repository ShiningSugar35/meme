"""Second-level price monitoring runner for open positions.

This runner polls all open positions every ACTIVE_POSITION_PRICE_POLL_SECONDS
(1 second by default) and evaluates hard TP/SL plus completed exits
independently of the slower risk-scan cycle.

Each position makes at most ONE price API request per polling cycle.
On failure the position stays open and is retried next cycle — no
EXIT_PENDING, no intra-cycle retry burst.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from ..db.repositories import Repositories
from ..logging_config import logger
from ..services.event_bus import event_bus
from .discovery_runner import acquire_holding_slot

_PRICE_FAILURE_COOLDOWN_SECONDS = 60


def _position_strategy_id(position: Dict[str, Any]) -> Optional[int]:
    if position.get("live_strategy_id"):
        return int(position["live_strategy_id"])
    locked = position.get("locked_strategy_config_json")
    if locked:
        try:
            cfg = json.loads(locked)
            return int(cfg.get("id") or cfg.get("strategy_id") or 0) or None
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return None


EXIT_REASON_LABELS: Dict[str, str] = {
    "HARD_TP_150": "硬止盈：价格超过 1.5x，撤仓50%",
    "HARD_TP_200": "硬止盈：价格超过 2.0x，全部撤仓",
    "HARD_TP_150_RETRACE": "硬止盈回撤：已超过1.5x后回落至1.5x及以下，全部撤仓",
    "HARD_SL_75": "硬止损：价格低于 0.75x，全部撤仓",
    "COMPLETED": "池子 type 变为 completed，全部撤仓",
}


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

    @staticmethod
    def _failure_key(position: Dict[str, Any]) -> str:
        return f"{position.get('id') or 0}:{_account_type(position)}:{position.get('token_mint', '?')}"

    def __init__(self, repo: Repositories, gmgn, trading_pipeline=None):
        self.repo = repo
        self.gmgn = gmgn
        self.trading_pipeline = trading_pipeline
        self._last_price_update: Dict[int, Dict[str, Any]] = {}
        self._consecutive_price_failures: Dict[str, int] = {}
        self._last_failure_event_at: Dict[str, float] = {}
        self._failure_start_time: Dict[str, float] = {}

    def set_trading_pipeline(self, trading_pipeline):
        self.trading_pipeline = trading_pipeline

    async def run_once(self):
        now = _utc_now()
        positions = await self.repo.list_open_positions()

        sem = asyncio.Semaphore(5)

        async def _process_wrapper(pos):
            async with sem:
                try:
                    await self._process_position(pos, now)
                except Exception as e:
                    logger.exception("ActivePositionPriceRunner failed", token=pos.get("token_mint"), error=str(e))

        tasks = [_process_wrapper(p) for p in positions]
        if tasks:
            await asyncio.gather(*tasks)

    async def _process_position(self, position: Dict[str, Any], now: datetime):
        token = position["token_mint"]
        account_type = _account_type(position)

        # Fetch latest price — one attempt per cycle
        latest = await self._fetch_latest_price(token, account_type, position)
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

        # Refresh position value and PnL% in DB
        pnl_pct = (current_price / entry_price) - 1.0
        try:
            await self.repo.update_position_remaining(
                int(position["id"]),
                remaining_token,
                remaining_value_usd,
                pnl_pct=pnl_pct,
            )
        except Exception as e:
            logger.warning(
                "Failed to refresh position remaining value",
                token=token,
                position_id=position.get("id"),
                error=str(e),
            )

        executed_rules = _executed_exit_rules(position)
        multiple = current_price / entry_price

        # ---- Evaluate price rules ----
        reasons: list[tuple[str, float]] = []

        # Completed type — also try fetching latest token snapshot for up-to-date type
        token_type = position.get("latest_token_type") or position.get("type")
        if token_type != "completed":
            try:
                slot = acquire_holding_slot("price_runner_snapshot")
                snap = await self.gmgn.fetch_token_snapshot(token, credential_slot=slot)
                if snap:
                    snap_type = snap.get("type") or snap.get("token_type")
                    if snap_type:
                        token_type = snap_type
            except Exception:
                pass
        if token_type == "completed":
            reasons.append(("COMPLETED", 1.0))

        # >2.0x full exit
        if multiple > 2.0 and "HARD_TP_200" not in executed_rules:
            reasons.append(("HARD_TP_200", 1.0))

        # <0.75x full exit
        if multiple < 0.75 and "HARD_SL_75" not in executed_rules:
            reasons.append(("HARD_SL_75", 1.0))

        # Already executed 1.5x half-exit, now retraced to <=1.5x → full exit
        if (
            "HARD_TP_150" in executed_rules
            and multiple <= 1.5
            and "HARD_TP_150_RETRACE" not in executed_rules
        ):
            reasons.append(("HARD_TP_150_RETRACE", 1.0))

        # First time >1.5x → 50% exit
        if multiple > 1.5 and "HARD_TP_150" not in executed_rules:
            reasons.append(("HARD_TP_150", 0.5))

        if not reasons:
            return

        # Pick the highest-priority reason (COMPLETED > HARD_TP_200 > HARD_SL_75 > RETRACE > HARD_TP_150)
        full_reasons = [r for r in reasons if r[1] >= 1.0]
        if full_reasons:
            reason_code, exit_pct = full_reasons[0]
        else:
            reason_code, exit_pct = max(reasons, key=lambda r: r[1])

        await self._execute_exit(position, exit_pct, reason_code, current_price, now)

    async def _fetch_latest_price(self, token: str, account_type: str, position: Dict[str, Any]) -> Dict[str, Any]:
        """Single-shot price fetch — one attempt per cycle, no retry loop."""
        key = self._failure_key(position)
        slot = acquire_holding_slot("price_runner")
        try:
            data = await self.gmgn.fetch_latest_price(token, credential_slot=slot)
            price = data.get("price_usd") or data.get("latest_price_usd") or data.get("price")
            if price is None or float(price) <= 0:
                raise ValueError("empty price")

            # Success — clear failure state
            was_failing = key in self._consecutive_price_failures
            fail_start = self._failure_start_time.pop(key, None)
            self._consecutive_price_failures.pop(key, None)
            self._last_failure_event_at.pop(key, None)
            if was_failing and fail_start is not None:
                downtime = int(time.time() - fail_start)
                try:
                    await self.repo.append_system_event(
                        "INFO", "PRICE",
                        f"Price fetch recovered for {token} after {downtime}s downtime",
                        _safe_json({
                            "position_id": int(position.get("id") or 0),
                            "token_mint": token,
                            "account_type": account_type,
                            "downtime_seconds": downtime,
                            "action": "PRICE_RECOVERED",
                        }),
                        account_type=account_type,
                    )
                except Exception:
                    pass
            return data

        except Exception as e:
            fails = self._consecutive_price_failures.get(key, 0) + 1
            self._consecutive_price_failures[key] = fails
            if fails == 1:
                self._failure_start_time[key] = time.time()

            logger.warning(
                "Price fetch failed; keep position open and retry next cycle",
                token=token,
                account_type=account_type,
                position_id=position.get("id"),
                failures=fails,
                error=str(e),
            )

            last_event_at = self._last_failure_event_at.get(key, 0.0)
            now = time.time()
            if fails == 1 or (now - last_event_at) >= _PRICE_FAILURE_COOLDOWN_SECONDS:
                self._last_failure_event_at[key] = now
                try:
                    await self.repo.append_system_event(
                        "WARN", "PRICE",
                        f"Price fetch failed for {token}; keep polling",
                        _safe_json({
                            "position_id": int(position.get("id") or 0),
                            "token_mint": token,
                            "account_type": account_type,
                            "consecutive_failures": fails,
                            "action": "KEEP_POLLING_NO_EXIT",
                            "error": str(e)[:300],
                        }),
                        account_type=account_type,
                    )
                except Exception:
                    pass
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
        exit_reason_label = EXIT_REASON_LABELS.get(reason_code, reason_code)
        trade_value_usd_net = sell_token_amount * current_price_usd

        await self.repo.append_trade_event(
            f"SELL_PRICE:{pos_id}:{reason_code}",
            position_id=pos_id,
            token_mint=token,
            strategy_id=_position_strategy_id(position),
            is_live=0,
            account_type=account_type,
            side="SELL",
            event_type="SIM_SELL",
            status="CONFIRMED",
            requested_pct=exit_pct,
            executed_token_amount=sell_token_amount,
            price_usd=current_price_usd,
            exit_reason=reason_code,
            exit_reason_label=exit_reason_label,
            trade_value_usd_net=trade_value_usd_net,
            fee_detail_json=json.dumps({"fallback": True}),
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
