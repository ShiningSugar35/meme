from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, List
import json

from ..db.repositories import Repositories
from ..strategy.exit_rules import decide_exit
try:
    from ..strategy.filters import run_risk_filter
except Exception:  # Backward compatibility with older filters.py
    run_risk_filter = None
from ..strategy.filters import run_initial_filter
from ..services.event_bus import event_bus
from ..config import settings
from ..logging_config import logger


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        v = float(value)
        if v != v:
            return default
        return v
    except (TypeError, ValueError):
        return default


def _account_type(position: Dict[str, Any]) -> str:
    return position.get("account_type") or ("LIVE" if position.get("is_live") else "SIM")


def risk_scan_interval_seconds(remaining_value_usd: float) -> int:
    return int(settings.get_risk_scan_interval_seconds(float(remaining_value_usd or 0.0)))


def _extract_price_sol(latest: Dict[str, Any], position: Optional[Dict[str, Any]] = None) -> Optional[float]:
    latest = latest or {}
    position = position or {}

    for key in ("price_sol", "latest_price_sol", "current_price_sol"):
        value = _to_float(latest.get(key))
        if value and value > 0:
            return value

    # Compatibility fallback for old MOCK providers that returned SOL price as "price".
    # Real providers should populate price_sol explicitly to avoid USD/SOL mixing.
    provider_mode = str(getattr(settings, "PROVIDER_MODE", "") or "").upper()
    if "MOCK" in provider_mode:
        value = _to_float(latest.get("price"))
        if value and value > 0:
            return value

    # Last-resort fallback for legacy SIM positions only.
    if _account_type(position) == "SIM":
        value = _to_float(position.get("entry_price_sol"))
        if value and value > 0:
            return value

    return None


def _extract_price_usd(latest: Dict[str, Any]) -> Optional[float]:
    latest = latest or {}
    for key in ("price_usd", "latest_price_usd"):
        value = _to_float(latest.get(key))
        if value and value > 0:
            return value

    # Old PriceAggregator used "price" as USD price.
    value = _to_float(latest.get("price"))
    if value and value > 0:
        return value

    return None


def _remaining_value_sol(position: Dict[str, Any], current_price_sol: Optional[float]) -> float:
    remaining_value_sol = _to_float(position.get("remaining_value_sol"))
    if remaining_value_sol is not None and remaining_value_sol >= 0:
        # Use DB value only as a fallback. If fresh price exists, recompute below.
        fallback = remaining_value_sol
    else:
        fallback = 0.0

    remaining_token = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0
    if current_price_sol and current_price_sol > 0:
        return max(remaining_token * current_price_sol, 0.0)

    entry_price_sol = _to_float(position.get("entry_price_sol"))
    if entry_price_sol and entry_price_sol > 0:
        return max(remaining_token * entry_price_sol, 0.0)

    return fallback


def _remaining_value_usd(position: Dict[str, Any], current_price_usd: Optional[float]) -> float:
    remaining_value_usd = _to_float(position.get("remaining_value_usd"))
    fallback = remaining_value_usd if remaining_value_usd is not None and remaining_value_usd >= 0 else 0.0

    remaining_token = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0
    if current_price_usd and current_price_usd > 0:
        return max(remaining_token * current_price_usd, 0.0)

    entry_price_usd = _to_float(position.get("entry_price_usd"))
    if entry_price_usd and entry_price_usd > 0:
        return max(remaining_token * entry_price_usd, 0.0)

    return fallback


def _recheck_due(position: Dict[str, Any], now: datetime) -> bool:
    next_risk_check_at = _parse_dt(position.get("next_risk_check_at"))
    if next_risk_check_at is None:
        return True
    return now >= next_risk_check_at


def _safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


