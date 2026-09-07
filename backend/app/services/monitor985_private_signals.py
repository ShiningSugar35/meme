from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .browser_session import BrowserCredentialSnapshot, read_local_storage
from .public_social_signals import _event_items, _timestamp


MONITOR985_ORIGIN = "https://www.985monitor.xyz"
_MONITOR985_STORAGE_ORIGINS = ("https://985monitor.xyz", MONITOR985_ORIGIN)
_MONITOR985_AUTH_KEYS = (
    "xMonitorWalletAddress",
    "xMonitorWalletToken",
    "xMonitorFomoMutedV1",
    "xMonitorFomoPrefsV1",
    "xMonitorPumpMutedV1",
    "xMonitorPumpPrefsV1",
    "xMonitorPumpOnlyMineV1",
)
_PRIVATE_FEATURES = (
    "ln(monitor_private_fomo_events_15m+1)",
    "ln(monitor_private_fomo_unique_authors_15m+1)",
    "monitor_private_fomo_buy_ratio_15m",
    "monitor_private_fomo_usd_imbalance_15m",
    "ln(monitor_private_fomo_usd_15m+1)",
    "monitor_private_source_coverage",
)


@dataclass(frozen=True, slots=True)
class Monitor985PrivateSnapshot:
    observed_at: int
    fetched_at: int
    features: Mapping[str, float | None]
    connected: bool
    successful_sources: tuple[str, ...]
    incomplete_sources: tuple[str, ...]
    failed_sources: tuple[str, ...]
    matched_events: int


def _missing_features() -> dict[str, None]:
    return {name: None for name in _PRIVATE_FEATURES}


def _safe_reason(prefix: str, exc: BaseException) -> str:
    return f"{prefix}:{type(exc).__name__}"


