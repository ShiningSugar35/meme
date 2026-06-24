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
from ..trading.accounting import platform_fee_amount_raw
from ..trading.sim_sell_accounting import prepare_sim_sell_accounting


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


def _is_live_position(position: Dict[str, Any]) -> bool:
    account_type = str(position.get("account_type") or "").upper()
    if account_type in ("LIVE", "SIM"):
        return account_type == "LIVE"
    raw = position.get("is_live")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return int(raw) == 1
    return str(raw).strip().lower() in ("1", "true", "yes", "live")


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

    def __init__(self, repo: Repositories, trading_pipeline=None, gmgn=None):
        self.repo = repo
        self.trading_pipeline = trading_pipeline
        self.gmgn = gmgn

    # ------------------------------------------------------------------
    # Atomic claim — prevents concurrent duplicate exits
    # ------------------------------------------------------------------
    async def _claim_exit(self, position: Dict[str, Any], reason_code: str) -> Dict[str, Any]:
        pos_id = int(position["id"])
        fresh = await self.repo.get_position(pos_id)
        if not fresh:
            return {"ok": False, "error": "POSITION_NOT_FOUND"}
        status = str(fresh.get("status") or "").upper()
        if "CLOSED" in status:
            return {"ok": False, "error": "POSITION_ALREADY_CLOSED"}
        if status == "EXIT_PENDING":
            return {"ok": False, "error": "POSITION_ALREADY_EXITING"}
        remaining = _to_float(fresh.get("remaining_token_amount"), 0.0) or 0.0
        if remaining <= 0:
            return {"ok": False, "error": "ZERO_REMAINING"}
        original_status = str(fresh.get("status") or "POSITION_OPEN")
        affected = await self.repo.mark_position_exit_pending(pos_id, reason_code)
        if affected == 0:
            return {"ok": False, "error": "POSITION_ALREADY_EXITING"}
        return {"ok": True, "position": fresh, "original_status": original_status}

    async def _release_exit_claim(self, pos_id: int, original_status: str = "POSITION_OPEN"):
        try:
            await self.repo.restore_position_status(pos_id, original_status)
        except Exception:
            pass

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
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Close or partially exit a position.

        Every exit goes through an atomic EXIT_PENDING claim to prevent
        concurrent duplicate exits from different runners.

        SIM positions are paper-sold instantly.
        LIVE positions MUST be routed through TradingPipeline.execute_sell();
        if that fails the position is NOT closed and the claim is released.
        """
        # 1. Atomic claim — re-reads fresh position from DB
        claim = await self._claim_exit(position, reason_code)
        if not claim["ok"]:
            return claim

        fresh_position = claim["position"]
        original_status = claim.get("original_status", "POSITION_OPEN")
        pos_id = int(fresh_position["id"])
        token = fresh_position["token_mint"]
        account_type = _account_type(fresh_position)
        is_live = _is_live_position(fresh_position)

        exit_pct = max(0.0, min(1.0, float(exit_pct)))
        if exit_pct <= 0:
            await self._release_exit_claim(pos_id, original_status)
            return {"ok": False, "error": "ZERO_EXIT_PCT"}

        # Resolve price — try current_price, then GMGN live fetch, then DB fallback
        price = current_price_usd
        latest_snapshot: Dict[str, Any] = {}
        if price is None or price <= 0:
            if self.gmgn is not None:
                try:
                    latest_snapshot = await self.gmgn.fetch_latest_price(token, credential_slot=None)
                    price = _to_float(latest_snapshot.get("price_usd") or latest_snapshot.get("latest_price_usd") or latest_snapshot.get("price"))
                except Exception:
                    pass
        if price is None or price <= 0:
            price = (
                _to_float(fresh_position.get("last_fill_price_usd"))
                or _to_float(fresh_position.get("entry_price_usd"))
                or 0.0
            )

        try:
            # ---- LIVE pathway ----
            if is_live:
                result = await self._exit_live(fresh_position, exit_pct, reason_code, price, source, audit_context=audit_context)
                if result["ok"]:
                    if exit_pct < 0.999999:
                        await self._release_exit_claim(pos_id, original_status)
                else:
                    await self._release_exit_claim(pos_id, original_status)
                return result

            # ---- SIM pathway ----
            result = await self._exit_sim(fresh_position, exit_pct, reason_code, price, source, risk_details, audit_context, latest_override=latest_snapshot)
            # Partial exit: release claim so remaining can be re-sold; full exit already set CLOSED
            if result.get("ok") and exit_pct < 0.999999:
                await self._release_exit_claim(pos_id, original_status)
            return result
        except Exception:
            await self._release_exit_claim(pos_id, original_status)
            raise

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
        audit_context: Optional[Dict[str, Any]] = None,
        latest_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        pos_id = int(position["id"])
        token = position["token_mint"]
        remaining_token = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0

        ctx = await prepare_sim_sell_accounting(
            repo=self.repo,
            gmgn=self.gmgn,
            jupiter=self.trading_pipeline.jupiter if self.trading_pipeline else None,
            position=position,
            exit_pct=exit_pct,
            reason_code=reason_code,
            current_price_usd_override=current_price_usd,
            latest_override=latest_override,
        )

        pct = ctx["pct"]
        current_price_usd = ctx["current_price_usd"]
        current_price_sol = ctx["current_price_sol"]
        sell_amount_human = ctx["sell_amount_human"]
        new_remaining = ctx["new_remaining"]
        gross_value_usd = ctx["gross_value_usd"]
        acct = ctx["acct"]
        fee_detail_json = ctx["fee_detail_json"]
        sell_price_effective = ctx["sell_price_effective"]
        quote = ctx["quote"]
        quote_json = ctx["quote_json"]
        route_plan_json = ctx["route_plan_json"]
        price_impact_pct = ctx["price_impact_pct"]
        execution_detail = ctx["execution_detail"]

        exit_label = EXIT_REASON_LABELS.get(reason_code, reason_code)
        idem_key = f"SIM_SELL:{pos_id}:{reason_code}:{remaining_token:.6f}:{pct:.4f}"

        te = await self.repo.append_trade_event(
            idem_key,
            position_id=pos_id,
            token_mint=token,
            strategy_id=_position_strategy_id(position),
            is_live=0,
            account_type="SIM",
            side="SELL",
            event_type="SIM_SELL",
            status="CONFIRMED",
            requested_pct=pct,
            requested_token_amount=sell_amount_human,
            executed_token_amount=sell_amount_human,
            price_usd=current_price_usd,
            price_sol=current_price_sol,
            exit_reason=reason_code,
            exit_reason_label=exit_label,
            gross_value_usd=acct["gross_value_usd"],
            trade_value_usd_net=acct["trade_value_usd_net"],
            trade_value_usd_expected=acct["trade_value_usd_expected"],
            trade_value_usd_conservative=acct["trade_value_usd_conservative"],
            trade_value_usd_actual=acct["trade_value_usd_net"],
            fee_usd_est=acct["fee_usd_est"],
            fee_detail_json=fee_detail_json,
            execution_detail_json=json.dumps(execution_detail, ensure_ascii=False),
            accounting_source=acct["accounting_source"],
            accounting_status=acct["accounting_status"],
            sell_price_usd_effective=sell_price_effective,
            platform_fee_amount=platform_fee_amount_raw(quote),
            provider="PIPELINE_SIM",
            price_impact_pct=price_impact_pct,
            quote_json=quote_json,
            route_plan_json=route_plan_json,
        )

        if pct >= 0.999999 or new_remaining <= 0:
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

        # Position audit — use real trade_event and real quote
        if hasattr(self.repo, "insert_position_audit"):
            try:
                from ..trading.audit_builder import build_exit_audit_payload
                account_type = _account_type(position)
                audit_payload = await build_exit_audit_payload(
                    repo=self.repo,
                    position=position,
                    sell_trade_event=te,
                    exit_reason=reason_code,
                    exit_pct=pct,
                    sell_amount_human=sell_amount_human,
                    gross_value_usd=gross_value_usd,
                    current_price_usd=current_price_usd,
                    current_price_sol=current_price_sol,
                    quote=quote,
                    **(audit_context or {}),
                )
                await self.repo.insert_position_audit(
                    position_id=pos_id,
                    token_mint=token,
                    account_type=account_type,
                    strategy_id=_position_strategy_id(position),
                    discovery_event_id=position.get("discovery_event_id"),
                    snapshot_id=None,
                    audit_type="EXIT",
                    audit_json=audit_payload,
                )
            except Exception as e:
                logger.warning(
                    "Position exit audit insert failed",
                    position_id=pos_id,
                    reason=reason_code,
                    error=str(e),
                )

        await self.repo.append_system_event(
            "INFO", "TRADE",
            "SIM sell executed (paper)",
            _safe_json({
                "position_id": pos_id,
                "exit_pct": pct,
                "exit_reason": reason_code,
                "trade_event_id": te.get("id"),
                "jupiter_quote_ok": ctx["quote_ok"],
                "accounting_source": acct["accounting_source"],
                "accounting_status": acct["accounting_status"],
            }),
            account_type=_account_type(position),
        )

        logger.info(
            "PositionExitService SIM exit",
            position_id=pos_id,
            token=token,
            reason=reason_code,
            exit_pct=pct,
            source=source,
        )
        return {"ok": True, "mode": "SIM", "position_id": pos_id, "reason": reason_code, "exit_pct": pct}

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
        audit_context: Optional[Dict[str, Any]] = None,
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
                audit_context=audit_context,
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
