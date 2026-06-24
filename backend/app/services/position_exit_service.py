"""Unified position exit service — single SIM/LIVE exit pathway.

Every automated exit (price, risk, soft stop) and manual sell MUST go through
this service.  No runner or API endpoint should duplicate SIM/LIVE sell logic.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..db.repositories import Repositories
from ..logging_config import logger
from ..strategy.exit_rules import EXIT_REASON_LABELS


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class PositionExitService:
    """Single entry point for closing or partially exiting a position.

    Usage (from any runner / API):
        exit_svc = PositionExitService(repo, trading_pipeline=tpl)
        result = await exit_svc.exit_position(
            position=pos,
            exit_pct=1.0,
            reason_code="MANUAL_SELL",
            current_price_usd=0.00123,
            source="MANUAL_API",
        )
    """

    def __init__(self, repo: Repositories, trading_pipeline=None):
        self.repo = repo
        self.trading_pipeline = trading_pipeline

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def exit_position(
        self,
        position: Dict[str, Any],
        exit_pct: float,
        reason_code: str,
        current_price_usd: Optional[float] = None,
        emergency: bool = False,
        source: str = "UNKNOWN",
        risk_details: Optional[list] = None,
        triggered_wallet: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Close or partially exit a position.

        Returns  {"ok": True/False, ...}  usable by every caller.

        SIM positions are paper-sold instantly.
        LIVE positions MUST be routed through TradingPipeline.execute_sell();
        if that fails the position is NOT closed.
        """
        pos_id = int(position["id"])
        token = position["token_mint"]
        account_type = _account_type(position)
        is_live = bool(position.get("is_live"))

        exit_pct = max(0.0, min(1.0, float(exit_pct)))
        if exit_pct <= 0:
            return {"ok": False, "error": "ZERO_EXIT_PCT"}

        remaining_token = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0
        if remaining_token <= 0:
            return {"ok": False, "error": "ZERO_REMAINING"}

        # Resolve price
        price = current_price_usd
        if price is None:
            price = (
                _to_float(position.get("last_fill_price_usd"))
                or _to_float(position.get("entry_price_usd"))
                or 0.0
            )

        # ---- LIVE pathway ----
        if is_live:
            return await self._exit_live(position, exit_pct, reason_code, price, source)

        # ---- SIM pathway ----
        return await self._exit_sim(position, exit_pct, reason_code, price, source, risk_details, triggered_wallet)

    # ------------------------------------------------------------------
    # SIM paper exit
    # ------------------------------------------------------------------
    async def _exit_sim(
        self,
        position: Dict[str, Any],
        exit_pct: float,
        reason_code: str,
        current_price_usd: float,
        source: str,
        risk_details: Optional[list] = None,
        triggered_wallet: Optional[str] = None,
    ) -> Dict[str, Any]:
        pos_id = int(position["id"])
        token = position["token_mint"]
        remaining_token = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0

        sell_amount = remaining_token * exit_pct
        new_remaining = max(0.0, remaining_token - sell_amount)
        trade_value_usd_net = sell_amount * current_price_usd

        exit_label = EXIT_REASON_LABELS.get(reason_code, reason_code)

        await self.repo.append_trade_event(
            f"EXIT_SVC:{pos_id}:{reason_code}",
            position_id=pos_id,
            token_mint=token,
            strategy_id=_position_strategy_id(position),
            is_live=0,
            account_type="SIM",
            side="SELL",
            event_type="SIM_SELL",
            status="CONFIRMED",
            requested_pct=exit_pct,
            executed_token_amount=sell_amount,
            price_usd=current_price_usd,
            exit_reason=reason_code,
            exit_reason_label=exit_label,
            trade_value_usd_net=trade_value_usd_net,
            fee_detail_json=json.dumps({"fallback": True, "source": source}),
            provider=source,
        )

        if exit_pct >= 0.999999 or new_remaining <= 0:
            close_reason = reason_code
            if risk_details:
                close_reason = f"{reason_code} ({', '.join(str(d) for d in risk_details)})"
            await self.repo.close_position(pos_id, close_reason=close_reason)
        else:
            await self.repo.update_position_remaining(
                pos_id,
                new_remaining,
                new_remaining * current_price_usd,
                last_fill_at=_utc_now_iso(),
                last_fill_price_usd=current_price_usd,
            )

        if hasattr(self.repo, "mark_exit_rule_executed"):
            await self.repo.mark_exit_rule_executed(pos_id, reason_code)

        logger.info(
            "PositionExitService SIM exit",
            position_id=pos_id,
            token=token,
            reason=reason_code,
            exit_pct=exit_pct,
            source=source,
        )
        return {"ok": True, "mode": "SIM", "position_id": pos_id, "reason": reason_code, "exit_pct": exit_pct}

    # ------------------------------------------------------------------
    # LIVE exit (must go through trading pipeline)
    # ------------------------------------------------------------------
    async def _exit_live(
        self,
        position: Dict[str, Any],
        exit_pct: float,
        reason_code: str,
        current_price_usd: float,
        source: str,
    ) -> Dict[str, Any]:
        pos_id = int(position["id"])
        token = position["token_mint"]
        account_type = _account_type(position)

        if self.trading_pipeline is None or not hasattr(self.trading_pipeline, "execute_sell"):
            logger.error(
                "LIVE exit blocked: no trading pipeline available",
                position_id=pos_id,
                token=token,
                reason=reason_code,
            )
            await self.repo.append_system_event(
                "ERROR", "EXIT",
                f"LIVE exit blocked: no trading pipeline for {token} ({reason_code})",
                _safe_json({"position_id": pos_id, "reason": reason_code}),
                account_type=account_type,
            )
            return {"ok": False, "error": "NO_TRADING_PIPELINE"}

        try:
            result = await self.trading_pipeline.execute_sell(
                position=position,
                exit_pct=exit_pct,
                exit_reason=reason_code,
            )
        except Exception as exc:
            logger.exception("LIVE execute_sell raised exception", position_id=pos_id, token=token)
            await self.repo.append_system_event(
                "ERROR", "EXIT",
                f"LIVE execute_sell exception for {token}: {exc}",
                _safe_json({"position_id": pos_id, "reason": reason_code, "error": str(exc)}),
                account_type=account_type,
            )
            return {"ok": False, "error": str(exc)}

        if result and (result.get("ok") is True or result.get("success") is True):
            if hasattr(self.repo, "mark_exit_rule_executed"):
                await self.repo.mark_exit_rule_executed(pos_id, reason_code)

            await self.repo.append_system_event(
                "INFO", "EXIT",
                f"LIVE exit succeeded: {reason_code} for {token}",
                _safe_json({"position_id": pos_id, "exit_pct": exit_pct, "reason": reason_code}),
                account_type=account_type,
            )
            return {"ok": True, "mode": "LIVE", "position_id": pos_id, "reason": reason_code, "exit_pct": exit_pct}

        # execute_sell returned failure — do NOT close DB position
        logger.warning(
            "LIVE execute_sell returned failure; position NOT closed",
            position_id=pos_id,
            token=token,
            reason=reason_code,
            result_ok=result.get("ok") if result else None,
        )
        await self.repo.append_system_event(
            "WARN", "EXIT",
            f"LIVE exit failed for {token} ({reason_code}); position stays open",
            _safe_json({"position_id": pos_id, "reason": reason_code, "result": result}),
            account_type=account_type,
        )
        return {"ok": False, "error": "EXECUTE_SELL_FAILED", "detail": result}
