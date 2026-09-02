from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..database import Database, utc_now_iso
from .constants import LabelPolicy
from .discovery import DiscoveryService, TRENDING_ORDER_BY
from .enrichment import EnrichmentService, EnrichmentProvider, merge_sources
from .labels import LabelFinalizer
from .models import CollectedSample, TokenCandidate
from .rate_limit import AsyncRateLimiter


CONTROL_SOURCES = ("trenches:new_creation", "trenches:near_completion")
TRENDING_SOURCES = tuple(f"trending:{name}" for name in TRENDING_ORDER_BY)
ALL_SOURCES = (*CONTROL_SOURCES, *TRENDING_SOURCES)


class DiscoveryExperimentManager:
    """Durable, shadow-only discovery-source experiment.

    Trending observations never enter the production samples table. Production
    Trenches results are observed through CollectorService and copied into this
    ledger only for like-for-like source comparison.
    """

    def __init__(
        self,
        database: Database,
        limiter: AsyncRateLimiter | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.limiter = limiter
        self._clock = clock
        self._cycle_id: str | None = None
        self._cycle_observed_at: int | None = None
        self._api_metrics: dict[str, dict[str, float | int]] = {}
        self._global_weight_start = 0.0

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    def create_or_resume(
        self,
        *,
        duration_seconds: int = 86_400,
        interval: str = "5m",
        limit_per_source: int = 80,
        max_shadow_enrich_per_cycle: int = 8,
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(self._clock())
        current = self.database.fetch_one(
            "SELECT * FROM discovery_experiments WHERE status='active' ORDER BY started_at DESC LIMIT 1"
        )
        if current is not None:
            return current
        experiment_id = f"disc24h_{uuid.uuid4().hex[:12]}"
        payload = {
            "sources": list(ALL_SOURCES),
            "trending_order_by": list(TRENDING_ORDER_BY),
            "direction": "desc",
            "chain": "sol",
            "shadow_only": True,
            "global_weighted_rps_unchanged": True,
            **dict(config or {}),
        }
        self.database.execute(
            """
            INSERT INTO discovery_experiments(
                id,started_at,ends_at,status,interval,limit_per_source,
                max_shadow_enrich_per_cycle,config_json,created_at
            ) VALUES(?,?,?,'active',?,?,?,?,?)
            """,
            (
                experiment_id,
                now,
                now + int(duration_seconds),
                interval,
                int(limit_per_source),
                int(max_shadow_enrich_per_cycle),
                self._json(payload),
                utc_now_iso(),
            ),
        )
        self.database.audit(
            category="collector",
            action="discovery_experiment_started",
            entity_type="discovery_experiment",
            entity_id=experiment_id,
            details={"started_at": now, "ends_at": now + int(duration_seconds), **payload},
        )
        return self.database.fetch_one(
            "SELECT * FROM discovery_experiments WHERE id=?", (experiment_id,)
        ) or {}

    def current(self) -> dict[str, Any] | None:
        return self.database.fetch_one(
            "SELECT * FROM discovery_experiments WHERE status='active' ORDER BY started_at DESC LIMIT 1"
        )

    def is_collecting(self) -> bool:
        experiment = self.current()
        return bool(experiment and int(self._clock()) < int(experiment["ends_at"]))

    def _maybe_complete(self) -> None:
        experiment = self.current()
        if not experiment or int(self._clock()) < int(experiment["ends_at"]):
            return
        pending = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM discovery_experiment_samples WHERE experiment_id=? AND label_status='pending'",
            (experiment["id"],),
        ) or {}
        if int(pending.get("count") or 0) > 0:
            return
        self.database.execute(
            "UPDATE discovery_experiments SET status='completed', completed_at=? WHERE id=? AND status='active'",
            (utc_now_iso(), experiment["id"]),
        )
        self.database.audit(
            category="collector",
            action="discovery_experiment_completed",
            entity_type="discovery_experiment",
            entity_id=str(experiment["id"]),
            details=self.summary(str(experiment["id"])),
        )

    def begin_cycle(self, cycle_id: str, *, observed_at: int | None = None) -> None:
        self._cycle_id = str(cycle_id)
        self._cycle_observed_at = int(observed_at or self._clock())
        self._api_metrics = {}
        if self.limiter is not None:
            self._global_weight_start = float(self.limiter.snapshot()["total_weight_acquired"])
        else:
            self._global_weight_start = 0.0

    def record_api_event(self, payload: Mapping[str, Any]) -> None:
        if not self.current() or not self._cycle_id:
            return
        path = str(payload.get("path") or "unknown")
        metric = self._api_metrics.setdefault(
            path,
            {
                "request_count": 0,
                "route_weight": float(payload.get("route_weight") or 0),
                "weighted_units": 0.0,
                "rate_limited_count": 0,
                "failure_count": 0,
                "total_latency_ms": 0.0,
                "slot_weighted_units": {},
            },
        )
        metric["request_count"] = int(metric["request_count"]) + 1
        weight = float(payload.get("route_weight") or metric["route_weight"] or 0)
        metric["route_weight"] = weight
        metric["weighted_units"] = float(metric["weighted_units"]) + weight
        metric["total_latency_ms"] = float(metric["total_latency_ms"]) + float(payload.get("latency_ms") or 0)
        slot = payload.get("slot")
        if slot is not None:
            slot_weights = metric["slot_weighted_units"]
            if isinstance(slot_weights, dict):
                key = str(int(slot))
                slot_weights[key] = float(slot_weights.get(key) or 0.0) + weight
        if bool(payload.get("rate_limited")):
            metric["rate_limited_count"] = int(metric["rate_limited_count"]) + 1
        if str(payload.get("outcome") or "success") != "success":
            metric["failure_count"] = int(metric["failure_count"]) + 1

    def finish_cycle(self) -> None:
        experiment = self.current()
        if not experiment or not self._cycle_id:
            self._cycle_id = None
            return
        observed_at = int(self._cycle_observed_at or self._clock())
        if self.limiter is not None:
            end_weight = float(self.limiter.snapshot()["total_weight_acquired"])
            self._api_metrics["__shared_global__"] = {
                "request_count": 0,
                "route_weight": 0.0,
                "weighted_units": max(0.0, end_weight - self._global_weight_start),
                "rate_limited_count": 0,
                "failure_count": 0,
                "total_latency_ms": 0.0,
                "slot_weighted_units": {},
            }
        for route_key, metric in self._api_metrics.items():
            self.database.execute(
                """
                INSERT INTO discovery_experiment_api_metrics(
                    experiment_id,cycle_id,observed_at,route_key,request_count,route_weight,
                    weighted_units,rate_limited_count,failure_count,total_latency_ms,payload_json,recorded_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(experiment_id,cycle_id,route_key) DO UPDATE SET
                    request_count=excluded.request_count, route_weight=excluded.route_weight,
                    weighted_units=excluded.weighted_units, rate_limited_count=excluded.rate_limited_count,
                    failure_count=excluded.failure_count, total_latency_ms=excluded.total_latency_ms,
                    payload_json=excluded.payload_json, recorded_at=excluded.recorded_at
                """,
                (
                    experiment["id"], self._cycle_id, observed_at, route_key,
                    int(metric["request_count"]), float(metric["route_weight"]),
                    float(metric["weighted_units"]), int(metric["rate_limited_count"]),
                    int(metric["failure_count"]), float(metric["total_latency_ms"]),
                    self._json({
                        "global_rps": self.limiter.requests_per_second if self.limiter else None,
                        "slot_weighted_units": metric.get("slot_weighted_units", {}),
                    }),
                    utc_now_iso(),
                ),
            )
        status = self.summary(str(experiment["id"]))
        self.database.set_runtime_state("discovery_experiment_status", status)
        self._cycle_id = None
        self._api_metrics = {}
        self._control_samples = {}
        self._maybe_complete()

    def _upsert_observation(
        self,
        *,
        source_key: str,
        source_kind: str,
        address: str,
        source_rank: int | None = None,
        raw: Mapping[str, Any] | None = None,
        observed_at: int | None = None,
        prefilter_accepted: bool | None = None,
        outcome: str = "discovered",
        reasons: Sequence[str] = (),
        sample_id: int | None = None,
    ) -> None:
        experiment = self.current()
        if not experiment or not self._cycle_id or not address:
            return
        observation_time = int(observed_at or self._cycle_observed_at or self._clock())
        existing = self.database.fetch_one(
            """
            SELECT raw_json,source_rank,prefilter_accepted FROM discovery_experiment_observations
            WHERE experiment_id=? AND cycle_id=? AND source_key=? AND address=?
            """,
            (experiment["id"], self._cycle_id, source_key, address),
        )
        old_raw: dict[str, Any] = {}
        if existing:
            try:
                decoded = json.loads(existing.get("raw_json") or "{}")
                old_raw = decoded if isinstance(decoded, dict) else {}
            except (TypeError, json.JSONDecodeError):
                old_raw = {}
        merged_raw = {**old_raw, **dict(raw or {})}
        rank_value = source_rank if source_rank is not None else (existing or {}).get("source_rank")
        if prefilter_accepted is None:
            prefilter_value = (existing or {}).get("prefilter_accepted")
        else:
            prefilter_value = int(bool(prefilter_accepted))
        self.database.execute(
            """
            INSERT INTO discovery_experiment_observations(
                experiment_id,cycle_id,observed_at,source_key,source_kind,address,source_rank,
                prefilter_accepted,outcome,rejection_reasons_json,raw_json,experiment_sample_id,recorded_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(experiment_id,cycle_id,source_key,address) DO UPDATE SET
                source_rank=COALESCE(excluded.source_rank,source_rank),
                prefilter_accepted=COALESCE(excluded.prefilter_accepted,prefilter_accepted),
                outcome=excluded.outcome,rejection_reasons_json=excluded.rejection_reasons_json,
                raw_json=excluded.raw_json,
                experiment_sample_id=COALESCE(excluded.experiment_sample_id,experiment_sample_id),
                recorded_at=excluded.recorded_at
            """,
            (
                experiment["id"], self._cycle_id, observation_time, source_key, source_kind, address,
                rank_value, prefilter_value, outcome, self._json(list(reasons)), self._json(merged_raw),
                sample_id, utc_now_iso(),
            ),
        )

    def _has_pending(self, source_key: str, address: str) -> bool:
        experiment = self.current()
        if not experiment:
            return False
        row = self.database.fetch_one(
            """
            SELECT 1 AS found FROM discovery_experiment_samples
            WHERE experiment_id=? AND source_key=? AND address=? AND label_status='pending' LIMIT 1
            """,
            (experiment["id"], source_key, address),
        )
        return row is not None

    def _insert_sample(
        self,
        source_key: str,
        source_kind: str,
        sample: CollectedSample,
        *,
        production_sample: bool,
    ) -> int | None:
        experiment = self.current()
        if not experiment or not self._cycle_id or self._has_pending(source_key, sample.address):
            return None
        now = utc_now_iso()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO discovery_experiment_samples(
                    experiment_id,cycle_id,source_key,source_kind,address,entry_time,entry_price,
                    launchpad,liquidity,features_json,raw_json,production_sample,label_status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)
                """,
                (
                    experiment["id"], self._cycle_id, source_key, source_kind, sample.address,
                    int(sample.entry_time), float(sample.entry_price), sample.launchpad, float(sample.liquidity),
                    self._json(dict(sample.features)), self._json(dict(sample.source)),
                    int(production_sample), now, now,
                ),
            )
            return int(cursor.lastrowid)

    def observe_control(self, action: str, payload: Mapping[str, object]) -> None:
        if not self.is_collecting() or not self._cycle_id:
            return
        source_key = str(payload.get("source_key") or "")
        address = str(payload.get("address") or "")
        if source_key not in CONTROL_SOURCES or not address:
            return
        if action == "candidate_discovered":
            self._upsert_observation(
                source_key=source_key, source_kind="trenches", address=address,
                source_rank=int(payload.get("source_rank") or 0) or None,
                raw=payload.get("raw") if isinstance(payload.get("raw"), Mapping) else None,
            )
        elif action == "candidate_duplicate":
            self._upsert_observation(source_key=source_key, source_kind="trenches", address=address, outcome="production_pending_duplicate")
        elif action == "candidate_prefilter_passed":
            self._upsert_observation(source_key=source_key, source_kind="trenches", address=address, prefilter_accepted=True, outcome="prefilter_passed")
        elif action == "candidate_prefilter_rejected":
            self._upsert_observation(
                source_key=source_key, source_kind="trenches", address=address,
                prefilter_accepted=False, outcome="prefilter_rejected",
                reasons=tuple(str(x) for x in (payload.get("reasons") or [])),
            )
        elif action == "candidate_enrichment_rejected":
            self._upsert_observation(
                source_key=source_key, source_kind="trenches", address=address,
                prefilter_accepted=True, outcome="enrichment_rejected",
                reasons=tuple(str(x) for x in (payload.get("reasons") or [])),
            )
        elif action == "candidate_accepted" and isinstance(payload.get("sample"), CollectedSample):
            sample = payload["sample"]
            sample_id = self._insert_sample(source_key, "trenches", sample, production_sample=True)
            self._upsert_observation(
                source_key=source_key, source_kind="trenches", address=address,
                prefilter_accepted=True, outcome="accepted", sample_id=sample_id,
            )

    async def run_trending_cycle(
        self,
        discovery: DiscoveryService,
        enrichment: EnrichmentService,
    ) -> dict[str, Any]:
        experiment = self.current()
        if not experiment or not self.is_collecting() or not self._cycle_id:
            return {"state": "inactive"}
        interval = str(experiment["interval"])
        limit = int(experiment["limit_per_source"])
        cap = int(experiment["max_shadow_enrich_per_cycle"])
        source_candidates: dict[str, list[TokenCandidate]] = {}
        errors: list[str] = []
        for order_by in TRENDING_ORDER_BY:
            source_key = f"trending:{order_by}"
            try:
                candidates = await discovery.discover_trending(order_by, interval=interval, limit=limit)
            except Exception as exc:
                source_candidates[source_key] = []
                errors.append(f"{source_key}:{type(exc).__name__}")
                continue
            source_observed_at = int(self._clock())
            stamped: list[TokenCandidate] = []
            for rank, candidate in enumerate(candidates, start=1):
                raw = {**dict(candidate.raw), "_experiment_source_observed_at": source_observed_at}
                stamped_candidate = TokenCandidate(candidate.address, candidate.token_type, raw)
                stamped.append(stamped_candidate)
                self._upsert_observation(
                    source_key=source_key, source_kind="trending", address=candidate.address,
                    source_rank=rank, raw=raw, observed_at=source_observed_at,
                )
            source_candidates[source_key] = stamped

        grouped: dict[str, list[tuple[str, int, TokenCandidate]]] = defaultdict(list)
        for source_key, candidates in source_candidates.items():
            for rank, candidate in enumerate(candidates, start=1):
                if self._has_pending(source_key, candidate.address):
                    self._upsert_observation(
                        source_key=source_key, source_kind="trending", address=candidate.address,
                        source_rank=rank, outcome="source_pending_duplicate",
                    )
                    continue
                decision = enrichment.prefilter(candidate)
                if not decision.accepted:
                    self._upsert_observation(
                        source_key=source_key, source_kind="trending", address=candidate.address,
                        source_rank=rank, prefilter_accepted=False, outcome="prefilter_rejected",
                        reasons=decision.reasons,
                    )
                    continue
                self._upsert_observation(
                    source_key=source_key, source_kind="trending", address=candidate.address,
                    source_rank=rank, prefilter_accepted=True, outcome="prefilter_passed",
                )
                grouped[candidate.address].append((source_key, rank, candidate))

        rotation = int(self._cycle_observed_at or 0) // 120 % max(1, len(TRENDING_SOURCES))
        source_priority = {
            TRENDING_SOURCES[(rotation + offset) % len(TRENDING_SOURCES)]: offset
            for offset in range(len(TRENDING_SOURCES))
        }
        ordered_addresses = sorted(
            grouped,
            key=lambda address: min(
                (source_priority.get(source_key, 99), rank)
                for source_key, rank, _ in grouped[address]
            ),
        )
        selected_addresses = set(ordered_addresses[:cap])
        for address in ordered_addresses[cap:]:
            for source_key, rank, _ in grouped[address]:
                self._upsert_observation(
                    source_key=source_key, source_kind="trending", address=address,
                    source_rank=rank, prefilter_accepted=True, outcome="budget_censored",
                )

        accepted = rejected = 0
        for address in ordered_addresses[:cap]:
            memberships = grouped[address]
            merged_raw: Mapping[str, Any] = merge_sources(*(item[2].raw for item in memberships))
            representative = TokenCandidate(address, "trending", merged_raw)
            # Entry-time facts must be no earlier than the actual Trending observation.
            enrichment_time = int(self._clock())
            result = await enrichment.enrich(representative, now_ts=enrichment_time)
            if result.sample is None:
                rejected += 1
                for source_key, rank, _ in memberships:
                    self._upsert_observation(
                        source_key=source_key, source_kind="trending", address=address,
                        source_rank=rank, prefilter_accepted=True, outcome="enrichment_rejected",
                        reasons=result.decision.reasons,
                    )
                continue
            sample = result.sample
            accepted += 1
            for source_key, rank, candidate in memberships:
                source_sample = CollectedSample(
                    address=sample.address,
                    token_type="trending",
                    entry_time=sample.entry_time,
                    entry_price=sample.entry_price,
                    launchpad=sample.launchpad,
                    liquidity=sample.liquidity,
                    features=dict(sample.features),
                    age_minutes=sample.age_minutes,
                    holder_count=sample.holder_count,
                    feature_schema_version=sample.feature_schema_version,
                    feature_snapshot_at=sample.feature_snapshot_at,
                    source=merge_sources(candidate.raw, sample.source),
                )
                sample_id = self._insert_sample(source_key, "trending", source_sample, production_sample=False)
                self._upsert_observation(
                    source_key=source_key, source_kind="trending", address=address,
                    source_rank=rank, prefilter_accepted=True,
                    outcome="accepted" if sample_id is not None else "source_pending_duplicate",
                    sample_id=sample_id,
                )
        return {
            "state": "collecting",
            "source_returned": {key: len(value) for key, value in source_candidates.items()},
            "prefilter_unique": len(grouped),
            "deep_enrich_budget": cap,
            "deep_enrich_selected": len(selected_addresses),
            "accepted_unique": accepted,
            "enrichment_rejected_unique": rejected,
            "budget_censored_unique": max(0, len(ordered_addresses) - cap),
            "errors": errors,
        }

    async def finalize_due(self, provider: EnrichmentProvider, *, now_ts: int | None = None) -> int:
        experiment = self.current()
        if not experiment:
            return 0
        now = int(now_ts or self._clock())
        policy = LabelPolicy()
        # Control samples reuse the production label instead of issuing duplicate Kline calls.
        control_rows = self.database.fetch_all(
            """
            SELECT e.id,e.address,e.entry_time,s.tag,s.label_max_price_ratio,s.label_min_price_ratio,
                   s.label_final_close_ratio,s.first_take_profit_at,s.first_stop_loss_at,s.exit_reason,
                   s.same_bar_conflict,s.gross_return_rate,s.label_version
            FROM discovery_experiment_samples e
            JOIN samples s ON s.chain='sol' AND s.address=e.address AND s.entry_time=e.entry_time
            WHERE e.experiment_id=? AND e.production_sample=1 AND e.label_status='pending'
              AND s.label_status='mature'
            """,
            (experiment["id"],),
        )
        finalized = 0
        for row in control_rows:
            self._save_label_from_mapping(row)
            finalized += 1

        due = self.database.fetch_all(
            """
            SELECT * FROM discovery_experiment_samples
            WHERE experiment_id=? AND production_sample=0 AND label_status='pending' AND entry_time<=?
            ORDER BY entry_time LIMIT 300
            """,
            (experiment["id"], now - policy.window_seconds),
        )
        by_address: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in due:
            by_address[str(row["address"])].append(row)
        finalizer = LabelFinalizer(policy)
        for address, rows in by_address.items():
            from_ts = min(int(row["entry_time"]) for row in rows) - policy.history_seconds
            to_ts = max(int(row["entry_time"]) for row in rows) + policy.window_seconds
            klines = await provider.klines(address, from_ts, to_ts)
            for row in rows:
                sample = CollectedSample(
                    address=address,
                    token_type="trending",
                    entry_time=int(row["entry_time"]),
                    entry_price=float(row["entry_price"]),
                    launchpad=str(row.get("launchpad") or "unknown"),
                    liquidity=float(row.get("liquidity") or 0),
                    features=json.loads(row.get("features_json") or "{}"),
                    source=json.loads(row.get("raw_json") or "{}"),
                )
                result = finalizer.finalize(sample, klines)
                self._save_label_result(int(row["id"]), result)
                finalized += 1
        self._maybe_complete()
        return finalized

    def _save_label_from_mapping(self, row: Mapping[str, Any]) -> None:
        self.database.execute(
            """
            UPDATE discovery_experiment_samples SET tag=?,label_max_price_ratio=?,label_min_price_ratio=?,
                label_final_close_ratio=?,first_take_profit_at=?,first_stop_loss_at=?,exit_reason=?,
                same_bar_conflict=?,gross_return_rate=?,label_version=?,label_status='mature',updated_at=?
            WHERE id=?
            """,
            (
                row.get("tag"), row.get("label_max_price_ratio"), row.get("label_min_price_ratio"),
                row.get("label_final_close_ratio"), row.get("first_take_profit_at"), row.get("first_stop_loss_at"),
                row.get("exit_reason"), int(row.get("same_bar_conflict") or 0), row.get("gross_return_rate"),
                row.get("label_version"), utc_now_iso(), row["id"],
            ),
        )

    def _save_label_result(self, sample_id: int, result: Any) -> None:
        policy = LabelPolicy()
        self.database.execute(
            """
            UPDATE discovery_experiment_samples SET tag=?,label_max_price_ratio=?,label_min_price_ratio=?,
                label_final_close_ratio=?,first_take_profit_at=?,first_stop_loss_at=?,exit_reason=?,
                same_bar_conflict=?,gross_return_rate=?,label_version=?,label_status='mature',updated_at=?
            WHERE id=?
            """,
            (
                result.tag, result.max_price_ratio, result.min_price_ratio, result.final_close_ratio,
                result.first_take_profit_at, result.first_stop_loss_at, result.exit_reason,
                int(result.first_take_profit_at is not None and result.first_take_profit_at == result.first_stop_loss_at),
                policy.take_profit_ratio - 1.0 if result.tag == 1 else policy.stop_loss_ratio - 1.0,
                result.label_version, utc_now_iso(), sample_id,
            ),
        )

    def summary(self, experiment_id: str | None = None) -> dict[str, Any]:
        experiment = (
            self.database.fetch_one("SELECT * FROM discovery_experiments WHERE id=?", (experiment_id,))
            if experiment_id
            else self.current()
        )
        if not experiment:
            return {"state": "none"}
        exp_id = str(experiment["id"])
        now = int(self._clock())
        source_rows: list[dict[str, Any]] = []
        winner_union_rows = self.database.fetch_all(
            "SELECT DISTINCT address FROM discovery_experiment_samples WHERE experiment_id=? AND label_status='mature' AND tag=1",
            (exp_id,),
        )
        winner_union = {str(row["address"]) for row in winner_union_rows}
        trench_observed = {
            str(row["address"])
            for row in self.database.fetch_all(
                "SELECT DISTINCT address FROM discovery_experiment_observations WHERE experiment_id=? AND source_kind='trenches'",
                (exp_id,),
            )
        }
        for source_key in ALL_SOURCES:
            observation = self.database.fetch_one(
                """
                SELECT COUNT(*) AS observations,COUNT(DISTINCT address) AS raw_unique,
                       SUM(CASE WHEN prefilter_accepted=1 THEN 1 ELSE 0 END) AS prefilter_passed,
                       SUM(CASE WHEN outcome='accepted' THEN 1 ELSE 0 END) AS accepted_observations,
                       SUM(CASE WHEN outcome='budget_censored' THEN 1 ELSE 0 END) AS budget_censored,
                       SUM(CASE WHEN outcome='enrichment_rejected' THEN 1 ELSE 0 END) AS enrichment_rejected
                FROM discovery_experiment_observations WHERE experiment_id=? AND source_key=?
                """,
                (exp_id, source_key),
            ) or {}
            sample = self.database.fetch_one(
                """
                SELECT COUNT(*) AS samples,
                       SUM(CASE WHEN label_status='mature' THEN 1 ELSE 0 END) AS mature,
                       SUM(CASE WHEN label_status='mature' AND tag=1 THEN 1 ELSE 0 END) AS positives
                FROM discovery_experiment_samples WHERE experiment_id=? AND source_key=?
                """,
                (exp_id, source_key),
            ) or {}
            mature = int(sample.get("mature") or 0)
            positives = int(sample.get("positives") or 0)
            negatives = max(0, mature - positives)
            profit_units = 3 * positives - negatives
            source_winners = {
                str(row["address"])
                for row in self.database.fetch_all(
                    "SELECT DISTINCT address FROM discovery_experiment_samples WHERE experiment_id=? AND source_key=? AND label_status='mature' AND tag=1",
                    (exp_id, source_key),
                )
            }
            source_addresses = {
                str(row["address"])
                for row in self.database.fetch_all(
                    "SELECT DISTINCT address FROM discovery_experiment_observations WHERE experiment_id=? AND source_key=?",
                    (exp_id, source_key),
                )
            }
            same_cycle_overlap = int((self.database.fetch_one(
                """
                SELECT COUNT(DISTINCT o.address) AS count
                FROM discovery_experiment_observations o
                WHERE o.experiment_id=? AND o.source_key=? AND EXISTS(
                    SELECT 1 FROM discovery_experiment_observations t
                    WHERE t.experiment_id=o.experiment_id AND t.cycle_id=o.cycle_id
                      AND t.address=o.address AND t.source_kind='trenches'
                )
                """, (exp_id, source_key)
            ) or {}).get("count") or 0)
            missed_at_entry = int((self.database.fetch_one(
                """
                SELECT COUNT(*) AS count FROM discovery_experiment_samples s
                WHERE s.experiment_id=? AND s.source_key=? AND s.label_status='mature' AND s.tag=1
                  AND NOT EXISTS(
                    SELECT 1 FROM discovery_experiment_observations t
                    WHERE t.experiment_id=s.experiment_id AND t.source_kind='trenches'
                      AND t.address=s.address AND t.observed_at<=s.entry_time
                  )
                """, (exp_id, source_key)
            ) or {}).get("count") or 0)
            source_rows.append(
                {
                    "source_key": source_key,
                    **{key: int(observation.get(key) or 0) for key in (
                        "observations", "raw_unique", "prefilter_passed", "accepted_observations",
                        "budget_censored", "enrichment_rejected",
                    )},
                    "samples": int(sample.get("samples") or 0),
                    "mature": mature,
                    "positives": positives,
                    "positive_rate": positives / mature if mature else None,
                    "profit_units_3_to_1": profit_units,
                    "J_trade_all": profit_units / positives if positives else None,
                    "winner_addresses": len(source_winners),
                    "winner_capture_vs_union": len(source_winners) / len(winner_union) if winner_union else None,
                    "raw_overlap_with_trenches": len(source_addresses & trench_observed),
                    "same_cycle_overlap_with_trenches": same_cycle_overlap,
                    "winner_not_seen_by_trenches": len(source_winners - trench_observed),
                    "winner_not_seen_by_trenches_by_entry": missed_at_entry,
                }
            )
        api_rows = self.database.fetch_all(
            """
            SELECT route_key,SUM(request_count) AS requests,SUM(weighted_units) AS weighted_units,
                   SUM(rate_limited_count) AS rate_limited,SUM(failure_count) AS failures,
                   SUM(total_latency_ms) AS latency_ms
            FROM discovery_experiment_api_metrics WHERE experiment_id=? GROUP BY route_key ORDER BY route_key
            """,
            (exp_id,),
        )
        slot_weighted_units: dict[str, float] = defaultdict(float)
        for raw_row in self.database.fetch_all(
            "SELECT payload_json FROM discovery_experiment_api_metrics WHERE experiment_id=?", (exp_id,)
        ):
            try:
                payload = json.loads(raw_row.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            for slot, weight in (payload.get("slot_weighted_units") or {}).items():
                slot_weighted_units[str(slot)] += float(weight or 0.0)

        return {
            "id": exp_id,
            "state": str(experiment["status"]),
            "collecting": str(experiment["status"]) == "active" and now < int(experiment["ends_at"]),
            "started_at": int(experiment["started_at"]),
            "ends_at": int(experiment["ends_at"]),
            "seconds_remaining": max(0, int(experiment["ends_at"]) - now),
            "label_grace_pending": int((self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM discovery_experiment_samples WHERE experiment_id=? AND label_status='pending'",
                (exp_id,),
            ) or {}).get("count") or 0),
            "interval": experiment["interval"],
            "limit_per_source": int(experiment["limit_per_source"]),
            "max_shadow_enrich_per_cycle": int(experiment["max_shadow_enrich_per_cycle"]),
            "winner_union_addresses": len(winner_union),
            "sources": source_rows,
            "collector_api_slot_weighted_units": dict(sorted(slot_weighted_units.items())),
            "api": [
                {
                    "route_key": row["route_key"],
                    "requests": int(row.get("requests") or 0),
                    "weighted_units": float(row.get("weighted_units") or 0),
                    "rate_limited": int(row.get("rate_limited") or 0),
                    "failures": int(row.get("failures") or 0),
                    "average_latency_ms": (
                        float(row.get("latency_ms") or 0) / int(row.get("requests") or 1)
                        if int(row.get("requests") or 0) else None
                    ),
                }
                for row in api_rows
            ],
        }
