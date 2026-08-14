from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from dotenv import dotenv_values

from ..collector.client import CollectorEndpoints, GMGNDataClient, HttpxTransport
from ..collector.enrichment import merge_sources
from ..collector.errors import CollectorAPIError, CollectorNetworkError
from ..collector.filters import first, normalize_token, to_float
from ..collector.models import ApiKeyRoles
from ..collector.rate_limit import AsyncRateLimiter
from ..config import PROJECT_ROOT, Settings, get_settings
from ..database import Database, utc_now_iso
from ..trading.live.errors import LiveTradeError
from ..trading.live.models import OrderStatus, SwapIntent, TradeSide
from .live_trading import LiveTradingService
from .paper_position_monitor import PaperPositionMonitor
from .paper_trading import PaperTradingService
from .platform_configuration import ENV_PATH, PlatformConfigurationService


@dataclass(frozen=True, slots=True)
class PositionMonitorCycle:
    checked_positions: int = 0
    market_requests: int = 0
    paper_closed: int = 0
    paper_pending: int = 0
    live_triggered: int = 0
    live_pending: int = 0
    blocked_positions: int = 0
    skipped_liquidation_positions: int = 0
    completed_at: str = ""


class GMGNPositionMarketProvider:
    """Current-price provider using the configured GMGN key rotation/fallback pool."""

    def __init__(
        self,
        client: GMGNDataClient,
        roles: ApiKeyRoles,
    ) -> None:
        self.client = client
        self.roles = roles
        self._cache: dict[str, Mapping[str, Any]] = {}
        self._position_index = 0

    def reset_cycle_cache(self) -> None:
        self._cache.clear()

    async def token_bundle(self, address: str) -> Mapping[str, Any]:
        cached = self._cache.get(address)
        if cached is not None:
            return cached
        pool = self.roles.position_monitor
        last_error: Exception | None = None
        start = self._position_index % len(pool)
        self._position_index = (start + 1) % len(pool)
        for offset in range(len(pool)):
            slot = pool[(start + offset) % len(pool)]
            try:
                data = await self.client.request(
                    slot,
                    self.client.endpoints.token_info,
                    params={"chain": "sol", "address": address},
                )
                bundle: Mapping[str, Any] = {"token_info": data}
                self._cache[address] = bundle
                return bundle
            except (CollectorNetworkError, CollectorAPIError) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("GMGN position-monitor key pool is empty")


