from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..db.repositories import Repositories
from ..services.price_aggregator import PriceAggregator
from ..services.event_bus import event_bus
from ..config import settings
from ..logging_config import logger


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _provider_mode_is_mock() -> bool:
    return "MOCK" in str(getattr(settings, "PROVIDER_MODE", "") or "").upper()


def _extract_price_usd(result: Dict[str, Any]) -> Optional[float]:
    for key in ("price_usd", "latest_price_usd"):
        value = _to_float(result.get(key))
        if value and value > 0:
            return value

    # Historical PriceAggregator used "price" as USD price.
    value = _to_float(result.get("price"))
    if value and value > 0:
        return value

    return None


def _extract_price_sol(result: Dict[str, Any]) -> Optional[float]:
    for key in ("price_sol", "latest_price_sol", "current_price_sol"):
        value = _to_float(result.get(key))
        if value and value > 0:
            return value

    # Compatibility fallback only for old mock providers.
    if _provider_mode_is_mock():
        value = _to_float(result.get("price"))
        if value and value > 0:
            return value

    return None


class PriceMonitorRunner:
    """
    Writes one tick per open token per run.

    The old version looped through positions and could write duplicate ticks when
    multiple positions held the same token. It also passed result["price"] as USD
    while some downstream code treated it as SOL. This version separates price_usd
    and price_sol explicitly.
    """

    def __init__(self, repo: Repositories, price_aggregator: PriceAggregator):
        self.repo = repo
        self.aggregator = price_aggregator

    async def run_once(self):
        positions = await self.repo.list_open_positions()
        if not positions:
            return

        # De-duplicate by token so one token with multiple SIM/LIVE positions does not
        # create multiple identical ticks in the same monitor cycle.
        token_to_account: Dict[str, str] = {}
        for p in positions:
            token = p.get("token_mint")
            if not token:
                continue
            account_type = p.get("account_type") or ("LIVE" if p.get("is_live") else "SIM")
            token_to_account.setdefault(token, account_type)

        now = _utc_now_iso()

        for token, account_type in token_to_account.items():
            try:
                result = await self.aggregator.get_price(token)
                if not result:
                    continue

                observed_at = result.get("observed_at") or now
                source = result.get("source") or "UNKNOWN"

                price_usd = _extract_price_usd(result)
                price_sol = _extract_price_sol(result)
                liquidity_usd = _to_float(result.get("liquidity_usd") or result.get("latest_liquidity_usd"))
                sol_side_liquidity = _to_float(
                    result.get("sol_side_liquidity")
                    or result.get("latest_sol_side_liquidity")
                )
                market_cap = _to_float(result.get("market_cap") or result.get("latest_market_cap"))

                # Do not write completely unusable ticks.
                if price_usd is None and price_sol is None:
                    await self.repo.append_system_event(
                        "WARN",
                        "PRICE",
                        f"PriceMonitorRunner skipped unusable tick for {token}",
                        str({"result": result}),
                        account_type=account_type,
                    )
                    continue

                # Some PriceAggregator implementations may already persist the tick.
                # If they explicitly tell us so, avoid double insertion.
                if not result.get("tick_persisted"):
                    await self.repo.insert_tick_snapshot(
                        token,
                        source,
                        observed_at,
                        price_usd,
                        price_sol,
                        liquidity_usd,
                        sol_side_liquidity,
                        market_cap,
                        str(result),
                    )

                if hasattr(self.repo, "update_token_latest_snapshot"):
                    await self.repo.update_token_latest_snapshot(
                        token_mint=token,
                        latest_price_usd=price_usd,
                        latest_price_sol=price_sol,
                        latest_liquidity_usd=liquidity_usd,
                        latest_sol_side_liquidity=sol_side_liquidity,
                        latest_market_cap=market_cap,
                    )

            except Exception as e:
                logger.exception("PriceMonitorRunner failed", token=token, error=str(e))
                await self.repo.append_system_event(
                    "ERROR",
                    "PRICE",
                    f"PriceMonitorRunner failed for {token}",
                    str({"error": str(e)}),
                    account_type=account_type,
                )
                await event_bus.publish(
                    "system",
                    {
                        "level": "ERROR",
                        "category": "PRICE",
                        "message": f"Tick failed for {token}",
                    },
                )
