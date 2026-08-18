from __future__ import annotations

import json
import math
import random
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ..collector.constants import FEATURE_SCHEMA_VERSION
from ..config import Settings, get_settings
from ..database import Database, utc_now_iso
from ..ml.economics import LOSS_UNITS, WIN_UNITS
from .regime import MarketRegimeService

POLICY_VERSION = "adaptive_regime_bandit_v2_shadow_gate"
ACTIONS = ("DEFENSIVE", "NEUTRAL", "EXPANSIVE")
# These are deliberately small candidate actions.  They are evaluated in full-
# information shadow replay before the system is allowed to apply them.
ACTION_DELTA = {"DEFENSIVE": 0.35, "NEUTRAL": 0.0, "EXPANSIVE": -0.20}
MIN_FEEDBACK_CLUSTERS = 120
MIN_CHANGED_CLUSTERS = 40
DISABLE_LCB = -0.05


def adaptive_threshold(base_threshold: float, delta_logit: float) -> float:
    base = min(1.0 - 1e-9, max(1e-9, float(base_threshold)))
    logit = math.log(base / (1.0 - base)) + float(delta_logit)
    return 1.0 / (1.0 + math.exp(-logit))


@dataclass(frozen=True, slots=True)
class AdaptiveDecision:
    id: int
    interval_start: int
    regime_snapshot_id: int | None
    action: str
    delta_logit: float
    propensity: float
    exploration: bool
    confidence: float
    policy_version: str
    reason: str


@dataclass(frozen=True, slots=True)
class EvidenceStatus:
    ready: bool
    feedback_rows: int
    unique_sample_clusters: int
    changed_clusters: int
    development_mean_excess: float | None
    certification_mean_excess: float | None
    certification_lcb95: float | None
    per_strategy_certification_mean: Mapping[str, float]
    reason: str