class PositionMonitorService:
    """Configurable current-market monitor shared by simulation and live positions."""

    PAPER_STRATEGIES = ("model_1", "model_2", "model_3", "rules_only")

    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        paper_service: PaperTradingService | None = None,
        paper_monitor: PaperPositionMonitor | None = None,
        live_service: LiveTradingService | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.configuration = PlatformConfigurationService(database)
        self.paper = paper_service or PaperTradingService(database, self.settings)
        self.paper_monitor = paper_monitor or PaperPositionMonitor(
            database, self.settings, paper_service=self.paper
        )
        self.live = live_service or LiveTradingService(database, self.settings)
        self._live_tasks: dict[str, asyncio.Task[None]] = {}

    async def run_cycle(
        self,
        provider: GMGNPositionMarketProvider,
        *,
        now_ts: int | None = None,
    ) -> PositionMonitorCycle:
        current_ts = int(now_ts or time.time())
        if not self.settings.position_monitor_enabled:
            return PositionMonitorCycle(completed_at=utc_now_iso())

        rows = self.database.fetch_all(
            """
            SELECT p.*
            FROM positions p
            WHERE p.status IN ('open','closing')
              AND (
                    p.account_kind='live'
                    OR (p.account_kind='simulation' AND p.strategy_key IN ('model_1','model_2','model_3','rules_only'))
                  )
            ORDER BY p.entry_time,p.id
            """
        )
        liquidation = self.database.get_runtime_state("liquidation_job") or {}
        liquidation_ids = (
            {str(value) for value in liquidation.get("position_ids", [])}
            if isinstance(liquidation, dict) and liquidation.get("status") in {"queued", "running"}
            else set()
        )
        candidates = [row for row in rows if str(row["id"]) not in liquidation_ids]
        skipped = len(rows) - len(candidates)
        grouped: dict[str, list[dict[str, Any]]] = {}
        direct_retry: list[dict[str, Any]] = []
        for row in candidates:
            if str(row["status"]) == "closing":
                try:
                    metadata = json.loads(row.get("metadata_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
                pending_key = (
                    "paper_exit_pending"
                    if str(row["account_kind"]) == "simulation"
                    else "live_exit_pending"
                )
                if isinstance(metadata.get(pending_key), dict):
                    direct_retry.append(row)
                    continue
            grouped.setdefault(str(row["token_address"]), []).append(row)

        checked = market_requests = paper_closed = paper_pending = 0
        live_triggered = live_pending = blocked = 0
        paper_work: list[tuple[dict[str, Any], float]] = []
        for row in direct_retry:
            checked += 1
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            pending_key = (
                "paper_exit_pending"
                if str(row["account_kind"]) == "simulation"
                else "live_exit_pending"
            )
            pending = metadata.get(pending_key) if isinstance(metadata, dict) else None
            reference_price = (
                float(pending.get("reference_price") or 0.0)
                if isinstance(pending, dict)
                else 0.0
            )
            if str(row["account_kind"]) == "simulation":
                paper_work.append((row, reference_price))
            else:
                outcome = self._monitor_live(row, reference_price, current_ts)
                live_triggered += int(outcome == "triggered")
                live_pending += int(outcome == "pending")
                blocked += int(outcome == "blocked")

        async def fetch_current_market(
            address: str, positions: list[dict[str, Any]]
        ) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None, Exception | None]:
            try:
                return address, positions, self._snapshot(await provider.token_bundle(address)), None
            except Exception as exc:
                return address, positions, None, exc

        market_batches = await asyncio.gather(
            *(fetch_current_market(address, positions) for address, positions in grouped.items())
        )
        market_requests += len(grouped)
        for address, positions, snapshot, market_error in market_batches:
            if market_error is not None or snapshot is None:
                message = f"{type(market_error).__name__}: {market_error}"[:300]
                checked += len(positions)
                blocked += len(positions)
                self.database.audit(
                    category="trading",
                    action="position_monitor_market_data_failed",
                    severity="warning",
                    entity_type="token",
                    entity_id=address,
                    details={"positions": len(positions), "error": message},
                )
                continue
            price = float(snapshot.get("price") or 0.0)
            if price <= 0:
                checked += len(positions)
                blocked += len(positions)
                continue

            for row in positions:
                self._persist_snapshot(str(row["id"]), snapshot)
                checked += 1
                if str(row["account_kind"]) == "simulation":
                    paper_work.append((row, price))
                else:
                    outcome = self._monitor_live(row, price, current_ts)
                    live_triggered += int(outcome == "triggered")
                    live_pending += int(outcome == "pending")
                    blocked += int(outcome == "blocked")

        if paper_work:
            jupiter_key_count = len(
                self.configuration.provider_credentials("jupiter") or self.settings.jupiter_api_keys
            )
            exit_concurrency = max(1, min(8, jupiter_key_count or 1))
            semaphore = asyncio.Semaphore(exit_concurrency)

            async def run_paper(row: dict[str, Any], price: float) -> str:
                async with semaphore:
                    return await self._monitor_paper(row, price, current_ts)

            outcomes = await asyncio.gather(
                *(run_paper(row, price) for row, price in paper_work)
            )
            paper_closed += sum(int(outcome == "closed") for outcome in outcomes)
            paper_pending += sum(int(outcome == "pending") for outcome in outcomes)
            blocked += sum(int(outcome == "blocked") for outcome in outcomes)

        self._live_tasks = {
            key: task for key, task in self._live_tasks.items() if not task.done()
        }
        return PositionMonitorCycle(
            checked_positions=checked,
            market_requests=market_requests,
            paper_closed=paper_closed,
            paper_pending=paper_pending,
            live_triggered=live_triggered,
            live_pending=live_pending,
            blocked_positions=blocked,
            skipped_liquidation_positions=skipped,
            completed_at=utc_now_iso(),
        )

    async def _monitor_paper(self, row: dict[str, Any], price: float, now_ts: int) -> str:
        result = self.paper.monitor_position_realtime(
            str(row["id"]), price, now_ts=now_ts, defer_execution=True
        )
        if result.state == "pending" and result.reason == "execution_route_probe_required":
            execution_quote = await self.paper_monitor._prime_exit_route(row, now_ts=now_ts)
            result = self.paper.monitor_position_realtime(
                str(row["id"]),
                price,
                now_ts=now_ts,
                execution_quote=execution_quote,
            )
        return result.state

    def _monitor_live(self, row: dict[str, Any], price: float, now_ts: int) -> str:
        position_id = str(row["id"])
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        pending = metadata.get("live_exit_pending")
        if isinstance(pending, dict):
            task = self._live_tasks.get(position_id)
            if task is None or task.done():
                if self._live_execution_armed():
                    self._live_tasks[position_id] = asyncio.create_task(
                        self._execute_live_exit(position_id),
                        name=f"live-position-exit:{position_id}",
                    )
                else:
                    return "blocked"
            return "pending"
        if str(row["status"]) != "open":
            return "pending"

        reason: str | None = None
        stop_price = float(row.get("stop_loss_price") or 0.0)
        take_price = float(row.get("take_profit_price") or 0.0)
        if stop_price > 0 and price <= stop_price:
            reason = "stop_loss_0_9x"
        elif take_price > 0 and price >= take_price:
            reason = "take_profit_1_6x"
        else:
            expires_at = datetime.fromisoformat(str(row["expires_at"]))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now_ts >= int(expires_at.timestamp()):
                reason = "timeout_1h"
        if reason is None:
            return "open"

        if not self._live_execution_armed():
            signal = {
                "reason": reason,
                "reference_price": price,
                "trigger_at": now_ts,
                "blocked_reason": "live_execution_not_armed",
            }
            if metadata.get("last_live_exit_signal") != signal:
                metadata["last_live_exit_signal"] = signal
                self.database.execute(
                    "UPDATE positions SET metadata_json=? WHERE id=? AND status='open'",
                    (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
                )
            return "blocked"

        raw_amount = metadata.get("token_amount_raw") or metadata.get("quantity_atomic")
        output_token = metadata.get("exit_output_token") or metadata.get("quote_token")
        if not self.settings.wallet_public_key or not raw_amount or not output_token:
            metadata["last_live_exit_signal"] = {
                "reason": reason,
                "reference_price": price,
                "trigger_at": now_ts,
                "blocked_reason": "live_exit_facts_incomplete",
            }
            self.database.execute(
                "UPDATE positions SET metadata_json=? WHERE id=? AND status='open'",
                (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
            )
            return "blocked"

        pending = {
            "reason": reason,
            "reference_price": price,
            "trigger_at": now_ts,
            "client_order_id": f"position-monitor:{position_id}:{now_ts}",
        }
        metadata["live_exit_pending"] = pending
        updated = self.database.execute(
            "UPDATE positions SET status='closing',metadata_json=? WHERE id=? AND status='open'",
            (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
        )
        if updated != 1:
            return "pending"
        self._live_tasks[position_id] = asyncio.create_task(
            self._execute_live_exit(position_id),
            name=f"live-position-exit:{position_id}",
        )
        return "triggered"

    async def _execute_live_exit(self, position_id: str) -> None:
        row = self.database.fetch_one("SELECT * FROM positions WHERE id=?", (position_id,))
        if not row or str(row.get("account_kind") or "") != "live":
            return
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        pending = metadata.get("live_exit_pending")
        if not isinstance(pending, dict):
            return
        raw_amount = metadata.get("token_amount_raw") or metadata.get("quantity_atomic")
        output_token = metadata.get("exit_output_token") or metadata.get("quote_token")
        if not self.settings.wallet_public_key or not raw_amount or not output_token:
            return
        intent = SwapIntent(
            chain="sol",
            wallet_address=self.settings.wallet_public_key,
            input_token=str(row["token_address"]),
            output_token=str(output_token),
            input_amount_raw=str(raw_amount),
            side=TradeSide.SELL,
            client_order_id=str(pending["client_order_id"]),
            metadata={"position_id": position_id, "account_kind": "live"},
        )
        try:
            result = await self.live.execute(intent)
        except LiveTradeError as exc:
            metadata["live_exit_last_error"] = {
                "kind": exc.kind.value,
                "code": exc.code,
                "at": utc_now_iso(),
            }
            self.database.execute(
                "UPDATE positions SET metadata_json=? WHERE id=? AND status='closing'",
                (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
            )
            self.database.audit(
                category="trading",
                action="live_position_exit_failed",
                severity="warning",
                entity_type="position",
                entity_id=position_id,
                details={"kind": exc.kind.value, "code": exc.code},
            )
            return
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:300]
            metadata["live_exit_last_error"] = {
                "kind": "unexpected",
                "code": type(exc).__name__,
                "at": utc_now_iso(),
            }
            self.database.execute(
                "UPDATE positions SET metadata_json=? WHERE id=? AND status='closing'",
                (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
            )
            self.database.audit(
                category="trading",
                action="live_position_exit_failed",
                severity="error",
                entity_type="position",
                entity_id=position_id,
                details={"kind": "unexpected", "error": message},
            )
            return

        metadata["live_exit_execution"] = {
            "status": result.status.value,
            "order_id": result.order_id,
            "tx_hash": result.tx_hash,
            "attempts": result.attempts,
            "error_code": result.error_code,
            "updated_at": utc_now_iso(),
        }
        if result.status is OrderStatus.CONFIRMED:
            reason = str(pending.get("reason") or "market_exit")
            metadata.pop("live_exit_pending", None)
            self.database.execute(
                """
                UPDATE positions SET status='closed',exit_time=?,exit_reason=?,metadata_json=?
                WHERE id=? AND status='closing'
                """,
                (
                    utc_now_iso(),
                    reason,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    position_id,
                ),
            )
            return
        if result.status in {OrderStatus.FAILED, OrderStatus.EXPIRED}:
            self.database.execute(
                "UPDATE positions SET status='manual_intervention',metadata_json=? WHERE id=? AND status='closing'",
                (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
            )
            return
        self.database.execute(
            "UPDATE positions SET metadata_json=? WHERE id=? AND status='closing'",
            (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
        )

    def _live_execution_armed(self) -> bool:
        return (
            not self.settings.dry_run
            and bool(self.database.get_runtime_state("live_trading_enabled", False))
        )

    @staticmethod
    def _snapshot(bundle: Mapping[str, Any]) -> dict[str, Any]:
        merged = merge_sources(bundle)
        normalized = normalize_token(merged, "")
        price = to_float(normalized.get("price"))
        liquidity = to_float(normalized.get("liquidity"))
        snapshot: dict[str, Any] = {
            "as_of": utc_now_iso(),
            "price": price,
            "liquidity_usd": liquidity,
        }
        decimals = to_float(first(merged, ("decimals", "decimal")))
        if decimals is not None and 0 <= decimals <= 18:
            snapshot["token_decimals"] = int(decimals)
        market_cap = to_float(normalized.get("marketcap"))
        source = "gmgn_direct" if market_cap is not None else None
        if market_cap is None and price is not None:
            circulating = to_float(first(merged, ("circulating_supply",)))
            total = to_float(first(merged, ("total_supply",)))
            supply = circulating if circulating is not None else total
            if supply is not None and supply > 0:
                market_cap = price * supply
                source = (
                    "gmgn_price_x_circulating_supply"
                    if circulating is not None
                    else "gmgn_price_x_total_supply"
                )
        snapshot["market_cap_usd"] = market_cap
        snapshot["market_cap_source"] = source
        return snapshot

    def _persist_snapshot(self, position_id: str, snapshot: Mapping[str, Any]) -> None:
        row = self.database.fetch_one("SELECT metadata_json FROM positions WHERE id=?", (position_id,)) or {}
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        metadata["market_snapshot"] = dict(snapshot)
        self.database.execute(
            "UPDATE positions SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), position_id),
        )

    async def shutdown(self) -> None:
        pending = [task for task in self._live_tasks.values() if not task.done()]
        if pending:
            done, remaining = await asyncio.wait(pending, timeout=10)
            for task in remaining:
                task.cancel()


class PositionMonitorWorker:
    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        gmgn_limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.service = PositionMonitorService(database, self.settings)
        self.configuration = PlatformConfigurationService(database)
        self._stop = asyncio.Event()
        self._transport: HttpxTransport | None = None
        self._provider: GMGNPositionMarketProvider | None = None
        self._gmgn_limiter = gmgn_limiter
        self._env_mtime_ns: int | None = None

    def _build_provider(self) -> GMGNPositionMarketProvider:
        env = {
            str(key): str(value)
            for key, value in dotenv_values(PROJECT_ROOT / ".env").items()
            if value not in (None, "")
        }
        credentials = self.configuration.provider_credentials("gmgn")
        roles = ApiKeyRoles.from_secrets(credentials)
        endpoints = CollectorEndpoints(
            trenches=env.get("GMGN_TRENCHES_PATH", "/v1/trenches"),
            token_info=env.get("GMGN_TOKEN_INFO_PATH", "/v1/token/info"),
            token_security=env.get("GMGN_TOKEN_SECURITY_PATH", "/v1/token/security"),
            token_pool_info=env.get("GMGN_TOKEN_POOL_INFO_PATH", "/v1/token/pool_info"),
            top_holders=env.get("GMGN_TOKEN_HOLDERS_PATH", "/v1/market/token_top_holders"),
            kline=env.get("GMGN_KLINE_PATH", "/v1/market/token_kline"),
            trending=env.get("GMGN_TRENDING_PATH", "/v1/market/rank"),
            created_tokens=env.get("GMGN_PORTFOLIO_CREATED_TOKENS_PATH", "/v1/user/created_tokens"),
        )
        self._transport = HttpxTransport()
        runtime = self.configuration.runtime_values()
        limiter = self._gmgn_limiter or AsyncRateLimiter(runtime["gmgn_global_rps"])
        limiter.requests_per_second = runtime["gmgn_global_rps"]
        self._gmgn_limiter = limiter
        client = GMGNDataClient(
            base_url=env.get("GMGN_API_BASE_URL", ""),
            transport=self._transport,
            limiter=limiter,
            endpoints=endpoints,
        )
        return GMGNPositionMarketProvider(client, roles)

    async def run_forever(self) -> None:
        if not self.settings.position_monitor_enabled:
            self.database.set_runtime_state(
                "position_monitor_status", {"state": "disabled", "updated_at": utc_now_iso()}
            )
            return
        try:
            self._provider = self._build_provider()
        except Exception as exc:
            self._provider = None
            self.database.set_runtime_state(
                "position_monitor_status",
                {"state": "blocked", "error": f"{type(exc).__name__}: {exc}"[:500]},
            )
        initial_runtime = self.configuration.runtime_values()
        initial_status = self.database.get_runtime_state("position_monitor_status", {})
        initial_status = initial_status if isinstance(initial_status, dict) else {}
        self.database.set_runtime_state(
            "position_monitor_status",
            {
                **initial_status,
                "state": "running" if self._provider is not None else "blocked",
                "target_poll_seconds": initial_runtime["position_monitor_poll_seconds"],
                "started_at": utc_now_iso(),
            },
        )
        try:
            previous_started: float | None = None
            self._env_mtime_ns = ENV_PATH.stat().st_mtime_ns if ENV_PATH.exists() else None
            while not self._stop.is_set():
                runtime_config = self.configuration.runtime_values()
                if self._gmgn_limiter is not None:
                    self._gmgn_limiter.requests_per_second = runtime_config["gmgn_global_rps"]
                current_mtime = ENV_PATH.stat().st_mtime_ns if ENV_PATH.exists() else None
                if self._provider is None or current_mtime != self._env_mtime_ns:
                    try:
                        if self._transport is not None:
                            await self._transport.close()
                        self._provider = self._build_provider()
                        self._env_mtime_ns = current_mtime
                    except Exception as exc:
                        self.database.set_runtime_state(
                            "position_monitor_status",
                            {
                                "state": "degraded",
                                "target_poll_seconds": runtime_config["position_monitor_poll_seconds"],
                                "last_error": f"configuration_reload_failed: {type(exc).__name__}: {exc}"[:500],
                                "updated_at": utc_now_iso(),
                            },
                        )
                        try:
                            await asyncio.wait_for(
                                self._stop.wait(),
                                timeout=min(3.0, runtime_config["position_monitor_poll_seconds"]),
                            )
                        except TimeoutError:
                            pass
                        continue
                started = time.monotonic()
                start_interval = started - previous_started if previous_started is not None else None
                previous_started = started
                cycle_started_at = utc_now_iso()
                self._provider.reset_cycle_cache()
                try:
                    report = await self.service.run_cycle(self._provider)
                    elapsed = time.monotonic() - started
                    self.database.set_runtime_state(
                        "position_monitor_status",
                        {
                            "state": "running",
                            "target_poll_seconds": runtime_config["position_monitor_poll_seconds"],
                            "last_cycle_seconds": elapsed,
                            "last_cycle_started_at": cycle_started_at,
                            "last_start_interval_seconds": start_interval,
                            **asdict(report),
                        },
                    )
                except Exception as exc:
                    elapsed = time.monotonic() - started
                    message = f"{type(exc).__name__}: {exc}"[:500]
                    self.database.set_runtime_state(
                        "position_monitor_status",
                        {
                            "state": "degraded",
                            "target_poll_seconds": runtime_config["position_monitor_poll_seconds"],
                            "last_cycle_seconds": elapsed,
                            "last_cycle_started_at": cycle_started_at,
                            "last_start_interval_seconds": start_interval,
                            "last_error": message,
                            "updated_at": utc_now_iso(),
                        },
                    )
                    self.database.audit(
                        category="trading",
                        action="position_monitor_cycle_failed",
                        severity="error",
                        details={"error": message},
                    )
                wait_seconds = max(
                    0.1,
                    float(runtime_config["position_monitor_poll_seconds"])
                    - (time.monotonic() - started),
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait_seconds)
                except TimeoutError:
                    pass
        finally:
            await self.service.shutdown()
            if self._transport is not None:
                await self._transport.close()
            self.database.set_runtime_state(
                "position_monitor_status", {"state": "stopped", "stopped_at": utc_now_iso()}
            )

    def stop(self) -> None:
        self._stop.set()
