from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
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
    LabelPolicy,
    PriceWindowResult,
)
from ..config import PROJECT_ROOT, Settings, get_settings
from ..database import Database, utc_now_iso
from ..repositories.samples import SampleRecord, SampleRepository
from .sol_price import SolUsdPriceService


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
            (now_ts - LabelPolicy().window_seconds,),
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
                UPDATE samples SET tag=?, price_1h_max_ratio=?, price_1h_min_ratio=?,
                    final_1h_close_ratio=?, first_take_profit_at=?, first_stop_loss_at=?,
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
                    0.60 if result.tag == 1 else -0.10,
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
        self._sol_price = SolUsdPriceService(database)
        existing_events = database.get_runtime_state("collector_events", [])
        seed = existing_events[-250:] if isinstance(existing_events, list) else []
        self._events: deque[dict[str, Any]] = deque(seed, maxlen=250)
        self._active_cycle_id: str | None = None

    @staticmethod
    def _type_label(token_type: object) -> str:
        return {
            "new_creation": "New Creation",
            "near_completion": "Near Completion",
        }.get(str(token_type), str(token_type or "Collector"))

    def _record_event(
        self,
        action: str,
        details: Mapping[str, object] | None = None,
        *,
        level: str | None = None,
    ) -> None:
        payload = dict(details or {})
        label = self._type_label(payload.get("token_type"))
        token = str(payload.get("token") or "")
        reasons = payload.get("reasons")
        reason_text = ", ".join(str(item) for item in reasons) if isinstance(reasons, list) else ""
        messages = {
            "cycle_started": (
                "采集周期开始：依次扫描 New Creation → Near Completion，"
                f"单类 limit={int(payload.get('requested_limit') or 0)}"
            ),
            "discovery_start": f"开始拉取 {label}",
            "discovery_result": f"{label} 拉回 {int(payload.get('returned') or 0)} 个候选",
            "candidate_duplicate": f"{token or 'candidate'} [{label}] 跳过：该 Token 仍有未成熟样本",
            "candidate_rejected": f"{token or 'candidate'} [{label}] 拒绝：{reason_text or 'unspecified'}",
            "candidate_accepted": f"{token or 'candidate'} [{label}] 已入样，进入 1h 标签等待",
            "discovery_type_complete": (
                f"{label} 完成：返回 {int(payload.get('returned') or 0)} / "
                f"入样 {int(payload.get('accepted') or 0)} / "
                f"拒绝 {int(payload.get('rejected') or 0)} / "
                f"重复 {int(payload.get('duplicates') or 0)}"
            ),
            "label_finalization": f"本轮完成 {int(payload.get('finalized') or 0)} 条 T+1h 标签补齐",
            "paper_monitor": (
                f"模拟盘监控：检查 {int(payload.get('checked') or 0)} 仓 / "
                f"退出 {int(payload.get('closed') or 0)} / 待重试 {int(payload.get('pending') or 0)}"
            ),
            "stage_error": f"{payload.get('stage') or 'collector'} 异常：{payload.get('error') or 'unknown'}",
            "cycle_complete": (
                f"采集周期完成：发现 {int(payload.get('discovered') or 0)} / "
                f"入样 {int(payload.get('accepted') or 0)} / "
                f"拒绝 {int(payload.get('rejected') or 0)} / "
                f"重复 {int(payload.get('duplicates') or 0)}，"
                f"耗时 {float(payload.get('elapsed_seconds') or 0):.1f}s"
            ),
        }
        resolved_level = level or (
            "error" if action == "stage_error" else
            "success" if action == "candidate_accepted" else
            "debug" if action in {"candidate_rejected", "candidate_duplicate"} else
            "info"
        )
        event = {
            "id": uuid.uuid4().hex[:12],
            "created_at": utc_now_iso(),
            "level": resolved_level,
            "action": action,
            "message": messages.get(action, action),
            "cycle_id": self._active_cycle_id,
            "details": payload,
        }
        self._events.append(event)
        self.database.set_runtime_state("collector_events", list(self._events))

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
            self._active_cycle_id = uuid.uuid4().hex[:10]
            requested_limit = min(self.settings.gmgn_trenches_limit, 80)
            previous_status = self.database.get_runtime_state("collector_status", {})
            previous_status = previous_status if isinstance(previous_status, dict) else {}
            self.database.set_runtime_state(
                "collector_status",
                {
                    **previous_status,
                    "state": "monitor_only" if self.monitor_only else "running",
                    "mode": "monitor_only" if self.monitor_only else "collector",
                    "cycle_state": "in_progress",
                    "cycle_id": self._active_cycle_id,
                    "cycle_started_at": utc_now_iso(),
                    "requested_limit_per_type": requested_limit,
                },
            )
            if not self.monitor_only:
                self._record_event("cycle_started", {"requested_limit": requested_limit})
            cycle_errors: list[dict[str, str]] = []
            collection_stats = {
                "discovered": 0,
                "accepted": 0,
                "rejected": 0,
                "duplicates": 0,
                "rejection_reasons": {},
                "type_stats": {},
            }
            finalized = 0

            # Freeze a recent SOL/USD observation before any simulated execution.
            # Paper accounting consumes this cache at the actual fee timestamp;
            # a stale/missing FX fact blocks the paper trade instead of repricing
            # it later with a different SOL price.
            try:
                await self._sol_price.refresh(self._service.provider, now_ts=int(time.time()))
            except Exception as exc:
                self.database.audit(
                    category="simulation",
                    action="sol_usd_price_refresh_failed",
                    severity="warning",
                    details={"error": f"{type(exc).__name__}: {exc}"[:300]},
                )

            # Position exits are handled by PositionMonitorWorker on its own
            # current-market cadence. Collector remains discovery/label-only.

            try:
                finalization = await self._service.finalize_due()
                finalized = finalization.finalized
                if finalized:
                    self._record_event("label_finalization", {"finalized": finalized})
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                cycle_errors.append({"stage": "label_finalization", "error": message})
                self._record_event("stage_error", {"stage": "label_finalization", "error": message})
                self.database.audit(
                    category="collector",
                    action="label_finalization_failed",
                    severity="error",
                    details={"error": message},
                )

            if not self.monitor_only:
                try:
                    collection = await self._service.collect_once(
                        limit=requested_limit,
                        event_sink=self._record_event,
                    )
                    collection_stats = {
                        "discovered": collection.discovered,
                        "accepted": collection.accepted,
                        "rejected": collection.rejected,
                        "duplicates": collection.unfinished_duplicates,
                        "rejection_reasons": dict(collection.rejection_reasons),
                        "type_stats": {key: dict(value) for key, value in collection.type_stats.items()},
                    }
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"[:500]
                    cycle_errors.append({"stage": "discovery", "error": message})
                    self._record_event("stage_error", {"stage": "discovery", "error": message})
                    self.database.audit(
                        category="collector",
                        action="discovery_cycle_failed",
                        severity="error",
                        details={"error": message},
                    )

            elapsed = time.time() - started
            if not self.monitor_only:
                self._record_event(
                    "cycle_complete",
                    {
                        **collection_stats,
                        "elapsed_seconds": elapsed,
                    },
                    level="warning" if cycle_errors else "info",
                )
            self.database.set_runtime_state(
                "collector_status",
                {
                    "state": "degraded" if cycle_errors else ("monitor_only" if self.monitor_only else "running"),
                    "mode": "monitor_only" if self.monitor_only else "collector",
                    "cycle_state": "idle",
                    "cycle_id": self._active_cycle_id,
                    "last_cycle_at": utc_now_iso(),
                    "last_cycle_duration_seconds": elapsed,
                    "requested_limit_per_type": requested_limit,
                    **collection_stats,
                    "finalized": finalized,
                    "errors": cycle_errors,
                },
            )
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