class PositionRiskRunner:
    """
    Risk and exit runner.

    This runner now does two separate jobs:
    1. Price-based exit decisions: hard TP/SL, dynamic TP/SL, dust, completed, time stop.
    2. Risk recheck: after entry, re-run the initial risk-threshold filter on a dynamic schedule:
       >=1.5 SOL every 2s; >=1.0 every 4s; >=0.5 every 8s;
       >=0.25 every 16s; <0.25 every 32s.

    If a TradingPipeline is supplied, all exits are sent through execute_sell().
    Without a pipeline, SIM positions are paper-exited while LIVE positions are never falsely closed.
    """

    def __init__(self, repo: Repositories, gmgn, trading_pipeline=None):
        self.repo = repo
        self.gmgn = gmgn
        self.trading_pipeline = trading_pipeline
        self._legacy_warned: set[int] = set()

    def set_trading_pipeline(self, trading_pipeline):
        self.trading_pipeline = trading_pipeline

    async def run_once(self):
        now = _utc_now()

        # Prefer DB-level due filtering when repositories.py from the first patch is installed.
        if hasattr(self.repo, "list_due_risk_check_positions"):
            positions = await self.repo.list_due_risk_check_positions(limit=200)
        else:
            positions = await self.repo.list_open_positions()

        for position in positions:
            try:
                await self._process_position(position, now)
            except Exception as e:
                token = position.get("token_mint")
                account_type = _account_type(position)
                logger.exception("PositionRiskRunner failed", token=token, error=str(e))
                await self.repo.append_system_event(
                    "ERROR",
                    "RISK",
                    f"PositionRiskRunner failed for {token}",
                    _safe_json_dumps({"position_id": position.get("id"), "error": str(e)}),
                    account_type=account_type,
                )

    async def _process_position(self, position: Dict[str, Any], now: datetime):
        token = position["token_mint"]
        pos_id = int(position["id"])
        account_type = _account_type(position)

        if not _recheck_due(position, now):
            return

        latest = await self._fetch_latest_price(token, account_type)
        price_sol = _extract_price_sol(latest, position)
        price_usd = _extract_price_usd(latest)
        remaining_value_sol = _remaining_value_sol(position, price_sol)
        remaining_value_usd = _remaining_value_usd(position, price_usd)

        interval = risk_scan_interval_seconds(remaining_value_usd)
        next_check_at = now + timedelta(seconds=interval)

        if hasattr(self.repo, "update_position_risk_schedule"):
            await self.repo.update_position_risk_schedule(
                position_id=pos_id,
                remaining_value_sol=remaining_value_sol,
                remaining_value_usd=remaining_value_usd,
                interval_seconds=interval,
                last_risk_check_at=_iso(now),
                next_risk_check_at=_iso(next_check_at),
            )

        if price_sol is None or price_sol <= 0:
            await self.repo.append_system_event(
                "WARN",
                "RISK",
                "Risk runner skipped position because SOL price is unavailable",
                _safe_json_dumps({"position_id": pos_id, "token": token, "latest": latest}),
                account_type=account_type,
            )
            return

        latest_snapshot = await self._fetch_latest_snapshot(token, account_type)
        token_info = await self.repo.get_token(token)
        if token_info and token_info.get("latest_type") and "type" not in latest_snapshot:
            latest_snapshot["type"] = token_info["latest_type"]

        ticks = await self.repo.get_recent_ticks(token, 60)
        prices_60 = [
            _to_float(t.get("price_sol"))
            for t in ticks
            if _to_float(t.get("price_sol")) is not None and _to_float(t.get("price_sol")) > 0
        ]
        if price_sol:
            prices_60.append(price_sol)

        rolling = {
            "low": min(prices_60) if prices_60 else price_sol,
            "high": max(prices_60) if prices_60 else price_sol,
        }

        position_for_decision = dict(position)
        position_for_decision["remaining_value_sol"] = remaining_value_sol
        position_for_decision["remaining_value_usd"] = remaining_value_usd
        if token_info and token_info.get("latest_type"):
            position_for_decision["latest_token_type"] = token_info["latest_type"]

        tick = {
            "price_sol": price_sol,
            "price_usd": price_usd,
            "remaining_value_sol": remaining_value_sol,
            "remaining_value_usd": remaining_value_usd,
        }

        # Risk recheck first: if security/risk thresholds degrade, full exit immediately.
        risk_ok = await self._risk_recheck(position_for_decision, latest_snapshot, now)
        if not risk_ok:
            await self._request_exit(
                position=position_for_decision,
                exit_pct=1.0,
                reason_code="RISK_RECHECK_FAILED",
                emergency=True,
                latest=latest,
                current_price_sol=price_sol,
                current_price_usd=price_usd,
            )
            return

        decision = await decide_exit(
            position_for_decision,
            tick,
            rolling,
            latest_snapshot,
            now=now,
            dust_force_exit_sol=float(getattr(settings, "DUST_FORCE_EXIT_SOL", 0.15)),
        )

        if not decision.should_exit:
            return

        # Prefer the highest-severity reason for idempotency and DB close reason.
        reason_code = self._primary_reason(decision)
        await self._request_exit(
            position=position_for_decision,
            exit_pct=decision.exit_pct,
            reason_code=reason_code,
            emergency=decision.emergency,
            latest=latest,
            current_price_sol=price_sol,
            current_price_usd=price_usd,
        )

    async def _fetch_latest_price(self, token: str, account_type: str) -> Dict[str, Any]:
        try:
            return await self.gmgn.fetch_latest_price(token)
        except Exception as e:
            await self.repo.append_system_event(
                "ERROR",
                "RISK",
                f"GMGN latest price failed for {token}",
                _safe_json_dumps({"error": str(e)}),
                account_type=account_type,
            )
            raise

    async def _fetch_latest_snapshot(self, token: str, account_type: str) -> Dict[str, Any]:
        try:
            snap = await self.gmgn.fetch_token_snapshot(token)
            return snap or {}
        except Exception as e:
            await self.repo.append_system_event(
                "ERROR",
                "RISK",
                f"GMGN token snapshot failed for {token}",
                _safe_json_dumps({"error": str(e)}),
                account_type=account_type,
            )
            return {}

    async def _risk_recheck(self, position: Dict[str, Any], snapshot: Dict[str, Any], now: datetime) -> bool:
        token = position["token_mint"]
        pos_id = int(position["id"])
        account_type = _account_type(position)

        locked = position.get("locked_strategy_config_json")
        legacy_status = position.get("legacy_config_status")

        if not locked:
            return True

        if legacy_status == "LEGACY_INVALID_CONFIG":
            if pos_id not in self._legacy_warned:
                self._legacy_warned.add(pos_id)
                await self.repo.append_system_event(
                    "WARN",
                    "RISK",
                    "Position has invalid legacy config, risk recheck skipped",
                    _safe_json_dumps({"position_id": pos_id, "legacy_config_status": legacy_status}),
                    account_type=account_type,
                )
            return True

        try:
            cfg = json.loads(locked)
            if legacy_status is None and hasattr(self.repo, "mark_position_legacy_config"):
                await self.repo.mark_position_legacy_config(pos_id, "VALID")
        except (json.JSONDecodeError, TypeError):
            if hasattr(self.repo, "mark_position_legacy_config"):
                await self.repo.mark_position_legacy_config(pos_id, "LEGACY_INVALID_CONFIG")
            await self.repo.append_system_event(
                "WARN",
                "RISK",
                "Position locked_strategy_config_json is invalid, risk recheck skipped",
                _safe_json_dumps({"position_id": pos_id}),
                account_type=account_type,
            )
            self._legacy_warned.add(pos_id)
            return True

        if not snapshot:
            # No complete risk snapshot means no forced risk stop. Do not close blindly.
            await self.repo.append_system_event(
                "WARN",
                "RISK",
                "Risk recheck skipped because token snapshot is empty",
                _safe_json_dumps({"position_id": pos_id, "token": token}),
                account_type=account_type,
            )
            return True

        # Preserve stable fields if the provider snapshot is partial.
        token_info = await self.repo.get_token(token)
        if token_info:
            snapshot = dict(snapshot)
            snapshot.setdefault("pool_created_at", token_info.get("pool_created_at"))
            snapshot.setdefault("type", token_info.get("latest_type"))
            snapshot.setdefault("pool_address", token_info.get("pool_address"))
            snapshot.setdefault("launchpad", token_info.get("launchpad"))

        if run_risk_filter is not None:
            res = await run_risk_filter(snapshot, cfg, now)
        else:
            res = await self._fallback_risk_filter(snapshot, cfg, now)

        strategy_id = cfg.get("id") or cfg.get("strategy_id") or position.get("live_strategy_id") or 0
        strategy_version = cfg.get("config_version") or position.get("strategy_config_version") or 1

        await self.repo.insert_strategy_match(
            token,
            int(strategy_id or 0),
            int(strategy_version or 1),
            snapshot.get("id"),
            "risk_recheck",
            bool(res.passed),
            _safe_json_dumps([getattr(d, "__dict__", d) for d in getattr(res, "details", [])]),
            _safe_json_dumps(getattr(res, "feature_vector", {})),
            discovery_event_id=position.get("discovery_event_id"),
        )

        if res.passed:
            return True

        await self.repo.append_system_event(
            "WARN",
            "RISK",
            "Risk recheck failed; requesting full exit",
            _safe_json_dumps(
                {
                    "position_id": pos_id,
                    "token": token,
                    "details": [getattr(d, "__dict__", d) for d in getattr(res, "details", [])],
                }
            ),
            account_type=account_type,
        )
        await event_bus.publish(
            "system",
            {
                "level": "WARN",
                "category": "RISK",
                "message": f"Risk recheck failed for {token}",
            },
        )
        return False

    async def _fallback_risk_filter(self, snapshot: Dict[str, Any], cfg: Dict[str, Any], now: datetime):
        """
        Compatibility fallback for older filters.py.

        It calls run_initial_filter but ignores lifecycle-only failures such as
        type/time window. This prevents mature held positions from being closed
        merely because they are no longer within the original entry-age window.
        """
        res = await run_initial_filter(snapshot, cfg, now)

        ignored_rule_names = {
            "type_new_creation",
            "type_is_new_creation",
            "pool_age_window",
            "time_window",
            "pool_created_at",
            "pool_created_at_parse",
        }

        details = list(getattr(res, "details", []) or [])
        failing_details = []
        for d in details:
            name = getattr(d, "name", None) or getattr(d, "rule", None) or ""
            passed = bool(getattr(d, "passed", False))
            if not passed and str(name) not in ignored_rule_names:
                failing_details.append(d)

        class _CompatResult:
            def __init__(self, passed, details, feature_vector):
                self.passed = passed
                self.details = details
                self.feature_vector = feature_vector

        return _CompatResult(
            passed=(len(failing_details) == 0),
            details=details,
            feature_vector=getattr(res, "feature_vector", {}),
        )

    def _primary_reason(self, decision) -> str:
        if not decision.reasons:
            return "EXIT"

        full_reasons = [r for r in decision.reasons if r.desired_exit_pct >= 1.0]
        if full_reasons:
            return full_reasons[0].name

        return decision.reasons[0].name

    async def _request_exit(
        self,
        position: Dict[str, Any],
        exit_pct: float,
        reason_code: str,
        emergency: bool,
        latest: Dict[str, Any],
        current_price_sol: float,
        current_price_usd: Optional[float],
    ):
        pos_id = int(position["id"])
        token = position["token_mint"]
        account_type = _account_type(position)
        is_live = bool(position.get("is_live"))

        remaining_value_sol = _remaining_value_sol(position, current_price_sol)
        dust_threshold = float(getattr(settings, "DUST_FORCE_EXIT_SOL", 0.15))
        if remaining_value_sol < dust_threshold:
            exit_pct = 1.0
            reason_code = "DUST_FORCE_EXIT"

        # One-shot rule idempotency. Full emergency exits are allowed to retry if a prior sell failed.
        if exit_pct < 1.0 and hasattr(self.repo, "has_exit_rule_executed"):
            if await self.repo.has_exit_rule_executed(pos_id, reason_code):
                return

        if self.trading_pipeline is not None and hasattr(self.trading_pipeline, "execute_sell"):
            ok = await self._try_pipeline_execute_sell(
                position=position,
                exit_pct=exit_pct,
                reason_code=reason_code,
                emergency=emergency,
                latest=latest,
            )
            if ok:
                if hasattr(self.repo, "mark_exit_rule_executed"):
                    await self.repo.mark_exit_rule_executed(pos_id, reason_code)
                return

        if is_live:
            await self.repo.append_trade_event(
                f"SELL_FAILED_PIPELINE_MISSING:{pos_id}:{reason_code}",
                position_id=pos_id,
                token_mint=token,
                strategy_id=position.get("live_strategy_id"),
                is_live=1,
                account_type=account_type,
                side="SELL",
                event_type="SELL",
                status="FAILED",
                requested_pct=exit_pct,
                price_sol=current_price_sol,
                price_usd=current_price_usd,
                error_code="TRADING_PIPELINE_MISSING",
                error_message="LIVE exit was requested, but no TradingPipeline.execute_sell was available.",
                provider="POSITION_RISK",
            )
            await self.repo.append_system_event(
                "ERROR",
                "RISK",
                "LIVE exit requested but TradingPipeline is missing; position not closed",
                _safe_json_dumps({"position_id": pos_id, "token": token, "reason": reason_code}),
                account_type=account_type,
            )
            return

        await self._paper_exit(
            position=position,
            exit_pct=exit_pct,
            reason_code=reason_code,
            current_price_sol=current_price_sol,
            current_price_usd=current_price_usd,
        )

    async def _try_pipeline_execute_sell(
        self,
        position: Dict[str, Any],
        exit_pct: float,
        reason_code: str,
        emergency: bool,
        latest: Dict[str, Any],
    ) -> bool:
        fn = self.trading_pipeline.execute_sell

        call_variants = [
            lambda: fn(position=position, exit_pct=exit_pct, exit_reason=reason_code, emergency=emergency, latest_price=latest),
            lambda: fn(position=position, exit_pct=exit_pct, reason=reason_code, emergency=emergency),
            lambda: fn(position, exit_pct, reason_code, emergency),
            lambda: fn(position["id"], exit_pct, reason_code),
        ]

        last_error = None
        for call in call_variants:
            try:
                result = await call()
                if result is None:
                    return True
                if isinstance(result, dict):
                    status = str(result.get("status") or result.get("result") or "").upper()
                    if result.get("ok") is True or result.get("success") is True:
                        return True
                    if status in {"CONFIRMED", "FILLED", "SUCCESS", "OK"}:
                        return True
                    if status in {"FAILED", "ERROR", "REJECTED"}:
                        return False
                return bool(result)
            except TypeError as e:
                last_error = e
                continue
            except Exception as e:
                last_error = e
                break

        logger.warning("execute_sell call failed", error=str(last_error), position_id=position.get("id"))
        return False

    async def _paper_exit(
        self,
        position: Dict[str, Any],
        exit_pct: float,
        reason_code: str,
        current_price_sol: float,
        current_price_usd: Optional[float],
    ):
        pos_id = int(position["id"])
        token = position["token_mint"]
        account_type = _account_type(position)
        now = _utc_now()

        remaining_token = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0
        sell_token_amount = max(remaining_token * min(max(exit_pct, 0.0), 1.0), 0.0)
        new_remaining = max(remaining_token - sell_token_amount, 0.0)

        executed_sol_amount = sell_token_amount * current_price_sol if current_price_sol else 0.0
        executed_usd_amount = sell_token_amount * current_price_usd if current_price_usd else None

        existing_return = _to_float(position.get("total_return_sol"), 0.0) or 0.0
        total_return_sol = existing_return + executed_sol_amount

        total_cost_sol = _to_float(position.get("total_cost_sol"), 0.0) or 0.0
        realized_pnl_sol = total_return_sol - total_cost_sol if total_cost_sol else None
        realized_pnl_pct = (realized_pnl_sol / total_cost_sol) if total_cost_sol and realized_pnl_sol is not None else None

        await self.repo.append_trade_event(
            f"SELL_SIM:{pos_id}:{reason_code}",
            position_id=pos_id,
            token_mint=token,
            strategy_id=position.get("live_strategy_id"),
            is_live=0,
            account_type=account_type,
            side="SELL",
            event_type="SELL",
            status="CONFIRMED",
            requested_pct=exit_pct,
            requested_token_amount=sell_token_amount,
            executed_token_amount=sell_token_amount,
            executed_sol_amount=executed_sol_amount,
            price_sol=current_price_sol,
            price_usd=current_price_usd,
            provider="POSITION_RISK_SIM",
        )

        if exit_pct >= 1.0 or new_remaining <= 0:
            await self.repo.close_position(
                pos_id,
                close_reason=reason_code,
                total_return_sol=total_return_sol,
                realized_pnl_sol=realized_pnl_sol,
                realized_pnl_pct=realized_pnl_pct,
                pnl_pct=realized_pnl_pct,
            )
            await event_bus.publish(
                "system",
                {
                    "level": "INFO",
                    "category": "RISK",
                    "message": f"Full SIM exit for {token}: {reason_code}",
                },
            )
        else:
            await self.repo.update_position_remaining(
                pos_id,
                remaining_token_amount=new_remaining,
                remaining_value_usd=executed_usd_amount,
                remaining_value_sol=new_remaining * current_price_sol if current_price_sol else None,
                last_fill_at=_iso(now),
                last_fill_price_usd=current_price_usd,
                last_fill_price_sol=current_price_sol,
                total_return_sol=total_return_sol,
                realized_pnl_sol=realized_pnl_sol,
                realized_pnl_pct=realized_pnl_pct,
                pnl_pct=realized_pnl_pct,
            )
            await event_bus.publish(
                "system",
                {
                    "level": "INFO",
                    "category": "RISK",
                    "message": f"Partial SIM exit {exit_pct:.0%} for {token}: {reason_code}",
                },
            )

        if hasattr(self.repo, "mark_exit_rule_executed"):
            await self.repo.mark_exit_rule_executed(pos_id, reason_code)