def _json_value(raw: str | None, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    try:
        parsed = json.loads(str(raw))
        return fallback if parsed is None else parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _prefs(values: Mapping[str, str]) -> dict[str, Any]:
    fomo_muted = _json_value(values.get("xMonitorFomoMutedV1"), [])
    fomo_prefs = _json_value(values.get("xMonitorFomoPrefsV1"), {})
    pump_muted = _json_value(values.get("xMonitorPumpMutedV1"), [])
    pump_prefs = _json_value(values.get("xMonitorPumpPrefsV1"), {})
    if not isinstance(fomo_muted, list):
        fomo_muted = []
    if not isinstance(fomo_prefs, Mapping):
        fomo_prefs = {}
    if not isinstance(pump_muted, list):
        pump_muted = []
    if not isinstance(pump_prefs, Mapping):
        pump_prefs = {}
    only_mine = str(values.get("xMonitorPumpOnlyMineV1") or "").strip().lower() != "false"
    return {
        "fomo": {"muted": fomo_muted, "prefs": dict(fomo_prefs)},
        "pump": {"muted": pump_muted, "prefs": dict(pump_prefs), "onlyMine": only_mine},
    }


def _event_time_ms(event: Mapping[str, Any]) -> int | None:
    # 985monitor's extension contract uses millisecond timestamps for raw.ts and
    # ISO timestamps for createdAt/tradeTime. Normalize to Unix seconds.
    for raw in (
        event.get("ts"),
        event.get("createdAt"),
        ((event.get("content") or {}).get("pumpTrade") or {}).get("tradeTime")
        if isinstance(event.get("content"), Mapping)
        else None,
    ):
        parsed = _timestamp(raw)
        if parsed is not None:
            return parsed
    return None


def _solana_chain(event: Mapping[str, Any], *, pump: bool) -> bool:
    if pump:
        content = event.get("content")
        trade = content.get("pumpTrade") if isinstance(content, Mapping) else None
        trade = trade if isinstance(trade, Mapping) else {}
        direct = str(trade.get("chainSlug") or trade.get("chain") or "").strip().lower()
        name = str(trade.get("chainName") or "").strip().lower()
        chain_id = str(trade.get("chainId") or "").strip()
        return direct in {"sol", "solana"} or name in {"sol", "solana"} or chain_id == "1399811149"
    name = str(event.get("chainName") or event.get("chain") or "").strip().lower()
    return name in {"sol", "solana"}


def _address(event: Mapping[str, Any], *, pump: bool) -> str:
    if pump:
        content = event.get("content")
        trade = content.get("pumpTrade") if isinstance(content, Mapping) else None
        trade = trade if isinstance(trade, Mapping) else {}
        return str(trade.get("mint") or trade.get("tokenAddress") or trade.get("contractAddress") or "").strip()
    return str(event.get("tokenAddress") or event.get("address") or event.get("mint") or "").strip()


def _side(event: Mapping[str, Any], *, pump: bool) -> str:
    if pump:
        content = event.get("content")
        trade = content.get("pumpTrade") if isinstance(content, Mapping) else None
        trade = trade if isinstance(trade, Mapping) else {}
        side = str(trade.get("side") or "").strip().lower()
    else:
        kind = str(event.get("eventType") or "").strip().upper()
        side = {"FOMO_BUY": "buy", "FOMO_SELL": "sell"}.get(kind, "")
    return side if side in {"buy", "sell"} else ""


def _usd(event: Mapping[str, Any], *, pump: bool) -> float | None:
    if pump:
        content = event.get("content")
        trade = content.get("pumpTrade") if isinstance(content, Mapping) else None
        trade = trade if isinstance(trade, Mapping) else {}
        raw = trade.get("amountUsd")
    else:
        raw = event.get("usd")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _principal(event: Mapping[str, Any], *, pump: bool) -> str:
    if pump:
        content = event.get("content")
        trade = content.get("pumpTrade") if isinstance(content, Mapping) else None
        trade = trade if isinstance(trade, Mapping) else {}
        value = trade.get("wallet") or trade.get("username") or trade.get("watchName") or trade.get("walletName")
    else:
        value = event.get("handle") or event.get("userName")
    return str(value or "").strip().lower()


def _window_complete(events: Sequence[Mapping[str, Any]], *, entry_time: int, seconds: int, row_limit: int = 150) -> bool:
    if len(events) < int(row_limit):
        return True
    timestamps = [
        timestamp
        for event in events
        if (timestamp := _event_time_ms(event)) is not None and timestamp <= int(entry_time)
    ]
    return bool(timestamps) and min(timestamps) <= int(entry_time) - int(seconds)


def _imbalance(buy: float, sell: float) -> float | None:
    total = float(buy) + float(sell)
    return (float(buy) - float(sell)) / total if total > 0 else None


def _log1p(value: float | int) -> float:
    return math.log1p(max(0.0, float(value)))


class Monitor985PrivateSignalProvider:
    """Optional PIT features from 985monitor's account-scoped read-only feeds.

    The browser wallet token is read only from an explicitly allowlisted
    same-origin localStorage key and used solely to exchange for the site's
    read-only extension session. Neither token is persisted by this service.
    """

    def __init__(
        self,
        *,
        base_url: str = MONITOR985_ORIGIN,
        cache_seconds: float = 15.0,
        auth_probe_seconds: float = 60.0,
        timeout_seconds: float = 10.0,
        retry_delay_seconds: float = 0.20,
        client: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_seconds = max(5.0, float(cache_seconds))
        self.auth_probe_seconds = max(15.0, float(auth_probe_seconds))
        self.timeout_seconds = max(2.0, float(timeout_seconds))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._client_id = f"meme-quant-{uuid.uuid4()}"
        self._session_token: str | None = None
        self._session_expires_at_ms = 0
        self._auth_checked_mono = 0.0
        self._auth_values: dict[str, str] = {}
        self._cache_mono = 0.0
        self._cache: dict[str, list[Mapping[str, Any]]] = {}
        self._failures: tuple[str, ...] = ()
        self._fetched_at = 0

    async def _http_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers={"User-Agent": "meme-quant-monitor985-readonly/1.0"},
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        self._session_token = None
        self._auth_values.clear()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _browser_auth(self, *, force: bool = False) -> BrowserCredentialSnapshot:
        now = time.monotonic()
        if not force and self._auth_checked_mono > 0 and now - self._auth_checked_mono < self.auth_probe_seconds:
            return BrowserCredentialSnapshot(None, dict(self._auth_values), "cached_local_storage")
        errors: list[str] = []
        for origin in _MONITOR985_STORAGE_ORIGINS:
            try:
                snapshot = await asyncio.to_thread(read_local_storage, origin, _MONITOR985_AUTH_KEYS)
            except Exception as exc:
                errors.append(_safe_reason("browser_storage", exc))
                continue
            errors.extend(snapshot.errors)
            wallet = str(snapshot.values.get("xMonitorWalletAddress") or "").strip()
            token = str(snapshot.values.get("xMonitorWalletToken") or "").strip()
            if wallet and token:
                self._auth_values = dict(snapshot.values)
                self._auth_checked_mono = now
                return BrowserCredentialSnapshot(
                    snapshot.profile,
                    dict(snapshot.values),
                    snapshot.source,
                    snapshot.unsupported_app_bound,
                    tuple(errors),
                )
        self._auth_values.clear()
        self._auth_checked_mono = now
        return BrowserCredentialSnapshot(None, {}, "chromium_local_storage", errors=tuple(errors))

    async def _session(self) -> str | None:
        now_ms = int(time.time() * 1000)
        if self._session_token and self._session_expires_at_ms > now_ms + 60_000:
            return self._session_token
        auth = await self._browser_auth(force=bool(self._session_token))
        wallet = str(auth.values.get("xMonitorWalletAddress") or "").strip()
        token = str(auth.values.get("xMonitorWalletToken") or "").strip()
        if not wallet or not token:
            self._session_token = None
            self._session_expires_at_ms = 0
            return None
        client = await self._http_client()
        for attempt in range(2):
            try:
                response = await client.post(
                    f"{self.base_url}/api/extension/session",
                    headers={
                        "Content-Type": "application/json",
                        "X-User-Id": wallet,
                        "X-User-Token": token,
                        "X-Wallet-Address": wallet,
                    },
                    json={"clientId": self._client_id, "prefs": _prefs(auth.values)},
                )
                status = int(response.status_code)
                if status != 200:
                    retryable = status in {408, 425, 429} or status >= 500
                    if attempt == 0 and retryable:
                        if self.retry_delay_seconds > 0:
                            await asyncio.sleep(self.retry_delay_seconds)
                        continue
                    self._session_token = None
                    self._session_expires_at_ms = 0
                    return None
                body = response.json()
                if not isinstance(body, Mapping) or body.get("ok") is not True:
                    return None
                session = body.get("session")
                if not isinstance(session, Mapping) or not session.get("token"):
                    return None
                self._session_token = str(session["token"])
                self._session_expires_at_ms = int(float(session.get("expiresAt") or 0))
                return self._session_token
            except Exception:
                if attempt == 0:
                    if self.retry_delay_seconds > 0:
                        await asyncio.sleep(self.retry_delay_seconds)
                    continue
                return None
        return None

    async def _fetch_one(self, key: str, path: str, session: str) -> tuple[str, list[Mapping[str, Any]] | None, bool]:
        client = await self._http_client()
        for attempt in range(2):
            try:
                response = await client.get(
                    f"{self.base_url}{path}",
                    headers={"Authorization": f"Bearer {session}", "Accept": "application/json"},
                )
                status = int(response.status_code)
                if status == 401:
                    self._session_token = None
                    self._session_expires_at_ms = 0
                    return key, None, True
                if status == 200:
                    return key, _event_items(response.json()), False
                retryable = status in {408, 425, 429} or status >= 500
                if attempt == 0 and retryable:
                    if self.retry_delay_seconds > 0:
                        await asyncio.sleep(self.retry_delay_seconds)
                    continue
                return key, None, False
            except Exception:
                if attempt == 0:
                    if self.retry_delay_seconds > 0:
                        await asyncio.sleep(self.retry_delay_seconds)
                    continue
                return key, None, False
        return key, None, False

    async def _refresh(self) -> tuple[dict[str, list[Mapping[str, Any]]], tuple[str, ...], int, bool]:
        now_mono = time.monotonic()
        if self._fetched_at and now_mono - self._cache_mono <= self.cache_seconds:
            return self._cache, self._failures, self._fetched_at, bool(self._cache)
        async with self._lock:
            now_mono = time.monotonic()
            if self._fetched_at and now_mono - self._cache_mono <= self.cache_seconds:
                return self._cache, self._failures, self._fetched_at, bool(self._cache)
            session = await self._session()
            if not session:
                self._cache = {}
                self._failures = ("login_required",)
                self._cache_mono = now_mono
                self._fetched_at = int(time.time())
                return self._cache, self._failures, self._fetched_at, False
            requests = (
                ("private_fomo", "/api/extension/fomo-events?limit=150"),
            )
            results = await asyncio.gather(*(self._fetch_one(key, path, session) for key, path in requests))
            if any(unauthorized for _key, _rows, unauthorized in results):
                # One forced rebind is enough. Never loop on an invalid wallet token.
                self._auth_checked_mono = 0.0
                session = await self._session()
                if session:
                    results = await asyncio.gather(*(self._fetch_one(key, path, session) for key, path in requests))
            cache: dict[str, list[Mapping[str, Any]]] = {}
            failures: list[str] = []
            for key, rows, _unauthorized in results:
                if rows is None:
                    failures.append(key)
                else:
                    cache[key] = rows
            self._cache = cache
            self._failures = tuple(failures)
            self._cache_mono = time.monotonic()
            self._fetched_at = int(time.time())
            return cache, self._failures, self._fetched_at, bool(cache)

    async def snapshot(self, address: str, *, entry_time: int) -> Monitor985PrivateSnapshot:
        feeds, failures, fetched_at, connected = await self._refresh()
        if not connected:
            return Monitor985PrivateSnapshot(
                observed_at=int(entry_time),
                fetched_at=fetched_at,
                features=_missing_features(),
                connected=False,
                successful_sources=(),
                incomplete_sources=(),
                failed_sources=tuple(sorted(failures)),
                matched_events=0,
            )

        completed: set[str] = set()
        incomplete: list[str] = []
        selected: dict[str, list[Mapping[str, Any]]] = {}
        for key, events in feeds.items():
            pump = key == "private_pump"
            # Completeness is a property of the provider's raw capped response,
            # not of the post-filter Solana subset. A 150-row mixed-chain page
            # can otherwise shrink below 150 after filtering and falsely turn a
            # truncated 15m window into an observed zero.
            if _window_complete(events, entry_time=int(entry_time), seconds=900):
                completed.add(key)
                selected[key] = [event for event in events if _solana_chain(event, pump=pump)]
            else:
                incomplete.append(key)

        def matches(key: str, seconds: int) -> list[Mapping[str, Any]]:
            if key not in completed:
                return []
            pump = key == "private_pump"
            found: list[Mapping[str, Any]] = []
            for event in selected.get(key, []):
                timestamp = _event_time_ms(event)
                if timestamp is None or timestamp > int(entry_time):
                    continue
                if int(entry_time) - timestamp > int(seconds):
                    continue
                if _address(event, pump=pump) != address:
                    continue
                found.append(event)
            return found

        fomo15 = matches("private_fomo", 900)

        def behavior(events: Sequence[Mapping[str, Any]], *, pump: bool) -> tuple[float | None, float | None, float | None]:
            buy_count = sell_count = 0
            buy_usd = sell_usd = 0.0
            has_usd = False
            for event in events:
                side = _side(event, pump=pump)
                if side == "buy":
                    buy_count += 1
                elif side == "sell":
                    sell_count += 1
                usd = _usd(event, pump=pump)
                if usd is None or not side:
                    continue
                has_usd = True
                if side == "buy":
                    buy_usd += usd
                else:
                    sell_usd += usd
            count = buy_count + sell_count
            return (
                buy_count / count if count else None,
                _imbalance(buy_usd, sell_usd) if has_usd else None,
                _log1p(buy_usd + sell_usd) if has_usd else None,
            )

        fomo_buy, fomo_imbalance, fomo_usd_log = behavior(fomo15, pump=False)
        fomo_complete = "private_fomo" in completed
        features: dict[str, float | None] = {
            "ln(monitor_private_fomo_events_15m+1)": _log1p(len(fomo15)) if fomo_complete else None,
            "ln(monitor_private_fomo_unique_authors_15m+1)": _log1p(len({_principal(event, pump=False) for event in fomo15 if _principal(event, pump=False)})) if fomo_complete else None,
            "monitor_private_fomo_buy_ratio_15m": (fomo_buy if fomo_buy is not None else 0.5) if fomo_complete else None,
            "monitor_private_fomo_usd_imbalance_15m": (fomo_imbalance if fomo_imbalance is not None else 0.0) if fomo_complete else None,
            "ln(monitor_private_fomo_usd_15m+1)": (fomo_usd_log if fomo_usd_log is not None else 0.0) if fomo_complete else None,
            "monitor_private_source_coverage": 1.0 if fomo_complete else 0.0,
        }
        return Monitor985PrivateSnapshot(
            observed_at=int(entry_time),
            fetched_at=fetched_at,
            features=features,
            connected=True,
            successful_sources=tuple(sorted(feeds)),
            incomplete_sources=tuple(sorted(incomplete)),
            failed_sources=tuple(sorted(failures)),
            matched_events=len(fomo15),
        )
