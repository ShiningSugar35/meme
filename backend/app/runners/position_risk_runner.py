from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
import json

from ..db.repositories import Repositories
from ..strategy.thresholds import compute_thresholds, normalize_rate_fraction
from ..strategy.filters import run_holding_risk_filter
from ..services.event_bus import event_bus
from ..config import settings
from ..logging_config import logger
from ..strategy.exit_rules import _executed_exit_rules
from .discovery_runner import acquire_feature_slot


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


def _normalize_amount_percentage(value: Any) -> Optional[float]:
    return normalize_rate_fraction(_to_float(value))


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
        self._last_smart_degen_sell: Dict[int, Dict[str, float]] = {}

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
        price_usd = _extract_price_usd(latest)
        remaining_value_usd = _remaining_value_usd(position, price_usd)

        if price_usd is None or price_usd <= 0:
            await self.repo.append_system_event(
                "WARN",
                "RISK",
                "Risk runner skipped position because USD price is unavailable",
                _safe_json_dumps({"position_id": pos_id, "token": token, "latest": latest}),
                account_type=account_type,
            )
            return

        interval = risk_scan_interval_seconds(remaining_value_usd)
        next_check_at = now + timedelta(seconds=interval)

        if hasattr(self.repo, "update_position_risk_schedule"):
            await self.repo.update_position_risk_schedule(
                position_id=pos_id,
                remaining_value_usd=remaining_value_usd,
                interval_seconds=interval,
                last_risk_check_at=_iso(now),
                next_risk_check_at=_iso(next_check_at),
            )

        latest_snapshot = await self._fetch_latest_snapshot(token, account_type)
        token_info = await self.repo.get_token(token)
        if token_info and token_info.get("latest_type") and "type" not in latest_snapshot:
            latest_snapshot["type"] = token_info["latest_type"]

        ticks = await self.repo.get_recent_ticks(token, 60)
        prices_60 = [
            _to_float(t.get("price_usd"))
            for t in ticks
            if _to_float(t.get("price_usd")) is not None and _to_float(t.get("price_usd")) > 0
        ]
        if price_usd:
            prices_60.append(price_usd)

        rolling = {
            "low": min(prices_60) if prices_60 else price_usd,
            "high": max(prices_60) if prices_60 else price_usd,
        }

        position_for_decision = dict(position)
        position_for_decision["remaining_value_usd"] = remaining_value_usd
        if token_info and token_info.get("latest_type"):
            position_for_decision["latest_token_type"] = token_info["latest_type"]

        tick = {
            "price_usd": price_usd,
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
                current_price_usd=price_usd,
            )
            return

        # Completed type check: if token is completed, full exit immediately.
        # This is checked both in PositionRiskRunner (as safety net) and
        # ActivePositionPriceRunner (primary price runner).
        token_type = latest_snapshot.get("type") or latest_snapshot.get("token_type")
        if token_type == "completed" and token_type != position.get("latest_token_type"):
            await self._request_exit(
                position=position_for_decision,
                exit_pct=1.0,
                reason_code="COMPLETED",
                emergency=True,
                latest=latest,
                current_price_usd=price_usd,
            )
            return

        # Dust force exit check (keep in PositionRiskRunner since it's not price-based)
        if remaining_value_usd is not None and remaining_value_usd < float(getattr(settings, "DUST_FORCE_EXIT_USD", 12.5)):
            await self._request_exit(
                position=position_for_decision,
                exit_pct=1.0,
                reason_code="DUST_FORCE_EXIT",
                emergency=True,
                latest=latest,
                current_price_usd=price_usd,
            )
            return

        # Top1 holder continuous monitoring: check addr_type=0 share against threshold
        top1_failed = await self._check_top1_holder(position_for_decision, now)
        if top1_failed:
            await self._request_exit(
                position=position_for_decision,
                exit_pct=1.0,
                reason_code="TOP1_HOLDER_RISK",
                emergency=True,
                latest=latest,
                current_price_usd=price_usd,
            )
            return

        # Smart money sell monitoring (feature-flagged, polling)
        smart_dump = await self._check_smart_money_sell(position_for_decision, now)
        if smart_dump:
            await self._request_exit(
                position=position_for_decision,
                exit_pct=0.5,
                reason_code="SMART_MONEY_SELL",
                emergency=False,
                latest=latest,
                current_price_usd=price_usd,
            )
            return

        # TOP3 smart degen reduction check: if any of the original TOP3 reduced
        # holdings by >25%, exit 50%
        top3_triggered_wallet = await self._check_top3_smart_degen_reduction(position_for_decision, now)
        if top3_triggered_wallet:
            await self._request_exit(
                position=position_for_decision,
                exit_pct=0.5,
                reason_code="TOP3_SMART_DEGEN_DUMP",
                emergency=False,
                latest=latest,
                current_price_usd=price_usd,
                triggered_wallet=top3_triggered_wallet,
            )
            return

        # NOTE: Price-based exit rules (HARD_TP, HARD_SL, DYN_SL, TIME_STOPLOSS)
        # are handled exclusively by ActivePositionPriceRunner to avoid duplicate triggers.
        # PositionRiskRunner covers only holding risk, TOP1 holder, smart money sell,
        # TOP3 smart degen dump, completed, and dust force exit.

    async def _fetch_latest_price(self, token: str, account_type: str) -> Dict[str, Any]:
        slot = acquire_feature_slot("risk_price")
        try:
            return await self.gmgn.fetch_latest_price(token, credential_slot=slot)
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

        res = await run_holding_risk_filter(snapshot, cfg, now)

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

    async def _check_top1_holder(self, position: Dict[str, Any], now: datetime) -> bool:
        """Check top1 holder (addr_type=0) share against threshold.

        Called inline during the risk scan cycle (no independent TOP1_HOLDER_SCAN_TIER).
        Returns True if risk is triggered (full exit needed), False otherwise.
        """
        pos_id = int(position["id"])
        token = position["token_mint"]
        account_type = _account_type(position)

        locked = position.get("locked_strategy_config_json")
        cfg: Dict[str, Any] = {}
        if locked:
            try:
                cfg = json.loads(locked)
            except (json.JSONDecodeError, TypeError):
                pass

        x_val = float(cfg.get("x") if cfg.get("x") is not None else settings.STRATEGY_DEFAULT_X)
        t = compute_thresholds(x_val)
        threshold = t.top1_addr_type0_max

        slot = acquire_feature_slot("risk_top1_holder")
        try:
            holders = await self.gmgn.fetch_top_holders(token, limit=20, credential_slot=slot)
        except Exception as e:
            await self.repo.append_system_event(
                "WARN",
                "RISK",
                "Top1 holder fetch failed, skipping check",
                _safe_json_dumps({"position_id": pos_id, "token": token, "error": str(e)}),
                account_type=account_type,
            )
            return False

        for h in holders:
            if int(h.get("addr_type", 0)) == 0:
                top1_rate = normalize_rate_fraction(_to_float(h.get("top1_holder_rate") or h.get("rate") or h.get("amount_percentage")))
                if top1_rate is not None and top1_rate >= threshold:
                    await self.repo.append_system_event(
                        "WARN",
                        "RISK",
                        "Top1 holder rate exceeds threshold; requesting full exit",
                        _safe_json_dumps({
                            "position_id": pos_id,
                            "token": token,
                            "top1_rate": top1_rate,
                            "threshold": threshold,
                            "x": x_val,
                        }),
                        account_type=account_type,
                    )
                    return True
                break

        return False

    async def _check_smart_money_sell(self, position: Dict[str, Any], now: datetime) -> bool:
        if not getattr(settings, "SMART_MONEY_SELL_MONITOR_ENABLED", False):
            return False
        pos_id = int(position["id"])
        remaining_value_usd = _to_float(position.get("remaining_value_usd"), 0.0) or 0.0
        interval = settings.get_risk_scan_interval_seconds(remaining_value_usd)
        if interval <= 0:
            return False

        last_check = self._last_smart_degen_sell.get(pos_id, {})
        last_time_val = last_check.get("_ts")
        if last_time_val is not None and now.timestamp() < last_time_val + interval:
            return False

        token = position["token_mint"]
        account_type = _account_type(position)

        slot = acquire_feature_slot("risk_smart_money")
        try:
            holders = await self.gmgn.fetch_smart_degen_holders(token, limit=20, credential_slot=slot)
        except Exception:
            return False

        if not holders:
            return False

        sell_trigger = float(getattr(settings, "SMART_MONEY_SELL_THRESHOLD_USD", 50.0))
        triggered = False
        for h in holders:
            addr = h.get("address") or ""
            if not addr:
                continue
            sell_cur = _to_float(h.get("sell_volume_cur")) or 0.0
            prev = last_check.get(addr, 0.0)
            delta = sell_cur - prev
            if delta > sell_trigger:
                triggered = True
                await self.repo.append_system_event(
                    "INFO", "RISK",
                    f"Smart money sell detected: wallet {addr} sold +${delta:.0f}",
                    _safe_json_dumps({
                        "position_id": pos_id, "token": token,
                        "wallet": addr, "sell_delta_usd": delta,
                        "threshold": sell_trigger,
                    }),
                    account_type=account_type,
                )
                break

        snapshot: Dict[str, Any] = {"_ts": now.timestamp()}
        for h in holders:
            addr = h.get("address") or ""
            snapshot[addr] = _to_float(h.get("sell_volume_cur")) or 0.0
        self._last_smart_degen_sell[pos_id] = snapshot

        return triggered

    async def _check_top3_smart_degen_reduction(self, position: Dict[str, Any], now: datetime) -> Optional[str]:
        """Check if any of the original TOP3 smart degen holders reduced holdings by >25%.

        Per-wallet one-shot: once a wallet triggers an exit, it is recorded in
        executed_exit_rules_json as TOP3_SMART_DEGEN_DUMP:<address> and will not
        trigger a second time.

        Comparison priority: token amount > amount_percentage.  Do NOT use usd_value
        alone because price change affects it.

        Returns the triggered wallet address if risk is detected, None otherwise.
        IMPORTANT: does NOT mark exit rule as executed — that happens only after
        the sell succeeds (in _request_exit), so a failed sell can be retried.
        """
        locked = position.get("locked_strategy_config_json")
        if not locked:
            return None
        try:
            cfg = json.loads(locked)
        except (json.JSONDecodeError, TypeError):
            return None

        token = position["token_mint"]
        pos_id = int(position["id"])

        top3_snapshot = cfg.get("top3_smart_degen_snapshot")
        if not top3_snapshot or not isinstance(top3_snapshot, list):
            baselines = []
            try:
                baselines = await self.repo.get_position_smart_money_baselines(pos_id)
            except Exception as e:
                logger.warning(
                    "top3 smart degen baseline fallback failed",
                    position_id=pos_id,
                    token=token,
                    error=str(e),
                )
            if baselines:
                top3_snapshot = [
                    {"address": b.get("wallet_address", ""),
                     "amount_percentage": _normalize_amount_percentage(b.get("baseline_amount_percentage")),
                     "usd_value": _to_float(b.get("baseline_usd_value"), 0.0)}
                    for b in baselines if b.get("wallet_address")
                ]
        if not top3_snapshot or not isinstance(top3_snapshot, list):
            return None

        executed = _executed_exit_rules(position)
        already_triggered_wallets: set[str] = set()
        for rule in executed:
            if rule.startswith("TOP3_SMART_DEGEN_DUMP:"):
                already_triggered_wallets.add(rule.split(":", 1)[1])

        slot = acquire_feature_slot("risk_top3_degen")
        try:
            holders = await self.gmgn.fetch_smart_degen_holders(token, limit=20, credential_slot=slot)
        except Exception:
            return None

        current_map: Dict[str, Dict[str, float]] = {}
        if holders:
            for h in holders:
                addr = h.get("address", "")
                if addr:
                    current_map[addr] = {
                        "token_amount": float(h.get("token_amount") or 0),
                        "amount_percentage": _normalize_amount_percentage(h.get("amount_percentage")) or 0.0,
                    }

        triggered_addr: Optional[str] = None
        for snap in top3_snapshot:
            addr = snap.get("address", "")
            if not addr or addr in already_triggered_wallets:
                continue

            if addr not in current_map:
                triggered_addr = addr
                break

            curr = current_map[addr]
            initial_token = float(snap.get("token_amount") or 0)
            initial_pct = _normalize_amount_percentage(snap.get("amount_percentage")) or 0.0

            if initial_token > 0:
                current_token = curr["token_amount"]
                if current_token <= 0:
                    triggered_addr = addr
                    break
                reduction = (initial_token - current_token) / initial_token
                if reduction > 0.25:
                    triggered_addr = addr
                    break
            elif initial_pct > 0:
                current_pct = curr["amount_percentage"]
                if current_pct <= 0:
                    triggered_addr = addr
                    break
                reduction = (initial_pct - current_pct) / initial_pct
                if reduction > 0.25:
                    triggered_addr = addr
                    break

        if triggered_addr is not None:
            account_type = _account_type(position)
            await self.repo.append_system_event(
                "WARN", "RISK",
                f"TOP3 smart degen dump detected: {triggered_addr}",
                _safe_json_dumps({
                    "position_id": pos_id, "token": token,
                    "address": triggered_addr,
                }),
                account_type=account_type,
            )
            return triggered_addr

        return None

    async def _request_exit(
        self,
        position: Dict[str, Any],
        exit_pct: float,
        reason_code: str,
        emergency: bool,
        latest: Dict[str, Any],
        current_price_usd: Optional[float],
        triggered_wallet: Optional[str] = None,
    ):
        pos_id = int(position["id"])
        token = position["token_mint"]
        account_type = _account_type(position)
        is_live = bool(position.get("is_live"))

        remaining_value_usd = _to_float(position.get("remaining_value_usd"), 0.0) or 0.0
        dust_threshold = float(getattr(settings, "DUST_FORCE_EXIT_USD", 0.50))
        if remaining_value_usd < dust_threshold:
            exit_pct = 1.0
            reason_code = "DUST_FORCE_EXIT"

        # One-shot rule idempotency. Full emergency exits are allowed to retry if a prior sell failed.
        if exit_pct < 1.0 and hasattr(self.repo, "has_exit_rule_executed"):
            if await self.repo.has_exit_rule_executed(pos_id, reason_code):
                return

        if self.trading_pipeline is not None and hasattr(self.trading_pipeline, "execute_sell") and is_live:
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
                    if triggered_wallet:
                        wallet_rule = f"TOP3_SMART_DEGEN_DUMP:{triggered_wallet}"
                        await self.repo.mark_exit_rule_executed(pos_id, wallet_rule)
                if exit_pct < 1.0:
                    if hasattr(self.repo, "update_position_remaining"):
                        now_iso = _iso(_utc_now())
                        remaining_token = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0
                        sell_amt = remaining_token * exit_pct
                        new_remaining = max(0.0, remaining_token - sell_amt)
                        new_value = new_remaining * (current_price_usd or 0.0)
                        await self.repo.update_position_remaining(
                            pos_id,
                            new_remaining,
                            new_value,
                            last_fill_at=now_iso,
                            last_fill_price_usd=current_price_usd,
                        )
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
            current_price_usd=current_price_usd,
            triggered_wallet=triggered_wallet,
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

        try:
            result = await fn(position=position, exit_pct=exit_pct, exit_reason=reason_code)
        except TypeError as e:
            # Fallback for older signatures
            try:
                result = await fn(position, exit_pct, reason_code)
            except Exception as e2:
                logger.warning("execute_sell call failed", error=str(e2), position_id=position.get("id"))
                return False
        except Exception as e:
            logger.warning("execute_sell call failed", error=str(e), position_id=position.get("id"))
            return False

        if result is None:
            return True
        if isinstance(result, dict):
            if result.get("ok") is False or result.get("success") is False:
                return False
            status = str(result.get("status") or result.get("result") or "").upper()
            if result.get("ok") is True or result.get("success") is True:
                return True
            if status in {"CONFIRMED", "FILLED", "SUCCESS", "OK"}:
                return True
            if status in {"FAILED", "ERROR", "REJECTED"}:
                return False
        return bool(result)

    async def _paper_exit(
        self,
        position: Dict[str, Any],
        exit_pct: float,
        reason_code: str,
        current_price_usd: Optional[float],
        triggered_wallet: Optional[str] = None,
    ):
        pos_id = int(position["id"])
        token = position["token_mint"]
        account_type = _account_type(position)
        now = _utc_now()

        remaining_token = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0
        sell_token_amount = max(remaining_token * min(max(exit_pct, 0.0), 1.0), 0.0)
        new_remaining = max(remaining_token - sell_token_amount, 0.0)
        new_value = new_remaining * (current_price_usd or 0.0)

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
            price_usd=current_price_usd,
            provider="POSITION_RISK_SIM",
        )

        if exit_pct >= 1.0 or new_remaining <= 0:
            await self.repo.close_position(
                pos_id,
                close_reason=reason_code,
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
                remaining_value_usd=new_value,
                last_fill_at=_iso(now),
                last_fill_price_usd=current_price_usd,
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
            if triggered_wallet:
                wallet_rule = f"TOP3_SMART_DEGEN_DUMP:{triggered_wallet}"
                await self.repo.mark_exit_rule_executed(pos_id, wallet_rule)
