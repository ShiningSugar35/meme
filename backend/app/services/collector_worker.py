from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from dotenv import dotenv_values

from ..collector import (
    ApiKeyRoles,
    AsyncRateLimiter,
    CollectedSample,
    CollectorEndpoints,
    CollectorService,
    DiscoveryService,
    EnrichmentService,
    GMGNDataClient,
    GMGNEnrichmentProvider,
    HttpxTransport,
    PriceWindowResult,
)
from ..config import PROJECT_ROOT, Settings, get_settings
from ..database import Database, utc_now_iso
from ..repositories.samples import SampleRecord, SampleRepository
from .paper_position_monitor import PaperPositionMonitor


class SqliteCollectorSink:
    """Adapter between the async collector protocol and SQLite repositories."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.samples = SampleRepository(database)

    async def has_unfinished_address(self, address: str) -> bool:
        row = self.database.fetch_one(
            "SELECT 1 AS found FROM samples WHERE chain='sol' AND address=? AND label_status='pending' LIMIT 1",
            (address,),
        )
        return row is not None

    async def add_sample(self, sample: CollectedSample) -> None:
        source = dict(sample.source)
        self.samples.insert(
            SampleRecord(
                chain="sol",
                address=sample.address,
                name=_source_text(source, "name", "base_name"),
                symbol=_source_text(source, "symbol", "base_symbol"),
                token_type=sample.token_type,
                entry_time=sample.entry_time,
                entry_price=sample.entry_price,
                launchpad=sample.launchpad,
                liquidity=sample.liquidity,
                liquidity_estimated=False,
                utility_eligible=True,
                features=dict(sample.features),
                label_status="pending",
                raw=source,
            )
        )

    async def due_samples(self, now_ts: int) -> Sequence[CollectedSample]:
        rows = self.database.fetch_all(
            """
            SELECT address, token_type, entry_time, entry_price, launchpad, liquidity, features_json, raw_json
            FROM samples
            WHERE label_status='pending' AND entry_time <= ?
            ORDER BY entry_time LIMIT 100
            """,
            (now_ts - 2 * 60 * 60,),
        )
        return tuple(
            CollectedSample(
                address=row["address"],
                token_type=row["token_type"] or "unknown",
                entry_time=int(row["entry_time"]),
                entry_price=float(row["entry_price"]),
                launchpad=row["launchpad"] or "unknown",
                liquidity=float(row["liquidity"] or 0),
                features=json.loads(row["features_json"] or "{}"),
                source=json.loads(row["raw_json"] or "{}"),
            )
            for row in rows
        )

    async def save_label(self, result: PriceWindowResult) -> None:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT id, features_json, utility_eligible FROM samples
                WHERE chain='sol' AND address=? AND entry_time=? AND label_status='pending'
                LIMIT 1
                """,
                (result.address, result.entry_time),
            ).fetchone()
            if row is None:
                return
            features = json.loads(row["features_json"] or "{}")
            if features.get("price_change_1h") is None and result.price_change_1h is not None:
                features["price_change_1h"] = result.price_change_1h
            if features.get("price_change_5m") is None and result.price_change_5m is not None:
                features["price_change_5m"] = result.price_change_5m
            connection.execute(
                """
                UPDATE samples SET tag=?, price_2h_max_ratio=?, price_2h_min_ratio=?,
                    final_close_ratio=?, first_take_profit_at=?, first_stop_loss_at=?,
                    exit_reason=?, same_bar_conflict=?, gross_return_rate=?,
                    return_source='collector_kline', label_status='mature', label_version=?,
                    label_source='collector', terminal_return_estimated=0,
                    utility_eligible=?, features_json=?, updated_at=? WHERE id=?
                """,
                (
                    result.tag,
                    result.max_price_ratio,
                    result.min_price_ratio,
                    result.final_close_ratio,
                    result.first_take_profit_at,
                    result.first_stop_loss_at,
                    result.exit_reason,
                    int(
                        result.first_take_profit_at is not None
                        and result.first_take_profit_at == result.first_stop_loss_at
                    ),
                    0.60 if result.tag == 1 else (
                        -0.10 if result.tag == 0 else result.final_close_ratio - 1.0
                    ),
                    result.label_version,
                    int(bool(row["utility_eligible"])),
                    json.dumps(features, ensure_ascii=False, separators=(",", ":")),
                    utc_now_iso(),
                    row["id"],
                ),
            )


