"""Entry data gate — blocking NULL / abnormal fields before buy.

BUY MUST NOT proceed unless all hard-required fields are present and valid.
This module provides both a static check and a retry wrapper that should be
called from the executor right before creating a position.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from ..logging_config import logger
from ..config import settings

ENTRY_HARD_REQUIRED_FIELDS: Set[str] = {
    "price_usd",
    "liquidity_usd",
    "market_cap",
    "holder_count",
    "top_10_holder_rate",
    "fresh_wallet_rate",
    "max_rug_ratio",
    "max_entrapment_ratio",
    "max_bundler_rate",
    "suspected_insider_hold_rate",
    "is_wash_trading",
    "rat_trader_amount_rate",
    "sell_tax",
    "burn_status",
    "sniper_count",
    "creator_balance_rate",
    "swaps_1h",
    "volume_1h",
    "price_change_percent1h",
    "socials",
}

# Fields that must be > 0 (not just present)
POSITIVE_REQUIRED = {
    "price_usd", "liquidity_usd", "holder_count",
    "market_cap", "swaps_1h", "volume_1h",
}


def _is_present(value: Any) -> bool:
    """True if value is not None / "" / [] / {}."""
    if value is None:
        return False
    if isinstance(value, str) and value.strip() == "":
        return False
    if isinstance(value, (list, dict)) and len(value) == 0:
        return False
    return True


def _is_positive(value: Any) -> bool:
    """True if value is a finite number > 0."""
    try:
        v = float(value)
        return v > 0
    except (TypeError, ValueError):
        return False


@dataclass
class EntryDataCompletenessReport:
    passed: bool = True
    missing_fields: List[str] = field(default_factory=list)
    abnormal_fields: List[str] = field(default_factory=list)
    details: Dict[str, str] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return len(self.missing_fields) > 0 or len(self.abnormal_fields) > 0


def check_entry_data_completeness(snapshot: Dict[str, Any]) -> EntryDataCompletenessReport:
    """Validate a snapshot dict for entry readiness.

    Returns a report.  If ``report.blocked`` is True, the executor MUST NOT
    proceed with the buy.
    """
    report = EntryDataCompletenessReport()

    for field in sorted(ENTRY_HARD_REQUIRED_FIELDS):
        value = snapshot.get(field)
        if not _is_present(value):
            report.missing_fields.append(field)
            report.details[field] = "MISSING"
            report.passed = False
            continue

        if field in POSITIVE_REQUIRED and not _is_positive(value):
            report.abnormal_fields.append(field)
            report.details[field] = f"ABNORMAL (value={value})"
            report.passed = False
            continue

        report.details[field] = "OK"

    # Additional checks
    if "creator_balance_rate" in report.details and report.details["creator_balance_rate"] == "OK":
        rate = snapshot.get("creator_balance_rate")
        if rate is not None and not isinstance(rate, (int, float)):
            report.abnormal_fields.append("creator_balance_rate")
            report.details["creator_balance_rate"] = f"ABNORMAL (non-numeric: {rate})"
            report.passed = False

    if "socials" in report.details and report.details["socials"] == "OK":
        val = snapshot.get("socials")
        if isinstance(val, (list, dict)) and len(val) == 0:
            report.missing_fields.append("socials")
            report.details["socials"] = "MISSING (empty)"
            report.passed = False

    return report


async def retry_fetch_complete_snapshot(
    gmgn,
    token_mint: str,
    *,
    same_slot_retries: int = 2,
    retry_sleep: float = 4.0,
    fallback_slots: int = 4,
) -> tuple[Dict[str, Any], EntryDataCompletenessReport]:
    """Attempt to get a complete snapshot for a token.

    Strategy:
    1. Primary call with current slot.
    2. Retry same slot up to ``same_slot_retries`` times.
    3. Rotate through up to ``fallback_slots`` other slots.
    4. If any call returns a complete snapshot, return it immediately.
    5. Otherwise return the best (partial) snapshot + report.

    Returns (snapshot_dict, report).
    """
    import asyncio
    from ..runners.position_risk_runner import acquire_holding_slot

    preferred_slot = acquire_holding_slot("entry_snapshot")
    slots_to_try: List[Optional[int]] = [preferred_slot]
    if fallback_slots > 0:
        feature_slots = sorted(settings.get_feature_slots())  # type: ignore[name-defined] # noqa
        for s in feature_slots:
            if s != preferred_slot and s not in slots_to_try:
                slots_to_try.append(s)
                if len(slots_to_try) >= 1 + fallback_slots:
                    break
    slots_to_try = slots_to_try[: int(1 + fallback_slots)]

    best_snapshot: Dict[str, Any] = {}
    best_report: Optional[EntryDataCompletenessReport] = None

    for slot_idx, slot in enumerate(slots_to_try):
        for attempt in range(1 + same_slot_retries):
            if attempt > 0:
                await asyncio.sleep(retry_sleep)
            try:
                snap = await gmgn.fetch_token_snapshot(token_mint, credential_slot=slot)
                snap = snap or {}
            except Exception as exc:
                logger.warning(f"entry_data_gate: fetch failed slot={slot} attempt={attempt}: {exc}")
                continue

            report = check_entry_data_completeness(snap)
            if not report.blocked:
                logger.info(f"entry_data_gate: complete snapshot for {token_mint} (slot={slot}, attempt={attempt})")
                return snap, report

            if best_report is None or len(report.missing_fields) + len(report.abnormal_fields) < \
               len(best_report.missing_fields) + len(best_report.abnormal_fields):
                best_snapshot = snap
                best_report = report

    if best_report is not None:
        logger.warning(
            f"entry_data_gate: all retries exhausted for {token_mint}; "
            f"missing={best_report.missing_fields} abnormal={best_report.abnormal_fields}"
        )
    else:
        best_report = EntryDataCompletenessReport(
            passed=False,
            missing_fields=list(ENTRY_HARD_REQUIRED_FIELDS),
            details={f: "UNREACHABLE" for f in ENTRY_HARD_REQUIRED_FIELDS},
        )

    return best_snapshot, best_report
