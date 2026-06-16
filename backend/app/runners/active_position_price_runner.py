"""Second-level price monitoring runner for open positions.

This runner polls all open positions every ACTIVE_POSITION_PRICE_POLL_SECONDS
(1 second by default) and evaluates hard TP/SL plus completed exits
independently of the slower risk-scan cycle.
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from ..config import settings
from ..db.repositories import Repositories
from ..logging_config import logger
from ..providers.credential_router import get_credential_router
from ..providers.rate_limiter import get_rate_limiter
from ..services.event_bus import event_bus
from .discovery_runner import acquire_holding_slot


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
    "HARD_TP_160": "硬止盈：价格超过 1.6x，撤仓50%",
    "HARD_TP_210": "硬止盈：价格超过 2.1x，全部撤仓",
    "HARD_SL_70": "硬止损：价格低于 0.7x，撤仓50%",
    "HARD_SL_45": "硬止损：价格低于 0.45x，全部撤仓",
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

    def __init__(self, repo: Repositories, gmgn, trading_pipeline=None):
        self.repo = repo
        self.gmgn = gmgn
        self.trading_pipeline = trading_pipeline
        self._last_price_update: Dict[int, Dict[str, Any]] = {}
        self._consecutive_price_failures: Dict[str, int] = {}

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

        # Fetch latest price with retry across slots
        latest = await self._fetch_latest_price(token, account_type, position=position)
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

        # HARD_TP_210: full exit
        if multiple >= 2.10 and "HARD_TP_210" not in executed_rules:
            reasons.append(("HARD_TP_210", 1.0))

        # HARD_TP_160: 50% exit (idempotent)
        if multiple >= 1.60 and "HARD_TP_160" not in executed_rules:
            reasons.append(("HARD_TP_160", 0.5))

        # HARD_SL_45: full exit
        if multiple <= 0.45 and "HARD_SL_45" not in executed_rules:
            reasons.append(("HARD_SL_45", 1.0))

        # HARD_SL_70: 50% exit
        if multiple <= 0.70 and "HARD_SL_70" not in executed_rules and "HARD_SL_45" not in executed_rules:
            reasons.append(("HARD_SL_70", 0.5))

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

        if not reasons:
            return

        # Pick the highest-priority reason
        full_reasons = [r for r in reasons if r[1] >= 1.0]
        if full_reasons:
            reason_code, exit_pct = full_reasons[0]
        else:
            reason_code, exit_pct = max(reasons, key=lambda r: r[1])

        await self._execute_exit(position, exit_pct, reason_code, current_price, now)

    async def _fetch_price_with_retry(self, token_mint: str, preferred_slot: Optional[int]) -> Dict[str, Any]:
        rl = get_rate_limiter()
        feature_pool = settings.get_feature_slots()
        attempted: Set[int] = set()
        endpoint = getattr(settings, "GMGN_TOKEN_INFO_PATH", "/v1/token/info")

        for attempt in range(4):
            slot = None
            if attempt == 0 and preferred_slot is not None and rl.is_slot_available(preferred_slot):
                slot = preferred_slot
            elif feature_pool:
                for s in feature_pool:
                    if s not in attempted and rl.is_slot_available(s):
                        slot = s
                        break
                if slot is None:
                    for s in feature_pool:
                        if s not in attempted:
                            slot = s
                            break

            if slot is None:
                break

            attempted.add(slot)
            try:
                data = await self.gmgn.fetch_latest_price(token_mint, credential_slot=slot)
                price = data.get("price_usd") or data.get("price")
                if price is not None and float(price) > 0:
                    return data
                await rl.report_failure(slot, endpoint=endpoint, kind="empty")
            except Exception:
                pass

            if attempt < 3:
                await asyncio.sleep(settings.ACTIVE_POSITION_PRICE_POLL_SECONDS)

        raise RuntimeError(f"Price fetch failed for {token_mint}: all 4 retry attempts exhausted")

    async def _fetch_latest_price(self, token: str, account_type: str, position: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        preferred_slot = acquire_holding_slot("price_runner")
        try:
            data = await self._fetch_price_with_retry(token, preferred_slot)
            self._consecutive_price_failures.pop(token, None)
            return data
        except Exception:
            fails = self._consecutive_price_failures.get(token, 0) + 1
            self._consecutive_price_failures[token] = fails
            if position is not None:
                await self._emergency_price_exit(position)
            return {}

    async def _emergency_price_exit(self, position: Dict[str, Any]):
        token = position["token_mint"]
        pos_id = int(position["id"])
        account_type = _account_type(position)
        is_live = bool(position.get("is_live"))

        logger.warning("Emergency price exit triggered", token=token, account_type=account_type)

        if is_live and self.trading_pipeline is not None:
            try:
                await self.trading_pipeline.execute_sell(
                    position=position,
                    exit_pct=1.0,
                    exit_reason="PRICE_API_UNAVAILABLE_EXIT",
                )
                await self.repo.append_system_event(
                    "WARN", "PRICE",
                    f"Emergency LIVE exit for {token} (price API unavailable)",
                    _safe_json({"position_id": pos_id, "reason": "PRICE_API_UNAVAILABLE_EXIT"}),
                    account_type=account_type,
                )
            except Exception as e:
                logger.error("Emergency LIVE exit failed", error=str(e), token=token)
            return

        if is_live:
            await self.repo.append_system_event(
                "WARN", "PRICE",
                f"Emergency LIVE exit skipped (no pipeline): {token}",
                _safe_json({"position_id": pos_id}),
                account_type=account_type,
            )
            return

        # SIM emergency exit — set EXIT_PENDING, don't write fake 0-price SELL
        if self.trading_pipeline is not None and hasattr(self.trading_pipeline, "emergency_sim_exit"):
            try:
                await self.trading_pipeline.emergency_sim_exit(
                    position=position,
                    exit_reason="PRICE_API_UNAVAILABLE_EXIT_PENDING",
                )
                return
            except Exception as e:
                logger.error("Emergency SIM exit failed via pipeline", error=str(e), token=token)

        # Fallback: mark as EXIT_PENDING instead of writing a fake 0-price SELL
        await self.repo.append_system_event(
            "WARN", "PRICE",
            f"SIM exit pending for {token}: price API unavailable, will retry next cycle",
            _safe_json({"position_id": pos_id, "reason": "PRICE_API_UNAVAILABLE_EXIT_PENDING"}),
            account_type=account_type,
        )
        await self.repo.mark_position_exit_pending(pos_id, "PRICE_API_UNAVAILABLE_EXIT_PENDING")

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
