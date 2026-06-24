"""Second-level price monitoring runner for open positions.

Polls all open positions every ACTIVE_POSITION_PRICE_POLL_SECONDS and
evaluates hard TP/SL plus completed exits via the unified exit_rules module.

Each position makes at most ONE price API request per polling cycle.
On failure the position stays open and is retried next cycle.
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
from ..strategy.exit_rules import decide_exit, EXIT_REASON_LABELS, _executed_exit_rules
from ..services.position_exit_service import PositionExitService
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


def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(obj), ensure_ascii=False)


class ActivePositionPriceRunner:
    """Price-only runner for open positions.

    Polls every ACTIVE_POSITION_PRICE_POLL_SECONDS.  Delegates all exit
    decisions to exit_rules.decide_exit() and execution to PositionExitService.
    """

    @staticmethod
    def _failure_key(position: Dict[str, Any]) -> str:
        return f"{position.get('id') or 0}:{_account_type(position)}:{position.get('token_mint', '?')}"

    def __init__(self, repo: Repositories, gmgn, trading_pipeline=None):
        self.repo = repo
        self.gmgn = gmgn
        self.trading_pipeline = trading_pipeline
        self.exit_service = PositionExitService(repo, trading_pipeline=trading_pipeline, gmgn=gmgn)
        self._last_price_update: Dict[int, Dict[str, Any]] = {}
        self._consecutive_price_failures: Dict[str, int] = {}
        self._last_failure_event_at: Dict[str, float] = {}
        self._failure_start_time: Dict[str, float] = {}
        self._last_snapshot_fetch_at: Dict[str, float] = {}

    def set_trading_pipeline(self, trading_pipeline):
        self.trading_pipeline = trading_pipeline
        self.exit_service.trading_pipeline = trading_pipeline

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

        # Build tick dict for unified exit rules
        tick = {"price_usd": current_price, "remaining_value_usd": remaining_value_usd}

        # Build latest_snapshot (try to get up-to-date type)
        latest_snapshot: Dict[str, Any] = {}
        token_type = position.get("latest_token_type") or position.get("type")
        if token_type:
            latest_snapshot["type"] = token_type
        if token_type != "completed":
            now_ts = time.time()
            last_snap = self._last_snapshot_fetch_at.get(token, 0.0)
            if now_ts - last_snap >= 30.0:
                self._last_snapshot_fetch_at[token] = now_ts
                try:
                    slot = acquire_holding_slot("price_runner_snapshot")
                    snap = await self.gmgn.fetch_token_snapshot(token, credential_slot=slot)
                    if snap:
                        snap_type = snap.get("type") or snap.get("token_type")
                        if snap_type:
                            latest_snapshot["type"] = snap_type
                except Exception:
                    pass

        # Delegate to unified exit rules
        decision = await decide_exit(
            position=position,
            tick=tick,
            rolling_60s={},
            latest_snapshot=latest_snapshot,
            now=now,
        )

        if not decision.should_exit:
            return

        await self.exit_service.exit_position(
            position=position,
            exit_pct=decision.exit_pct,
            reason_code=decision.reasons[0].name,
            current_price_usd=current_price,
            source="PRICE_RUNNER",
        )

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
