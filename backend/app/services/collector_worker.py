from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from dotenv import dotenv_values

from ..collector import (
    ApiKeyRoles,
    AsyncRateLimiter,
    CollectedSample,
    CollectorEndpoints,
    CollectorNetworkError,
    CollectorRateLimitError,
    CollectorService,
    DiscoveryService,
    EnrichmentService,
    FEATURE_SCHEMA_VERSION,
    GMGNDataClient,
    GMGNEnrichmentProvider,
    HttpxTransport,
    LabelPolicy,
    PriceWindowResult,
)
from ..config import PROJECT_ROOT, Settings, get_settings
from ..database import Database, utc_now_iso
from ..collector.market_regime import GMGNMarketRegimeProvider
from ..collector.discovery_experiment import DiscoveryExperimentManager
from ..repositories.samples import SampleRecord, SampleRepository
from .onchain_admission import OnchainAdmissionService
from .public_social_signals import PublicSocialSignalProvider
from .monitor985_private_signals import Monitor985PrivateSignalProvider
from .platform_configuration import ENV_PATH, PlatformConfigurationService
from .sol_price import SolUsdPriceService


class SqliteCollectorSink:
    """Adapter between the async collector protocol and SQLite repositories."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.samples = SampleRepository(database)

    async def has_unfinished_address(self, address: str) -> bool:
        row = self.database.fetch_one(
            "SELECT 1 AS found FROM samples WHERE chain='sol' AND address=? AND label_status='pending' AND feature_schema_version=? LIMIT 1",
            (address, FEATURE_SCHEMA_VERSION),
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
                age_minutes=sample.age_minutes,
                holder_count=sample.holder_count,
                entry_price=sample.entry_price,
                launchpad=sample.launchpad,
                liquidity=sample.liquidity,
                liquidity_estimated=False,
                utility_eligible=True,
                features=dict(sample.features),
                feature_schema_version=sample.feature_schema_version,
                feature_snapshot_at=sample.feature_snapshot_at or sample.entry_time,
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
            # Entry features are immutable after feature_snapshot_at; label finalization only writes target facts.
            # price_change_1h is deliberately not reconstructed at T+1h.
            # No mutation of the frozen entry feature payload.
            # price_change_5m is deliberately not reconstructed at T+1h.
            # Missing entry facts remain missing rather than being rewritten as zero or hindsight data.
            policy = LabelPolicy()
            connection.execute(
                """
                UPDATE samples SET tag=?, label_max_price_ratio=?, label_min_price_ratio=?,
                    label_final_close_ratio=?, label_window_seconds=?,
                    first_take_profit_at=?, first_stop_loss_at=?, exit_reason=?, same_bar_conflict=?,
                    gross_return_rate=?, return_source='collector_kline',
                    label_status='mature', label_version=?, label_source='collector',
                    terminal_return_estimated=0, utility_eligible=?, features_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    result.tag,
                    result.max_price_ratio,
                    result.min_price_ratio,
                    result.final_close_ratio,
                    policy.window_seconds,
                    result.first_take_profit_at,
                    result.first_stop_loss_at,
                    result.exit_reason,
                    int(
                        result.first_take_profit_at is not None
                        and result.first_take_profit_at == result.first_stop_loss_at
                    ),
                    (
                        policy.take_profit_ratio - 1.0
                        if result.tag == 1
                        else policy.stop_loss_ratio - 1.0
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


def _is_network_failure(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, CollectorNetworkError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


class CollectorWorker:
    def __init__(
        self,
        database: Database,
        settings: Settings | None = None,
        *,
        monitor_only: bool = False,
        gmgn_limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.configuration = PlatformConfigurationService(database)
        self.monitor_only = monitor_only
        self._stop = asyncio.Event()
        self._transport: HttpxTransport | None = None
        self._onchain_admission: OnchainAdmissionService | None = None
        self._public_social_signals: PublicSocialSignalProvider | None = None
        self._account_social_signals: Monitor985PrivateSignalProvider | None = None
        self._service: CollectorService | None = None
        self._regime_provider: GMGNMarketRegimeProvider | None = None
        self._gmgn_limiter = gmgn_limiter
        self._experiment = DiscoveryExperimentManager(database, gmgn_limiter)
        self._env_mtime_ns: int | None = None
        self._transport_rebuilds = 0
        self._sol_price = SolUsdPriceService(database)
        existing_circuit = database.get_runtime_state("collector_rate_limit_circuit", {})
        existing_circuit = existing_circuit if isinstance(existing_circuit, dict) else {}
        self._rate_limit_streak = int(existing_circuit.get("streak") or 0)
        self._rate_limit_until_epoch = float(existing_circuit.get("next_probe_epoch") or 0.0)
        self._rate_limit_previous_streak = int(existing_circuit.get("previous_streak") or 0)
        self._rate_limit_last_recovered_epoch = float(existing_circuit.get("recovered_epoch") or 0.0)
        existing_events = database.get_runtime_state("collector_events", [])
        seed = existing_events[-250:] if isinstance(existing_events, list) else []
        self._events: deque[dict[str, Any]] = deque(seed, maxlen=250)
        self._active_cycle_id: str | None = None

    @staticmethod
    def _rate_limit_error(exc: BaseException | None) -> CollectorRateLimitError | None:
        seen: set[int] = set()
        current = exc
        while isinstance(current, BaseException) and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, CollectorRateLimitError):
                return current
            current = current.__cause__ or current.__context__
        return None

    def _rate_limit_remaining(self) -> float:
        return max(0.0, self._rate_limit_until_epoch - time.time())

    def _open_rate_limit_circuit(self, exc: BaseException, *, stage: str) -> dict[str, Any]:
        rate_error = self._rate_limit_error(exc)
        now = time.time()
        recent_recovery = (
            self._rate_limit_last_recovered_epoch > 0
            and now - self._rate_limit_last_recovered_epoch <= 900.0
        )
        base_streak = self._rate_limit_streak
        if base_streak <= 0 and recent_recovery:
            base_streak = self._rate_limit_previous_streak
        self._rate_limit_streak = max(1, base_streak + 1)
        # Five minutes is deliberately longer than the ~100-155s rolling reset
        # observed in production. Repeated half-open failures double the quiet
        # period up to one hour so the worker cannot perpetually renew a ban.
        backoff_seconds = min(3600.0, 300.0 * (2 ** min(4, self._rate_limit_streak - 1)))
        server_reset = float(rate_error.reset_at) if rate_error and rate_error.reset_at else 0.0
        self._rate_limit_until_epoch = max(now + backoff_seconds, server_reset + 15.0)
        payload = {
            "state": "open",
            "streak": self._rate_limit_streak,
            "stage": stage,
            "backoff_seconds": backoff_seconds,
            "server_reset_at": int(server_reset) if server_reset else None,
            "next_probe_epoch": self._rate_limit_until_epoch,
            "next_probe_at": datetime.fromtimestamp(self._rate_limit_until_epoch, timezone.utc).isoformat(),
            "opened_at": utc_now_iso(),
        }
        self.database.set_runtime_state("collector_rate_limit_circuit", payload)
        self.database.audit(
            category="collector",
            action="gmgn_rate_limit_circuit_opened",
            severity="warning",
            details=payload,
        )
        return payload

    def _close_rate_limit_circuit(self) -> None:
        if self._rate_limit_streak <= 0 and self._rate_limit_until_epoch <= 0:
            return
        previous_streak = self._rate_limit_streak
        recovered_epoch = time.time()
        self._rate_limit_previous_streak = previous_streak
        self._rate_limit_last_recovered_epoch = recovered_epoch
        self._rate_limit_streak = 0
        self._rate_limit_until_epoch = 0.0
        payload = {
            "state": "closed",
            "streak": 0,
            "previous_streak": previous_streak,
            "recovered_epoch": recovered_epoch,
            "recovered_at": datetime.fromtimestamp(recovered_epoch, timezone.utc).isoformat(),
            "probation_until": datetime.fromtimestamp(recovered_epoch + 900.0, timezone.utc).isoformat(),
        }
        self.database.set_runtime_state("collector_rate_limit_circuit", payload)
        self.database.audit(
            category="collector",
            action="gmgn_rate_limit_circuit_closed",
            severity="info",
            details=payload,
        )

    @staticmethod
    def _type_label(token_type: object) -> str:
        return {
            "new_creation": "New Creation",
            "trending": "Trending / Volume 1h",
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
        _rejection_stage = {
            "trench_prefilter": "本地粗筛",
            "enrichment": "Enrichment 深筛",
        }.get(str(payload.get("stage") or ""), "筛选")
        reason_text = f"{_rejection_stage} / {reason_text}" if reason_text else _rejection_stage
        messages = {
            "cycle_started": (
                "采集周期开始：依次扫描 Trenches New → Trending Volume 1h，"
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

    def _persist_cycle_snapshot(self, stats: Mapping[str, Any]) -> None:
        type_stats = stats.get("type_stats") if isinstance(stats.get("type_stats"), Mapping) else {}
        self.database.execute(
            """
            INSERT INTO collector_cycle_snapshots(
                observed_at,discovered,accepted,rejected,new_creation_returned,
                near_completion_returned,trending_returned,payload_json,recorded_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                int(time.time()),
                int(stats.get("discovered") or 0),
                int(stats.get("accepted") or 0),
                int(stats.get("rejected") or 0),
                int((type_stats.get("new_creation") or {}).get("returned") or 0),
                0,
                int((type_stats.get("trending") or {}).get("returned") or 0),
                json.dumps(dict(stats), ensure_ascii=False, separators=(",", ":")),
                utc_now_iso(),
            ),
        )

    def _build(self) -> CollectorService:
        env = _env_values()
        credentials = self.configuration.provider_credentials("gmgn")
        roles = ApiKeyRoles.from_secrets(credentials)
        base_url = env.get("GMGN_API_BASE_URL", "")
        endpoints = CollectorEndpoints(
            trenches=env.get("GMGN_TRENCHES_PATH", "/v1/trenches"),
            token_info=env.get("GMGN_TOKEN_INFO_PATH", "/v1/token/info"),
            token_security=env.get("GMGN_TOKEN_SECURITY_PATH", "/v1/token/security"),
            token_pool_info=env.get("GMGN_TOKEN_POOL_INFO_PATH", "/v1/token/pool_info"),
            top_holders=env.get("GMGN_TOKEN_HOLDERS_PATH", "/v1/market/token_top_holders"),
            kline=env.get("GMGN_KLINE_PATH", "/v1/market/token_kline"),
            trending=env.get("GMGN_TRENDING_PATH", "/v1/market/rank"),
            signal=env.get("GMGN_SIGNAL_PATH", "/v1/market/token_signal"),
            hot_searches=env.get("GMGN_HOT_SEARCHES_PATH", "/v1/market/hot_searches"),
            created_tokens=env.get("GMGN_PORTFOLIO_CREATED_TOKENS_PATH", "/v1/user/created_tokens"),
        )
        self._transport = HttpxTransport()
        runtime = self.configuration.runtime_values()
        limiter = self._gmgn_limiter or AsyncRateLimiter(runtime["gmgn_global_rps"])
        limiter.requests_per_second = runtime["gmgn_global_rps"]
        self._gmgn_limiter = limiter
        self._experiment.limiter = limiter
        client = GMGNDataClient(
            base_url=base_url,
            transport=self._transport,
            limiter=limiter,
            endpoints=endpoints,
            telemetry_sink=self._experiment.record_api_event,
        )
        discovery = DiscoveryService(client, roles)
        provider = GMGNEnrichmentProvider(client, roles)
        self._regime_provider = GMGNMarketRegimeProvider(client, roles)
        self._onchain_admission = OnchainAdmissionService()
        self._public_social_signals = PublicSocialSignalProvider()
        self._account_social_signals = Monitor985PrivateSignalProvider()
        return CollectorService(
            discovery,
            EnrichmentService(
                provider,
                onchain_admission=self._onchain_admission,
                public_social_signals=self._public_social_signals,
                account_social_signals=self._account_social_signals,
            ),
            provider,
            SqliteCollectorSink(self.database),
        )

    async def _rebuild_after_network_failure(self) -> None:
        old_transport = self._transport
        old_onchain = self._onchain_admission
        old_social = self._public_social_signals
        old_account_social = self._account_social_signals
        self._service = None
        self._transport = None
        self._onchain_admission = None
        self._public_social_signals = None
        self._account_social_signals = None
        if old_transport is not None:
            await old_transport.close()
        if old_onchain is not None:
            await old_onchain.close()
        if old_social is not None:
            await old_social.close()
        if old_account_social is not None:
            await old_account_social.close()
        self._service = self._build()
        self._transport_rebuilds += 1

    async def run_forever(self) -> None:
        self.database.set_runtime_state("collector_status", {"state": "starting"})
        try:
            self._service = self._build()
        except Exception as exc:
            self._service = None
            self.database.set_runtime_state(
                "collector_status",
                {"state": "blocked", "reason": f"{type(exc).__name__}: {exc}"[:500]},
            )
        self._env_mtime_ns = ENV_PATH.stat().st_mtime_ns if ENV_PATH.exists() else None
        while not self._stop.is_set():
            runtime_config = self.configuration.runtime_values()
            if self._gmgn_limiter is not None:
                self._gmgn_limiter.requests_per_second = runtime_config["gmgn_global_rps"]
            current_mtime = ENV_PATH.stat().st_mtime_ns if ENV_PATH.exists() else None
            if self._service is None or current_mtime != self._env_mtime_ns:
                try:
                    if self._transport is not None:
                        await self._transport.close()
                    if self._onchain_admission is not None:
                        await self._onchain_admission.close()
                        self._onchain_admission = None
                    if self._public_social_signals is not None:
                        await self._public_social_signals.close()
                        self._public_social_signals = None
                    if self._account_social_signals is not None:
                        await self._account_social_signals.close()
                        self._account_social_signals = None
                    self._service = self._build()
                    self._env_mtime_ns = current_mtime
                except Exception as exc:
                    self.database.set_runtime_state(
                        "collector_status",
                        {"state": "blocked", "reason": f"{type(exc).__name__}: {exc}"[:500]},
                    )
                    await asyncio.sleep(3.0)
                    continue

            requested_limit = min(self.settings.gmgn_trenches_limit, 80)
            rate_limit_remaining = self._rate_limit_remaining()
            if rate_limit_remaining > 0:
                circuit = self.database.get_runtime_state("collector_rate_limit_circuit", {})
                circuit = circuit if isinstance(circuit, dict) else {}
                self.database.set_runtime_state(
                    "collector_status",
                    {
                        "state": "rate_limited",
                        "mode": "monitor_only" if self.monitor_only else "collector",
                        "cycle_state": "backoff",
                        "requested_limit_per_type": requested_limit,
                        "rate_limit_streak": self._rate_limit_streak,
                        "rate_limit_backoff_remaining_seconds": rate_limit_remaining,
                        "next_probe_at": circuit.get("next_probe_at"),
                        "errors": [],
                        "transport_rebuilds": self._transport_rebuilds,
                        "updated_at": utc_now_iso(),
                    },
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=rate_limit_remaining)
                except TimeoutError:
                    pass
                continue

            # A persisted/open rate-limit circuit is half-opened with the
            # production-critical collection path *before* Kline/Regime work.
            # This prevents quota-heavy ancillary calls from consuming the first
            # recovered requests and immediately re-banning discovery.
            if self._rate_limit_streak > 0 and not self.monitor_only:
                probe_started = time.time()
                self._active_cycle_id = uuid.uuid4().hex[:10]
                self.database.set_runtime_state(
                    "collector_status",
                    {
                        "state": "recovering",
                        "mode": "collector",
                        "cycle_state": "half_open_probe",
                        "cycle_id": self._active_cycle_id,
                        "cycle_started_at": utc_now_iso(),
                        "requested_limit_per_type": requested_limit,
                        "rate_limit_streak": self._rate_limit_streak,
                    },
                )
                try:
                    collection = await self._service.collect_once(
                        limit=requested_limit,
                        event_sink=self._record_event,
                    )
                    probe_stats = {
                        "discovered": collection.discovered,
                        "accepted": collection.accepted,
                        "rejected": collection.rejected,
                        "prefilter_rejected": collection.prefilter_rejected,
                        "enrichment_rejected": collection.enrichment_rejected,
                        "duplicates": collection.unfinished_duplicates,
                        "rejection_reasons": dict(collection.rejection_reasons),
                        "type_stats": {key: dict(value) for key, value in collection.type_stats.items()},
                    }
                    self._persist_cycle_snapshot(probe_stats)
                    previous_streak = self._rate_limit_streak
                    self._close_rate_limit_circuit()
                    self.database.set_runtime_state(
                        "collector_status",
                        {
                            "state": "running",
                            "mode": "collector",
                            "cycle_state": "idle",
                            "cycle_id": self._active_cycle_id,
                            "last_cycle_at": utc_now_iso(),
                            "last_cycle_duration_seconds": time.time() - probe_started,
                            "requested_limit_per_type": requested_limit,
                            **probe_stats,
                            "finalized": 0,
                            "errors": [],
                            "transport_rebuilds": self._transport_rebuilds,
                            "rate_limit_recovered": True,
                            "recovered_from_streak": previous_streak,
                        },
                    )
                except Exception as exc:
                    if self._rate_limit_error(exc) is not None:
                        circuit = self._open_rate_limit_circuit(
                            exc, stage="half_open_collection_probe"
                        )
                        self.database.set_runtime_state(
                            "collector_status",
                            {
                                "state": "rate_limited",
                                "mode": "collector",
                                "cycle_state": "backoff",
                                "cycle_id": self._active_cycle_id,
                                "last_cycle_at": utc_now_iso(),
                                "last_cycle_duration_seconds": time.time() - probe_started,
                                "requested_limit_per_type": requested_limit,
                                "rate_limit_streak": self._rate_limit_streak,
                                "next_probe_at": circuit["next_probe_at"],
                                "errors": [{
                                    "stage": "half_open_collection_probe",
                                    "error": f"{type(exc).__name__}: {exc}"[:500],
                                }],
                                "transport_rebuilds": self._transport_rebuilds,
                            },
                        )
                    else:
                        self.database.set_runtime_state(
                            "collector_status",
                            {
                                "state": "degraded",
                                "mode": "collector",
                                "cycle_state": "idle",
                                "cycle_id": self._active_cycle_id,
                                "last_cycle_at": utc_now_iso(),
                                "last_cycle_duration_seconds": time.time() - probe_started,
                                "requested_limit_per_type": requested_limit,
                                "errors": [{
                                    "stage": "half_open_collection_probe",
                                    "error": f"{type(exc).__name__}: {exc}"[:500],
                                }],
                                "transport_rebuilds": self._transport_rebuilds,
                            },
                        )
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(),
                            timeout=max(1.0, min(self.settings.collector_poll_seconds, self._rate_limit_remaining() or self.settings.collector_poll_seconds)),
                        )
                    except TimeoutError:
                        pass
                    continue

                # Recovery succeeded. Give the normal cadence one full interval
                # before reintroducing label/Regime/SOL requests.
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=max(1.0, self.settings.collector_poll_seconds)
                    )
                except TimeoutError:
                    pass
                continue

            started = time.time()
            self._active_cycle_id = uuid.uuid4().hex[:10]
            self._experiment.begin_cycle(self._active_cycle_id, observed_at=int(started))
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
                "prefilter_rejected": 0,
                "enrichment_rejected": 0,
                "duplicates": 0,
                "rejection_reasons": {},
                "type_stats": {},
            }
            finalized = 0
            network_failure = False
            rate_limit_circuit: dict[str, Any] | None = None

            # Freeze a recent SOL/USD observation before any simulated execution.
            # Paper accounting consumes this cache at the actual fee timestamp;
            # a stale/missing FX fact blocks the paper trade instead of repricing
            # it later with a different SOL price.
            try:
                await self._sol_price.refresh(self._service.provider, now_ts=int(time.time()))
            except Exception as exc:
                network_failure = network_failure or _is_network_failure(exc)
                self.database.audit(
                    category="simulation",
                    action="sol_usd_price_refresh_failed",
                    severity="warning",
                    details={"error": f"{type(exc).__name__}: {exc}"[:300]},
                )

            # Refresh aggregate GMGN attention/event state with the existing key
            # pool. Optional endpoint failures only reduce Regime confidence.
            if self._regime_provider is not None:
                try:
                    market_feed = await self._regime_provider.snapshot()
                    self.database.set_runtime_state("gmgn_market_regime_feed", market_feed)
                except Exception as exc:
                    self.database.set_runtime_state(
                        "gmgn_market_regime_feed",
                        {"available": False, "errors": [f"{type(exc).__name__}:market_regime_feed"]},
                    )

            # Position exits are handled by PositionMonitorWorker on its own
            # current-market cadence. Collector remains discovery/label-only.

            try:
                finalization = await self._service.finalize_due()
                finalized = finalization.finalized
                if finalized:
                    self._record_event("label_finalization", {"finalized": finalized})
            except Exception as exc:
                network_failure = network_failure or _is_network_failure(exc)
                message = f"{type(exc).__name__}: {exc}"[:500]
                cycle_errors.append({"stage": "label_finalization", "error": message})
                self._record_event("stage_error", {"stage": "label_finalization", "error": message})
                self.database.audit(
                    category="collector",
                    action="label_finalization_failed",
                    severity="error",
                    details={"error": message},
                )

            experiment_stats: dict[str, Any] = {"state": "inactive"}
            experiment_finalized = 0
            if not self.monitor_only and self._experiment.current() is not None:
                try:
                    experiment_finalized = await self._experiment.finalize_due(self._service.provider)
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"[:500]
                    experiment_stats = {"state": "degraded", "errors": [{"stage": "label_finalization", "error": message}]}
                    self.database.audit(
                        category="collector", action="discovery_experiment_label_failed", severity="warning",
                        details={"cycle_id": self._active_cycle_id, "error": message},
                    )

            if not self.monitor_only:
                try:
                    collection = await self._service.collect_once(
                        limit=requested_limit,
                        event_sink=self._record_event,
                        observation_sink=self._experiment.observe_control,
                    )
                    collection_stats = {
                        "discovered": collection.discovered,
                        "accepted": collection.accepted,
                        "rejected": collection.rejected,
                        "prefilter_rejected": collection.prefilter_rejected,
                        "enrichment_rejected": collection.enrichment_rejected,
                        "duplicates": collection.unfinished_duplicates,
                        "rejection_reasons": dict(collection.rejection_reasons),
                        "type_stats": {key: dict(value) for key, value in collection.type_stats.items()},
                    }
                    self._persist_cycle_snapshot(collection_stats)
                except Exception as exc:
                    network_failure = network_failure or _is_network_failure(exc)
                    message = f"{type(exc).__name__}: {exc}"[:500]
                    cycle_errors.append({"stage": "discovery", "error": message})
                    self._record_event("stage_error", {"stage": "discovery", "error": message})
                    self.database.audit(
                        category="collector",
                        action="discovery_cycle_failed",
                        severity="error",
                        details={"error": message},
                    )
                    if self._rate_limit_error(exc) is not None:
                        rate_limit_circuit = self._open_rate_limit_circuit(
                            exc, stage="discovery"
                        )

            if (
                not self.monitor_only
                and self._experiment.current() is not None
                and rate_limit_circuit is None
                and not network_failure
            ):
                try:
                    trending_stats = await self._experiment.run_trending_cycle(
                        self._service.discovery, self._service.enrichment
                    )
                    experiment_stats = {**trending_stats, "labels_finalized": experiment_finalized}
                except Exception as exc:
                    # Shadow experiment failures are isolated from production Trenches.
                    message = f"{type(exc).__name__}: {exc}"[:500]
                    experiment_stats = {
                        "state": "degraded",
                        "labels_finalized": experiment_finalized,
                        "errors": [{"stage": "trending_shadow", "error": message}],
                    }
                    self.database.audit(
                        category="collector", action="discovery_experiment_cycle_failed", severity="warning",
                        details={"cycle_id": self._active_cycle_id, "error": message},
                    )
            elif not self.monitor_only and self._experiment.current() is not None:
                experiment_stats = {
                    "state": "skipped_production_pressure",
                    "labels_finalized": experiment_finalized,
                    "production_rate_limited": rate_limit_circuit is not None,
                    "production_network_failure": bool(network_failure),
                }
            try:
                self._experiment.finish_cycle()
            except Exception as exc:
                self.database.audit(
                    category="collector", action="discovery_experiment_metric_flush_failed", severity="warning",
                    details={"cycle_id": self._active_cycle_id, "error": f"{type(exc).__name__}: {exc}"[:300]},
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
                    "state": (
                        "rate_limited"
                        if rate_limit_circuit is not None
                        else ("degraded" if cycle_errors else ("monitor_only" if self.monitor_only else "running"))
                    ),
                    "mode": "monitor_only" if self.monitor_only else "collector",
                    "cycle_state": "idle",
                    "cycle_id": self._active_cycle_id,
                    "last_cycle_at": utc_now_iso(),
                    "last_cycle_duration_seconds": elapsed,
                    "requested_limit_per_type": requested_limit,
                    **collection_stats,
                    "finalized": finalized,
                    "errors": cycle_errors,
                    "transport_rebuilds": self._transport_rebuilds,
                    "discovery_experiment": experiment_stats,
                    "rate_limit_streak": self._rate_limit_streak if rate_limit_circuit is not None else 0,
                    "next_probe_at": rate_limit_circuit.get("next_probe_at") if rate_limit_circuit else None,
                },
            )
            if network_failure:
                try:
                    await self._rebuild_after_network_failure()
                    status = self.database.get_runtime_state("collector_status", {})
                    status = status if isinstance(status, dict) else {}
                    self.database.set_runtime_state(
                        "collector_status",
                        {
                            **status,
                            "state": "recovering",
                            "transport_rebuilds": self._transport_rebuilds,
                            "recovery_reason": "gmgn_network_failure",
                            "recovered_at": utc_now_iso(),
                        },
                    )
                    self.database.audit(
                        category="collector",
                        action="gmgn_transport_rebuilt",
                        severity="warning",
                        details={"transport_rebuilds": self._transport_rebuilds},
                    )
                except Exception as rebuild_exc:
                    self._service = None
                    self.database.set_runtime_state(
                        "collector_status",
                        {
                            "state": "blocked",
                            "mode": "monitor_only" if self.monitor_only else "collector",
                            "reason": f"transport_rebuild_failed: {type(rebuild_exc).__name__}: {rebuild_exc}"[:500],
                            "transport_rebuilds": self._transport_rebuilds,
                            "updated_at": utc_now_iso(),
                        },
                    )
            wait_seconds = 1.0 if network_failure else max(1.0, self.settings.collector_poll_seconds - elapsed)
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
        if self._onchain_admission is not None:
            await self._onchain_admission.close()
        if self._public_social_signals is not None:
            await self._public_social_signals.close()
        if self._account_social_signals is not None:
            await self._account_social_signals.close()

    def stop(self) -> None:
        self._stop.set()
