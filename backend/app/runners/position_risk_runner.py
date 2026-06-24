from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set
import json

from ..db.repositories import Repositories
from ..strategy.thresholds import normalize_rate_fraction
from ..strategy.filters import run_holding_risk_filter
from ..services.event_bus import event_bus
from ..config import settings
from ..logging_config import logger
from ..strategy.exit_rules import _executed_exit_rules, EXIT_REASON_LABELS
from ..providers.credential_router import get_credential_router
from ..providers.rate_limiter import get_rate_limiter
from ..services.position_exit_service import PositionExitService
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


def _risk_detail_summary(details: Any) -> List[str]:
    out: List[str] = []
    for d in details or []:
        if isinstance(d, dict):
            passed = d.get("passed")
            name = d.get("name") or d.get("rule") or "?"
            value = d.get("value")
            threshold = d.get("threshold")
        else:
            passed = getattr(d, "passed", None)
            name = getattr(d, "name", getattr(d, "rule", "?"))
            value = getattr(d, "value", None)
            threshold = getattr(d, "threshold", None)
        if passed is False:
            out.append(f"{name}={value}(阈:{threshold})")
    return out


def _normalize_amount_percentage(value: Any) -> Optional[float]:
    return normalize_rate_fraction(_to_float(value))


class PositionRiskRunner:
    """
    Risk and exit runner.

    This runner now does two separate jobs:
    1. Non-price safety exits: dust, completed, holder/smart-money checks.
    2. Risk recheck: after entry, re-run the initial risk-threshold filter on a dynamic schedule:
       >=150 USD every 4s; >=100 every 8s; >=50 every 16s;
       >=25 every 32s; <25 every 64s.

    If a TradingPipeline is supplied, all exits are sent through execute_sell().
    Without a pipeline, SIM positions are paper-exited while LIVE positions are never falsely closed.
    """

    def __init__(self, repo: Repositories, gmgn, trading_pipeline=None):
        self.repo = repo
        self.gmgn = gmgn
        self.trading_pipeline = trading_pipeline
        self.exit_service = PositionExitService(repo, trading_pipeline=trading_pipeline, gmgn=gmgn)
        self._legacy_warned: set[int] = set()
        self._last_risk_fail_details: Dict[int, List[str]] = {}
        self._last_risk_fail_structured: Dict[int, List[Dict[str, Any]]] = {}
        self._last_scan: Dict[int, datetime] = {}
        self._consecutive_risk_failures: Dict[int, int] = {}
        self._risk_unavailable_counts: Dict[int, int] = {}

    def set_trading_pipeline(self, trading_pipeline):
        self.trading_pipeline = trading_pipeline
        self.exit_service.trading_pipeline = trading_pipeline

    async def _fetch_risk_data_with_retry(self, method_ref, preferred_slot, validate_func, endpoint="", retry_delay_seconds=0):
        """Call GMGN with retry across different slots.

        2+3 pattern: 2 attempts on preferred slot, then 3 fallback-slots.
        Raises RuntimeError after 5 total attempts.
        """
        rl = get_rate_limiter()
        feature_pool = settings.get_feature_slots()
        last_exc = None
        attempted: Set[int] = set()

        for attempt in range(5):
            if attempt > 0 and retry_delay_seconds > 0:
                await asyncio.sleep(retry_delay_seconds)

            slot = None
            # First 2 attempts prefer the preferred_slot
            if attempt < 2 and preferred_slot is not None and rl.is_slot_available(preferred_slot) and preferred_slot not in attempted:
                slot = preferred_slot
            else:
                for s in feature_pool or ():
                    if s not in attempted and rl.is_slot_available(s):
                        slot = s
                        break

            if slot is None:
                break

            attempted.add(slot)
            try:
                result = await method_ref(credential_slot=slot)
                if validate_func is None or validate_func(result):
                    return result
                last_exc = ValueError("Risk data validation failed")
                if endpoint:
                    await rl.report_response_anomaly(slot, endpoint, "validation_failed")
            except Exception as e:
                last_exc = e

        raise RuntimeError("Risk data unavailable after 5 attempts") from last_exc

    async def _emergency_risk_exit(self, position, reason="RISK_DATA_UNAVAILABLE_EXIT"):
        token = position["token_mint"]
        pos_id = int(position["id"])
        account_type = _account_type(position)
        await self.repo.append_system_event(
            "ERROR",
            "RISK",
            f"Emergency exit triggered for {token}: {reason}",
            _safe_json_dumps({"position_id": pos_id, "token": token, "reason": reason}),
            account_type=account_type,
        )
        await self._request_exit(
            position=position,
            exit_pct=1.0,
            reason_code=reason,
            emergency=True,
            latest={},
            current_price_usd=None,
        )

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
        self._last_scan[pos_id] = now

        if not _recheck_due(position, now):
            return

        stored_remaining_usd = _to_float(position.get("remaining_value_usd"), 0.0) or 0.0
        prefetch_interval = risk_scan_interval_seconds(stored_remaining_usd)

        try:
            latest = await self._fetch_latest_price(token, account_type, retry_delay_seconds=prefetch_interval)
        except RuntimeError:
            count = self._risk_unavailable_counts.get(pos_id, 0) + 1
            self._risk_unavailable_counts[pos_id] = count
            threshold = getattr(settings, "RISK_DATA_UNAVAILABLE_THRESHOLD", 3)
            await self.repo.append_system_event(
                "WARN",
                "RISK",
                f"Latest price fetch failed for {token} ({count}/{threshold})",
                _safe_json_dumps({"position_id": pos_id, "count": count, "threshold": threshold}),
                account_type=account_type,
            )
            if count >= threshold:
                await self._emergency_risk_exit(position)
            return
        self._risk_unavailable_counts.pop(pos_id, None)
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

        try:
            latest_snapshot = await self._fetch_latest_snapshot(token, account_type, retry_delay_seconds=interval)
        except RuntimeError:
            count = self._risk_unavailable_counts.get(pos_id, 0) + 1
            self._risk_unavailable_counts[pos_id] = count
            threshold = getattr(settings, "RISK_DATA_UNAVAILABLE_THRESHOLD", 3)
            await self.repo.append_system_event(
                "WARN",
                "RISK",
                f"Risk snapshot fetch failed for {token} ({count}/{threshold})",
                _safe_json_dumps({"position_id": pos_id, "count": count, "threshold": threshold}),
                account_type=account_type,
            )
            if count >= threshold:
                await self._emergency_risk_exit(position, reason="RISK_DATA_UNAVAILABLE_EXIT")
            return

        classification = self._classify_snapshot(latest_snapshot)
        if classification == "unavailable":
            count = self._risk_unavailable_counts.get(pos_id, 0) + 1
            self._risk_unavailable_counts[pos_id] = count
            threshold = getattr(settings, "RISK_DATA_UNAVAILABLE_THRESHOLD", 3)
            await self.repo.append_system_event(
                "WARN",
                "RISK",
                f"Risk snapshot unavailable for {token} ({count}/{threshold})",
                _safe_json_dumps({"position_id": pos_id, "count": count, "threshold": threshold}),
                account_type=account_type,
            )
            if count >= threshold:
                await self._emergency_risk_exit(position, reason="RISK_DATA_UNAVAILABLE_EXIT")
            return

        token_info = await self.repo.get_token(token)
        if token_info and token_info.get("latest_type") and "type" not in latest_snapshot:
            latest_snapshot["type"] = token_info["latest_type"]

        if classification == "partial":
            missing_fields = [k for k, aliases in self.RISK_REQUIRED_ALIASES.items()
                              if not any(a in latest_snapshot for a in aliases)]
            await self.repo.append_system_event(
                "WARN",
                "RISK",
                f"Holding risk snapshot partial for {token}: missing {missing_fields}",
                _safe_json_dumps({"position_id": pos_id, "missing_fields": missing_fields}),
                account_type=account_type,
            )
            # Partial snapshot ⇒ skip risk recheck this cycle, do NOT force exit.
            return

        # Snapshot is complete: reset consecutive unavailable counter
        self._risk_unavailable_counts.pop(pos_id, None)

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
            risk_details = self._last_risk_fail_details.get(pos_id, [])
            await self._request_exit(
                position=position_for_decision,
                exit_pct=1.0,
                reason_code="RISK_RECHECK_FAILED",
                emergency=True,
                latest=latest,
                current_price_usd=price_usd,
                risk_details=risk_details,
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

        # Dust force exit check
        remaining_token = _to_float(position.get("remaining_token_amount"), 0.0) or 0.0
        dust_threshold = float(getattr(settings, "DUST_FORCE_EXIT_USD", 12.5))
        if (
            remaining_value_usd is not None
            and remaining_value_usd < dust_threshold
            and price_usd is not None
            and remaining_token > 0
        ):
            if hasattr(self.repo, "insert_position_audit"):
                await self.repo.insert_position_audit(
                    position_id=pos_id,
                    token_mint=token,
                    account_type=account_type,
                    strategy_id=_position_strategy_id(position),
                    discovery_event_id=position.get("discovery_event_id"),
                    audit_type="DECISION",
                    audit_json=_safe_json_dumps({
                        "reason": "DUST_FORCE_EXIT",
                        "remaining_value_usd_before": remaining_value_usd,
                        "dust_threshold": dust_threshold,
                        "current_price_usd": price_usd,
                        "remaining_token_amount_before": remaining_token,
                    }),
                )
            await self._request_exit(
                position=position_for_decision,
                exit_pct=1.0,
                reason_code="DUST_FORCE_EXIT",
                emergency=True,
                latest=latest,
                current_price_usd=price_usd,
            )
            return

        # NOTE: Price-based exit rules (HARD_TP, HARD_SL) are handled exclusively
        # by ActivePositionPriceRunner to avoid duplicate triggers.
        # PositionRiskRunner covers only holding risk, completed, and dust force exit.

    async def _fetch_latest_price(self, token: str, account_type: str, retry_delay_seconds: float = 0) -> Dict[str, Any]:
        preferred = acquire_holding_slot("risk_price")

        async def _do_fetch(*, credential_slot):
            return await self.gmgn.fetch_latest_price(token, credential_slot=credential_slot)

        try:
            return await self._fetch_risk_data_with_retry(_do_fetch, preferred, None, retry_delay_seconds=retry_delay_seconds)
        except Exception as e:
            await self.repo.append_system_event(
                "ERROR",
                "RISK",
                f"GMGN latest price failed for {token}",
                _safe_json_dumps({"error": str(e)}),
                account_type=account_type,
            )
            raise

    RISK_REQUIRED_ALIASES = {
        "liquidity_usd": ("liquidity_usd", "liquidity", "pool_liquidity_usd"),
        "holder_count": ("holder_count", "holders", "total_holders", "holder"),
        "top10_holder_rate": ("top_10_holder_rate", "top10_holder_rate", "top10_holder_percent", "top_10_rate"),
        "fresh_wallet_rate": ("fresh_wallet_rate", "fresh_wallets_rate", "fresh_wallet"),
        "dev_team_hold_rate": ("dev_team_hold_rate", "creator_balance_rate", "creator_hold_rate", "dev_hold_rate"),
        "rug_ratio": ("max_rug_ratio", "rug_ratio", "max_rugged_ratio", "rug"),
        "entrapment_ratio": ("max_entrapment_ratio", "entrapment_ratio", "entrapment"),
        "insider_ratio": ("max_insider_ratio", "insider_ratio", "insider_rate"),
        "max_bundler_rate": ("max_bundler_rate", "bundler_trader_amount_rate", "bundler_rate", "bundler"),
        "suspected_insider_hold_rate": ("suspected_insider_hold_rate", "insider_hold_rate"),
        "is_wash_trading": ("is_wash_trading", "wash_trading", "wash_trading_detected", "is_wash"),
        "rat_trader_amount_rate": ("rat_trader_amount_rate", "rat_trader_rate", "rat_trader"),
        "sniper_count": ("sniper_count", "snipers", "sniper_trader_count", "sniper_cnt"),
    }

    def _classify_snapshot(self, snapshot: Dict[str, Any]) -> str:
        """Classify snapshot as ``complete``, ``partial``, or ``unavailable``.

        complete  — every risk semantic group (via aliases) has at least one key.
        partial   — snapshot has data but one or more groups are missing.
        unavailable — empty / error dict (nothing usable).
        """
        if not isinstance(snapshot, dict) or not snapshot:
            return "unavailable"
        if "error" in snapshot:
            return "unavailable"
        all_present = all(
            any(k in snapshot for k in aliases)
            for aliases in self.RISK_REQUIRED_ALIASES.values()
        )
        return "complete" if all_present else "partial"

    def _validate_snapshot(self, result) -> bool:
        """Minimal validation: only check that we got a non-error dict.

        The three-way classification happens after fetch, so retry only
        triggers on complete API failure, not on partial data.
        """
        return isinstance(result, dict) and bool(result) and "error" not in result

    async def _fetch_latest_snapshot(self, token: str, account_type: str, retry_delay_seconds: float = 0) -> Dict[str, Any]:
        preferred = acquire_holding_slot("risk_snapshot")
        endpoint = getattr(settings, "GMGN_TOKEN_SNAPSHOT_PATH", "/v1/token/security")

        async def _do_fetch(*, credential_slot):
            snap = await self.gmgn.fetch_token_snapshot(token, credential_slot=credential_slot)
            return snap or {}

        try:
            return await self._fetch_risk_data_with_retry(_do_fetch, preferred, self._validate_snapshot, endpoint=endpoint, retry_delay_seconds=retry_delay_seconds)
        except RuntimeError:
            await self.repo.append_system_event(
                "ERROR",
                "RISK",
                f"GMGN token snapshot failed for {token} (4 attempts exhausted)",
                _safe_json_dumps({"error": "RiskDataUnavailableError"}),
                account_type=account_type,
            )
            raise

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

        # Inject top1_addr_type0_rate from token_top_holders (best-effort, don't fail on error)
        try:
            holders = await self.gmgn.fetch_top_holders(token, limit=5)
            if holders:
                top1_rate = None
                for h in holders:
                    at = h.get("addr_type", 0)
                    if at is not None and int(at) == 0:
                        rate = _to_float(h.get("top1_holder_rate") or h.get("rate") or h.get("amount_percentage"))
                        if rate is not None and (top1_rate is None or rate > top1_rate):
                            top1_rate = rate
                if top1_rate is not None:
                    snapshot["top1_addr_type0_rate"] = top1_rate
        except Exception as exc:
            logger.warning(f"top1_holders unavailable for {token}: {exc}")

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
            self._last_risk_fail_details.pop(pos_id, None)
            self._last_risk_fail_structured.pop(pos_id, None)
            return True

        fail_summary = _risk_detail_summary(getattr(res, "details", []))
        self._last_risk_fail_details[pos_id] = fail_summary
        structured_fails = []
        for d in getattr(res, "details", []):
            dd = getattr(d, "__dict__", d) if not isinstance(d, dict) else d
            structured_fails.append({
                "rule": dd.get("name") or dd.get("rule", "?"),
                "label": dd.get("label", ""),
                "value": dd.get("value"),
                "threshold": dd.get("threshold"),
                "passed": dd.get("passed", False),
                "reason": dd.get("reason", ""),
            })
        self._last_risk_fail_structured[pos_id] = structured_fails

        await self.repo.append_system_event(
            "WARN",
            "RISK",
            "Risk recheck failed; requesting full exit",
            _safe_json_dumps(
                {
                    "position_id": pos_id,
                    "token": token,
                    "failed_rules": fail_summary,
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
                "message": f"Risk recheck failed for {token}: {', '.join(fail_summary)[:160]}",
            },
        )
        return False

    async def _request_exit(
        self,
        position: Dict[str, Any],
        exit_pct: float,
        reason_code: str,
        emergency: bool,
        latest: Dict[str, Any],
        current_price_usd: Optional[float],
        risk_details: Optional[List[str]] = None,
    ):
        pos_id = int(position["id"])
        token = position["token_mint"]
        account_type = _account_type(position)

        remaining_value_usd = _to_float(position.get("remaining_value_usd"), 0.0) or 0.0
        dust_threshold = float(getattr(settings, "DUST_FORCE_EXIT_USD", 12.5))
        if remaining_value_usd < dust_threshold:
            exit_pct = 1.0
            if reason_code != "DUST_FORCE_EXIT":
                reason_code = "DUST_FORCE_EXIT"

        audit_context: Dict[str, Any] = {}
        if reason_code == "RISK_RECHECK_FAILED":
            risk_failed_rules = self._last_risk_fail_structured.get(pos_id, [])
            if risk_failed_rules:
                audit_context["risk_failed_rules"] = risk_failed_rules

        # One-shot rule idempotency for partial exits
        if exit_pct < 1.0 and hasattr(self.repo, "has_exit_rule_executed"):
            if await self.repo.has_exit_rule_executed(pos_id, reason_code):
                return

        # Delegate to unified exit service
        result = await self.exit_service.exit_position(
            position=position,
            exit_pct=exit_pct,
            reason_code=reason_code,
            current_price_usd=current_price_usd,
            emergency=emergency,
            source="RISK_RUNNER",
            risk_details=risk_details,
        )

        # Post-exit audit for risk-specific context
        if result.get("ok") and audit_context and hasattr(self.repo, "insert_position_audit"):
            from ..trading.audit_builder import build_exit_audit_payload
            try:
                trade_events = await self.repo.get_position_trade_events(pos_id, limit=1)
                te = trade_events[0] if trade_events else None
                if te:
                    sell_amount = min(
                        _to_float(position.get("remaining_token_amount"), 0.0) or 0.0,
                        (_to_float(position.get("remaining_token_amount"), 0.0) or 0.0) * exit_pct,
                    )
                    gross_value = sell_amount * (current_price_usd or 0.0)
                    exit_audit = await build_exit_audit_payload(
                        repo=self.repo,
                        position=position,
                        sell_trade_event=te,
                        exit_reason=reason_code,
                        exit_pct=exit_pct,
                        sell_amount_human=sell_amount,
                        gross_value_usd=gross_value,
                        current_price_usd=current_price_usd,
                        quote=None,
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
                        audit_json=exit_audit,
                    )
            except Exception:
                logger.warning("Risk audit insert failed", position_id=pos_id, reason=reason_code)