def _source_text(source: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _env_values(path: Path = PROJECT_ROOT / ".env") -> dict[str, str]:
    values = dotenv_values(path)
    return {str(key): str(value) for key, value in values.items() if value not in (None, "")}


class CollectorWorker:
    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        monitor_only: bool = False,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.monitor_only = monitor_only
        self._stop = asyncio.Event()
        self._transport: HttpxTransport | None = None
        self._service: CollectorService | None = None
        self._paper_monitor = PaperPositionMonitor(database, self.settings)

    def _build(self) -> CollectorService:
        env = _env_values()
        secrets = [env.get(f"GMGN_API_KEY_{index}", "") for index in range(1, 13)]
        roles = ApiKeyRoles.from_secrets(secrets)
        base_url = env.get("GMGN_API_BASE_URL", "")
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
        client = GMGNDataClient(
            base_url=base_url,
            transport=self._transport,
            limiter=AsyncRateLimiter(2.0),
            endpoints=endpoints,
        )
        discovery = DiscoveryService(client, roles)
        provider = GMGNEnrichmentProvider(client, roles)
        return CollectorService(
            discovery,
            EnrichmentService(provider),
            provider,
            SqliteCollectorSink(self.database),
        )

    async def run_forever(self) -> None:
        self.database.set_runtime_state("collector_status", {"state": "starting"})
        try:
            self._service = self._build()
        except Exception as exc:
            self.database.set_runtime_state(
                "collector_status",
                {"state": "blocked", "reason": f"{type(exc).__name__}: {exc}"[:500]},
            )
            return
        while not self._stop.is_set():
            started = time.time()
            cycle_errors: list[dict[str, str]] = []
            collection_stats = {
                "discovered": 0,
                "accepted": 0,
                "rejected": 0,
                "duplicates": 0,
                "rejection_reasons": {},
            }
            monitor_stats = {
                "paper_positions_checked": 0,
                "paper_positions_closed": 0,
                "paper_positions_pending_exit": 0,
                "paper_positions_blocked": 0,
            }
            finalized = 0

            # Existing position exits have operational priority over discovering
            # new tokens. Failure in any stage is isolated so a broken Trenches
            # request can never stop paper TP/SL/timeout handling.
            try:
                monitor = await self._paper_monitor.run_cycle(self._service.provider)
                monitor_stats = {
                    "paper_positions_checked": monitor.checked_positions,
                    "paper_positions_closed": monitor.closed_positions,
                    "paper_positions_pending_exit": monitor.pending_positions,
                    "paper_positions_blocked": monitor.blocked_positions,
                }
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                cycle_errors.append({"stage": "paper_monitor", "error": message})
                self.database.audit(
                    category="simulation",
                    action="paper_monitor_cycle_failed",
                    severity="error",
                    details={"error": message},
                )

            try:
                finalization = await self._service.finalize_due()
                finalized = finalization.finalized
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                cycle_errors.append({"stage": "label_finalization", "error": message})
                self.database.audit(
                    category="collector",
                    action="label_finalization_failed",
                    severity="error",
                    details={"error": message},
                )

            if not self.monitor_only:
                try:
                    collection = await self._service.collect_once(
                        limit=min(self.settings.gmgn_trenches_limit, 80)
                    )
                    collection_stats = {
                        "discovered": collection.discovered,
                        "accepted": collection.accepted,
                        "rejected": collection.rejected,
                        "duplicates": collection.unfinished_duplicates,
                        "rejection_reasons": dict(collection.rejection_reasons),
                    }
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"[:500]
                    cycle_errors.append({"stage": "discovery", "error": message})
                    self.database.audit(
                        category="collector",
                        action="discovery_cycle_failed",
                        severity="error",
                        details={"error": message},
                    )

            self.database.set_runtime_state(
                "collector_status",
                {
                    "state": "degraded" if cycle_errors else ("monitor_only" if self.monitor_only else "running"),
                    "mode": "monitor_only" if self.monitor_only else "collector",
                    "last_cycle_at": utc_now_iso(),
                    **collection_stats,
                    "finalized": finalized,
                    **monitor_stats,
                    "errors": cycle_errors,
                },
            )
            elapsed = time.time() - started
            wait_seconds = max(1.0, self.settings.collector_poll_seconds - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_seconds)
            except TimeoutError:
                pass
        previous = self.database.get_runtime_state("collector_status", {})
        previous = previous if isinstance(previous, dict) else {}
        self.database.set_runtime_state(
            "collector_status",
            {
                **previous,
                "state": "stopped",
                "mode": "monitor_only" if self.monitor_only else "collector",
                "stopped_at": utc_now_iso(),
            },
        )
        if self._transport is not None:
            await self._transport.close()

    def stop(self) -> None:
        self._stop.set()
