from __future__ import annotations

import time
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .client import GMGNDataClient
from .models import ApiKeyRoles


SIGNAL_TYPES: dict[int, str] = {
    2: "dex_ad",
    4: "dex_trending",
    5: "dex_boost",
    12: "smart_money_buy",
    14: "large_buy",
    15: "multi_buy",
    16: "multi_large_buy",
    20: "kol_buy",
}


def _mappings(value: Any, depth: int = 0) -> list[Mapping[str, Any]]:
    if depth > 8:
        return []
    if isinstance(value, Mapping):
        result: list[Mapping[str, Any]] = [value]
        for nested in value.values():
            result.extend(_mappings(nested, depth + 1))
        return result
    if isinstance(value, list):
        result = []
        for nested in value:
            result.extend(_mappings(nested, depth + 1))
        return result
    return []


def _number(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        return parsed
    return None


def _timestamp(row: Mapping[str, Any]) -> int | None:
    for key in ("trigger_at", "trigger_time", "timestamp", "time", "created_at"):
        try:
            value = int(float(row.get(key)))
        except (TypeError, ValueError):
            continue
        return value // 1000 if value > 10_000_000_000 else value
    return None


def _rank_rows(payload: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not payload:
        return []
    return [
        row
        for row in _mappings(payload)
        if any(row.get(key) for key in ("address", "token_address", "token_mint"))
        and any(key in row for key in ("volume", "volume_1m", "swaps", "swaps_1m", "visiting_count"))
    ]


class GMGNMarketRegimeProvider:
    """Low-frequency aggregate attention/event feed using the existing GMGN pool.

    Optional endpoint failures are isolated: the caller gets partial features and
    a short error label, never a fabricated numeric zero for an unavailable feed.
    """

    def __init__(self, client: GMGNDataClient, roles: ApiKeyRoles) -> None:
        self.client = client
        self.roles = roles
        self._index = 0

    def _slot(self):
        slots = self.roles.realtime
        slot = slots[self._index % len(slots)]
        self._index += 1
        return slot

    async def _call_variants(
        self,
        path: str,
        variants: Sequence[tuple[str, Mapping[str, Any] | None, Mapping[str, Any] | None]],
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        last_error: str | None = None
        for method, params, body in variants:
            try:
                return await self.client.request(
                    self._slot(), path, method=method, params=params, json_body=body
                ), None
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                code = getattr(exc, "code", None)
                suffix = ":".join(str(item) for item in (status, code) if item not in (None, ""))
                last_error = f"{type(exc).__name__}:{path}" + (f":{suffix}" if suffix else "")
        return None, last_error

    async def snapshot(self, *, now_ts: int | None = None) -> dict[str, Any]:
        observed_at = int(now_ts or time.time())
        trending, trend_error = await self._call_variants(
            self.client.endpoints.trending,
            (("GET", {"chain": "sol", "interval": "1m", "limit": 50}, None),),
        )
        hot, hot_error = await self._call_variants(
            self.client.endpoints.hot_searches,
            ((
                "POST",
                None,
                {"params": [{"label": "hot-search", "chain": "sol", "interval": "1m", "limit": 100}]},
            ),),
        )
        signals, signal_error = await self._call_variants(
            self.client.endpoints.signal,
            (("POST", None, {"chain": "sol", "groups": [{}]}),),
        )

        trend_rows = _rank_rows(trending)
        hot_rows = _rank_rows(hot)
        buy_count = sell_count = buy_volume = sell_volume = 0.0
        count_rows = 0
        for row in trend_rows:
            buys = _number(row, "buys_1m", "buys", "buy_count")
            sells = _number(row, "sells_1m", "sells", "sell_count")
            bv = _number(row, "buy_volume_1m", "buy_volume")
            sv = _number(row, "sell_volume_1m", "sell_volume")
            if buys is not None and sells is not None:
                buy_count += buys
                sell_count += sells
                count_rows += 1
            if bv is not None:
                buy_volume += bv
            if sv is not None:
                sell_volume += sv
        count_total = buy_count + sell_count
        volume_total = buy_volume + sell_volume
        hot_visits = sum(max(0.0, _number(row, "visiting_count", "visitingCount") or 0.0) for row in hot_rows)

        counts: Counter[str] = Counter()
        cutoff = observed_at - 15 * 60
        for row in _mappings(signals or {}):
            raw = row.get("signal_type", row.get("signalType", row.get("type")))
            try:
                signal_type = int(raw)
            except (TypeError, ValueError):
                continue
            label = SIGNAL_TYPES.get(signal_type)
            if label is None:
                continue
            stamp = _timestamp(row)
            if stamp is not None and stamp < cutoff:
                continue
            counts[label] += 1

        errors = [item for item in (trend_error, hot_error, signal_error) if item]
        signal_values = {
            f"signal_{name}_15m": (counts.get(name, 0) if signals is not None else None)
            for name in SIGNAL_TYPES.values()
        }
        return {
            "observed_at": observed_at,
            "available": any(payload is not None for payload in (trending, hot, signals)),
            "trending_count_1m": len(trend_rows) if trending is not None else None,
            "hot_search_count_1m": len(hot_rows) if hot is not None else None,
            "hot_search_visits_1m": hot_visits if hot is not None else None,
            "market_buy_count_imbalance_1m": (
                (buy_count - sell_count) / count_total if count_total > 0 else None
            ),
            "market_buy_volume_imbalance_1m": (
                (buy_volume - sell_volume) / volume_total if volume_total > 0 else None
            ),
            "market_activity_rows": count_rows if trending is not None else None,
            **signal_values,
            "errors": errors,
        }