class AdaptivePolicyService:
    """Safety-gated contextual policy around the three model strategies.

    The regime mapper always produces a *shadow recommendation*.  Actual model
    thresholds remain neutral until chronological full-information replay on the
    new feature generation certifies that the frozen mapping improves the +4/-1
    utility proxy.  ``rules_only`` never calls this service.

    The 5% exploration mechanism exists for future contextual-bandit learning,
    but it additionally requires a second persisted evidence flag and therefore
    cannot turn itself on merely because an environment variable was changed.
    """

    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()
        self.regime = MarketRegimeService(database)

    def _from_row(self, row: Mapping[str, Any]) -> AdaptiveDecision:
        return AdaptiveDecision(
            id=int(row["id"]),
            interval_start=int(row["interval_start"]),
            regime_snapshot_id=int(row["regime_snapshot_id"]) if row.get("regime_snapshot_id") is not None else None,
            action=str(row["action"]),
            delta_logit=float(row["delta_logit"]),
            propensity=float(row["propensity"]),
            exploration=bool(row["exploration"]),
            confidence=float(row["confidence"]),
            policy_version=str(row["policy_version"]),
            reason=str(row.get("reason") or ""),
        )

    def _shadow_recommendation(self) -> tuple[str, str, float, int | None]:
        latest = self.regime.latest()
        snapshots = int(
            (self.database.fetch_one("SELECT COUNT(*) AS n FROM market_regime_snapshots") or {}).get("n") or 0
        )
        confidence = latest.confidence if latest else 0.0
        if not self.settings.adaptive_policy_enabled:
            return "NEUTRAL", "adaptive_disabled", confidence, latest.id if latest else None
        if latest is None:
            return "NEUTRAL", "regime_unavailable", 0.0, None
        if snapshots < self.settings.adaptive_min_regime_snapshots:
            return (
                "NEUTRAL",
                f"regime_warmup:{snapshots}/{self.settings.adaptive_min_regime_snapshots}",
                confidence,
                latest.id,
            )
        if latest.confidence < self.settings.adaptive_min_confidence:
            return "NEUTRAL", f"low_confidence:{latest.confidence:.3f}", confidence, latest.id
        if latest.label == "cold":
            return "DEFENSIVE", "high_confidence_cold_regime", confidence, latest.id
        if latest.label == "hot":
            return "EXPANSIVE", "high_confidence_hot_regime", confidence, latest.id
        return "NEUTRAL", "neutral_regime", confidence, latest.id

    @staticmethod
    def _recommended_action_from_reason(reason: str, applied_action: str) -> str:
        text = str(reason or "")
        if text.startswith("shadow_only:"):
            parts = text.split(":", 2)
            if len(parts) >= 2 and parts[1] in ACTIONS:
                return parts[1]
        if text.startswith("safe_exploration:"):
            parts = text.split(":", 2)
            if len(parts) >= 2 and parts[1] in ACTIONS:
                return parts[1]
        return applied_action if applied_action in ACTIONS else "NEUTRAL"

    def decision(self, *, now_ts: int | None = None) -> AdaptiveDecision:
        now = int(now_ts or time.time())
        interval_seconds = self.settings.adaptive_action_interval_minutes * 60
        interval_start = now - now % interval_seconds
        existing = self.database.fetch_one(
            "SELECT * FROM adaptive_policy_decisions WHERE interval_start=?", (interval_start,)
        )
        if existing:
            return self._from_row(existing)

        recommended, recommendation_reason, confidence, regime_snapshot_id = self._shadow_recommendation()
        policy_ready = self.database.get_runtime_state("adaptive_policy_ready", False) is True
        if policy_ready:
            action = recommended
            reason = recommendation_reason
        else:
            action = "NEUTRAL"
            reason = f"shadow_only:{recommended}:{recommendation_reason}"

        exploration = False
        propensity = 1.0
        exploration_ready = self.database.get_runtime_state("adaptive_exploration_ready", False) is True
        rate = min(0.05, max(0.0, float(self.settings.adaptive_exploration_rate)))
        if policy_ready and exploration_ready and rate > 0:
            if random.random() < rate:
                alternatives = [item for item in ACTIONS if item != recommended]
                action = random.choice(alternatives)
                exploration = True
                propensity = rate / len(alternatives)
                reason = f"safe_exploration:{recommended}:{recommendation_reason}"
            else:
                propensity = 1.0 - rate

        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO adaptive_policy_decisions(
                    interval_start,regime_snapshot_id,action,delta_logit,propensity,
                    exploration,confidence,policy_version,reason,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    interval_start,
                    regime_snapshot_id,
                    action,
                    ACTION_DELTA[action],
                    propensity,
                    int(exploration),
                    confidence,
                    POLICY_VERSION,
                    reason,
                    utc_now_iso(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM adaptive_policy_decisions WHERE interval_start=?", (interval_start,)
            ).fetchone()
        assert row is not None
        return self._from_row(dict(row))

    @staticmethod
    def effective_threshold(base_threshold: float, decision: AdaptiveDecision) -> float:
        return adaptive_threshold(base_threshold, decision.delta_logit)

    def settle_feedback(self, limit: int = 500) -> int:
        rows = self.database.fetch_all(
            """
            SELECT p.id AS prediction_id,p.selected,p.neutral_selected,p.sample_id,p.strategy_key,
                   p.probability,p.base_threshold,p.adaptive_action,p.action_propensity,
                   s.tag,s.feature_schema_version,s.entry_time,
                   d.id AS decision_id,d.reason AS decision_reason,d.action AS decision_action
            FROM predictions p
            JOIN samples s ON s.id=p.sample_id
            LEFT JOIN adaptive_policy_feedback f ON f.prediction_id=p.id
            LEFT JOIN adaptive_policy_decisions d ON d.id=(
                SELECT d2.id FROM adaptive_policy_decisions d2
                WHERE d2.interval_start<=s.entry_time ORDER BY d2.interval_start DESC LIMIT 1
            )
            WHERE f.prediction_id IS NULL AND s.label_status='mature' AND s.tag IN (0,1)
              AND s.feature_schema_version=?
              AND p.strategy_key IN ('model_1','model_2','model_3')
            ORDER BY s.entry_time,p.id LIMIT ?
            """,
            (FEATURE_SCHEMA_VERSION, limit),
        )
        settled = 0
        with self.database.transaction(immediate=True) as connection:
            for row in rows:
                tag = int(row["tag"])
                utility = WIN_UNITS if tag == 1 else -LOSS_UNITS
                adaptive_reward = utility if int(row.get("selected") or 0) else 0.0
                neutral_reward = utility if int(row.get("neutral_selected") or 0) else 0.0

                recommended = self._recommended_action_from_reason(
                    str(row.get("decision_reason") or ""), str(row.get("decision_action") or "NEUTRAL")
                )
                base_threshold = float(row.get("base_threshold") or 0.0)
                probability = float(row.get("probability") or 0.0)
                shadow_threshold = adaptive_threshold(base_threshold, ACTION_DELTA[recommended])
                shadow_selected = probability >= shadow_threshold
                shadow_reward = utility if shadow_selected else 0.0
                shadow_excess = shadow_reward - neutral_reward

                pnl_row = connection.execute(
                    """
                    SELECT net_pnl_usd FROM positions
                    WHERE sample_id=? AND strategy_key=? AND account_kind='simulation' AND status='closed'
                    ORDER BY exit_time DESC LIMIT 1
                    """,
                    (int(row["sample_id"]), str(row["strategy_key"])),
                ).fetchone()
                actual_pnl = float(pnl_row["net_pnl_usd"]) if pnl_row and pnl_row["net_pnl_usd"] is not None else None
                payload = {
                    "sample_id": int(row["sample_id"]),
                    "strategy_key": str(row["strategy_key"]),
                    "entry_time": int(row["entry_time"]),
                    "shadow_recommended_action": recommended,
                    "shadow_delta_logit": ACTION_DELTA[recommended],
                    "shadow_threshold": shadow_threshold,
                    "shadow_selected": bool(shadow_selected),
                    "shadow_reward_proxy": shadow_reward,
                    "shadow_excess_reward_proxy": shadow_excess,
                }
                connection.execute(
                    """
                    INSERT OR IGNORE INTO adaptive_policy_feedback(
                        prediction_id,decision_id,reward_proxy,neutral_reward_proxy,
                        excess_reward_proxy,actual_net_pnl_usd,matured_at,payload_json
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        int(row["prediction_id"]),
                        row.get("decision_id"),
                        adaptive_reward,
                        neutral_reward,
                        adaptive_reward - neutral_reward,
                        actual_pnl,
                        utc_now_iso(),
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                settled += 1
        if settled:
            self.refresh_evidence_gate()
        return settled

    @staticmethod
    def _mean_lcb95(values: list[float]) -> tuple[float | None, float | None]:
        if not values:
            return None, None
        mean = statistics.fmean(values)
        if len(values) < 2:
            return mean, None
        sd = statistics.stdev(values)
        return mean, mean - 1.96 * sd / math.sqrt(len(values))

    def evidence_status(self) -> EvidenceStatus:
        rows = self.database.fetch_all(
            """
            SELECT f.payload_json,p.sample_id,p.strategy_key,s.entry_time
            FROM adaptive_policy_feedback f
            JOIN predictions p ON p.id=f.prediction_id
            JOIN samples s ON s.id=p.sample_id
            WHERE s.feature_schema_version=?
            ORDER BY s.entry_time,p.sample_id,p.strategy_key
            """,
            (FEATURE_SCHEMA_VERSION,),
        )
        by_sample: dict[int, dict[str, Any]] = {}
        per_strategy_rows: dict[str, list[tuple[int, float]]] = {key: [] for key in ("model_1", "model_2", "model_3")}
        valid_rows = 0
        for row in rows:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
                excess = float(payload["shadow_excess_reward_proxy"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if not math.isfinite(excess):
                continue
            valid_rows += 1
            sample_id = int(row["sample_id"])
            entry_time = int(row["entry_time"])
            strategy = str(row["strategy_key"])
            bucket = by_sample.setdefault(sample_id, {"entry_time": entry_time, "values": []})
            bucket["values"].append(excess)
            if strategy in per_strategy_rows:
                per_strategy_rows[strategy].append((entry_time, excess))

        clusters = sorted(
            (
                (int(bucket["entry_time"]), statistics.fmean(bucket["values"]))
                for bucket in by_sample.values()
                if bucket["values"]
            ),
            key=lambda item: item[0],
        )
        changed_clusters = sum(1 for _, value in clusters if abs(value) > 1e-12)
        if len(clusters) < MIN_FEEDBACK_CLUSTERS:
            return EvidenceStatus(
                False, valid_rows, len(clusters), changed_clusters, None, None, None, {},
                f"feedback_warmup:{len(clusters)}/{MIN_FEEDBACK_CLUSTERS}",
            )
        if changed_clusters < MIN_CHANGED_CLUSTERS:
            return EvidenceStatus(
                False, valid_rows, len(clusters), changed_clusters, None, None, None, {},
                f"insufficient_policy_separation:{changed_clusters}/{MIN_CHANGED_CLUSTERS}",
            )

        split = max(1, int(len(clusters) * 0.70))
        development = [value for _, value in clusters[:split]]
        certification = [value for _, value in clusters[split:]]
        development_mean, _ = self._mean_lcb95(development)
        certification_mean, certification_lcb = self._mean_lcb95(certification)
        cert_start = clusters[split][0] if split < len(clusters) else clusters[-1][0]
        strategy_means: dict[str, float] = {}
        for strategy, values in per_strategy_rows.items():
            cert_values = [value for entry_time, value in values if entry_time >= cert_start]
            if cert_values:
                strategy_means[strategy] = statistics.fmean(cert_values)

        all_strategies_nonnegative = len(strategy_means) == 3 and all(value >= 0 for value in strategy_means.values())
        ready = bool(
            development_mean is not None
            and development_mean > 0
            and certification_lcb is not None
            and certification_lcb > 0
            and all_strategies_nonnegative
        )
        reason = "chronological_shadow_certified" if ready else "shadow_certification_failed"
        return EvidenceStatus(
            ready,
            valid_rows,
            len(clusters),
            changed_clusters,
            development_mean,
            certification_mean,
            certification_lcb,
            strategy_means,
            reason,
        )

    def refresh_evidence_gate(self) -> EvidenceStatus:
        evidence = self.evidence_status()
        current = self.database.get_runtime_state("adaptive_policy_ready", False) is True
        should_ready = evidence.ready
        # Once enabled, revoke only on materially negative recent certification;
        # otherwise a confidence interval brushing zero does not cause oscillation.
        if current and evidence.certification_lcb95 is not None and evidence.certification_lcb95 > DISABLE_LCB:
            should_ready = True
        if should_ready != current:
            self.database.set_runtime_state("adaptive_policy_ready", should_ready)
            self.database.audit(
                category="adaptive_policy",
                action="evidence_gate_enabled" if should_ready else "evidence_gate_revoked",
                severity="info" if should_ready else "warning",
                details=asdict(evidence),
            )
        self.database.set_runtime_state("adaptive_policy_evidence", asdict(evidence))
        return evidence

    def status(self) -> dict[str, Any]:
        latest = self.database.fetch_one(
            "SELECT * FROM adaptive_policy_decisions ORDER BY interval_start DESC LIMIT 1"
        )
        feedback = self.database.fetch_one(
            "SELECT COUNT(*) AS n,AVG(excess_reward_proxy) AS mean_excess FROM adaptive_policy_feedback"
        ) or {}
        evidence = self.evidence_status()
        return {
            "enabled": self.settings.adaptive_policy_enabled,
            "policy_ready": self.database.get_runtime_state("adaptive_policy_ready", False) is True,
            "exploration_rate": self.settings.adaptive_exploration_rate,
            "exploration_ready": self.database.get_runtime_state("adaptive_exploration_ready", False) is True,
            "latest_decision": asdict(self._from_row(latest)) if latest else None,
            "feedback_count": int(feedback.get("n") or 0),
            "mean_excess_reward_proxy": feedback.get("mean_excess"),
            "shadow_evidence": asdict(evidence),
            "policy_version": POLICY_VERSION,
        }
