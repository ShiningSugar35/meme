from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

import pandas as pd

from ..collector.constants import FEATURE_SCHEMA_VERSION
from ..database import Database
from ..services.training import TrainingService
from .features import DEFAULT_MODEL_TRAINING_FEATURES, FeatureBuilder, FeaturePolicy
from .trainer import ModelTrainer, TrainerConfig


TOKEN_LOCAL_FAMILY: tuple[str, ...] = (
    "price_change_2m",
    "ln(volume_1m+1)",
    "ln(swaps_1m+1)",
    "buy_count_imbalance_1m",
    "buy_volume_imbalance_1m",
    "ln(volume_1m/swaps_1m+1)",
    "ln(volume_2m+1)",
    "volume_acceleration_2m",
    "holder_count/age",
    "ln(marketcap+1)",
    "creator_token_status",
)

TOKEN_ATTENTION_FAMILY: tuple[str, ...] = (
    "dexscr_ad",
    "ln(dexscr_boost_fee+1)",
    "dexscr_trending_bar",
    "ln(x_user_follower+1)",
    "ln(tg_call_count+1)",
)

NEW_TOKEN_FEATURES = frozenset((*TOKEN_LOCAL_FAMILY, *TOKEN_ATTENTION_FAMILY))
LEGACY_BASELINE_FEATURES: tuple[str, ...] = tuple(
    feature for feature in DEFAULT_MODEL_TRAINING_FEATURES if feature not in NEW_TOKEN_FEATURES
)

REGIME_FAMILIES: dict[str, tuple[str, ...]] = {
    "meme_regime": (
        "meme_discovery_breadth",
        "meme_acceptance_rate",
        "meme_new_creation_breadth",
        "meme_near_completion_breadth",
        "market_buy_count_imbalance_1m",
        "market_buy_volume_imbalance_1m",
    ),
    "sol_network": (
        "sol_return_5m",
        "sol_return_15m",
        "sol_return_60m",
        "sol_realized_vol_15m",
        "solana_non_vote_tps",
        "solana_slot_rate",
        "solana_priority_fee_p50",
        "solana_priority_fee_p90",
    ),
    "crypto_market": (
        "btc_return_5m",
        "btc_return_15m",
        "btc_return_60m",
        "btc_realized_vol_15m",
    ),
    "attention": (
        "trending_count_1m",
        "hot_search_count_1m",
        "hot_search_visits_1m",
        "signal_smart_money_buy_15m",
        "signal_kol_buy_15m",
        "signal_large_buy_15m",
        "signal_multi_buy_15m",
        "signal_dex_ad_15m",
        "signal_dex_boost_15m",
        "signal_dex_trending_15m",
        "dexscreener_sol_ads_latest_count",
        "dexscreener_sol_boosts_latest_count",
        "dexscreener_sol_boost_amount_latest",
    ),
}

AUDIT_FAMILIES: dict[str, tuple[str, ...]] = {
    "local_event": TOKEN_LOCAL_FAMILY,
    "attention_entry": TOKEN_ATTENTION_FAMILY,
    **REGIME_FAMILIES,
}


@dataclass(frozen=True, slots=True)
class FamilyAuditReadiness:
    status: str
    generation: str
    mature_samples: int
    positives: int
    negatives: int
    min_mature_samples: int
    min_positive_samples: int
    min_negative_samples: int
    reasons: tuple[str, ...]


class FeatureFamilyAuditService:
    """Chronological family-ablation audit that never changes production models.

    The final holdout produced by ModelTrainer is certification-only. Family lift
    is measured from the development-period composite score of one fixed reference
    learner so the comparison isolates data families rather than model selection.
    """

    def __init__(
        self,
        database: Database,
        *,
        min_mature_samples: int = 200,
        min_positive_samples: int = 25,
        min_negative_samples: int = 50,
        regime_max_lag_seconds: int = 300,
        min_family_row_coverage: float = 0.60,
    ) -> None:
        self.database = database
        self.training = TrainingService(database)
        self.min_mature_samples = int(min_mature_samples)
        self.min_positive_samples = int(min_positive_samples)
        self.min_negative_samples = int(min_negative_samples)
        self.regime_max_lag_seconds = int(regime_max_lag_seconds)
        self.min_family_row_coverage = min(1.0, max(0.0, float(min_family_row_coverage)))

    def readiness(self) -> FamilyAuditReadiness:
        rows = self.training.samples.list_mature()
        positives = sum(int(row.get("tag") or 0) == 1 for row in rows)
        negatives = len(rows) - positives
        reasons: list[str] = []
        if len(rows) < self.min_mature_samples:
            reasons.append(f"mature_samples:{len(rows)}/{self.min_mature_samples}")
        if positives < self.min_positive_samples:
            reasons.append(f"positives:{positives}/{self.min_positive_samples}")
        if negatives < self.min_negative_samples:
            reasons.append(f"negatives:{negatives}/{self.min_negative_samples}")
        return FamilyAuditReadiness(
            status="READY" if not reasons else "INSUFFICIENT_DATA",
            generation=FEATURE_SCHEMA_VERSION,
            mature_samples=len(rows),
            positives=positives,
            negatives=negatives,
            min_mature_samples=self.min_mature_samples,
            min_positive_samples=self.min_positive_samples,
            min_negative_samples=self.min_negative_samples,
            reasons=tuple(reasons),
        )

    def _regime_features_at(self, entry_time: int) -> Mapping[str, Any]:
        row = self.database.fetch_one(
            """
            SELECT observed_at,features_json
            FROM market_regime_snapshots
            WHERE observed_at<=?
            ORDER BY observed_at DESC LIMIT 1
            """,
            (int(entry_time),),
        )
        if not row:
            return {}
        observed_at = int(row.get("observed_at") or 0)
        if int(entry_time) - observed_at > self.regime_max_lag_seconds:
            return {}
        try:
            decoded = json.loads(row.get("features_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, Mapping) else {}

    def _frame(self, rows: list[dict[str, Any]]) -> pd.DataFrame:
        frame, _ = self.training._training_frame(rows)
        regime_columns = {name for family in REGIME_FAMILIES.values() for name in family}
        values = {name: [] for name in regime_columns}
        for row in rows:
            regime = self._regime_features_at(int(row["entry_time"]))
            for name in regime_columns:
                values[name].append(regime.get(name))
        for name, column in values.items():
            frame[name] = column
        return frame

    @staticmethod
    def _coverage(frame: pd.DataFrame, names: tuple[str, ...]) -> dict[str, float]:
        result: dict[str, float] = {}
        total = len(frame)
        for name in names:
            if name not in frame.columns or total == 0:
                result[name] = 0.0
                continue
            result[name] = float(frame[name].notna().sum() / total)
        return result

    @staticmethod
    def _row_coverage(frame: pd.DataFrame, names: tuple[str, ...]) -> float:
        available = [name for name in names if name in frame.columns]
        if not available or frame.empty:
            return 0.0
        return float(frame.loc[:, available].notna().any(axis=1).mean())

    @staticmethod
    def _evaluate(frame: pd.DataFrame, features: tuple[str, ...]) -> dict[str, Any]:
        dataset = FeatureBuilder(FeaturePolicy(feature_allowlist=features)).prepare(frame)
        trainer = ModelTrainer(
            TrainerConfig(
                candidate_names=("random_forest",),
                top_k=1,
            )
        )
        result = trainer.train(dataset)
        candidate = result.candidates_by_algorithm["random_forest"]
        if candidate.status != "ok":
            raise RuntimeError(candidate.skip_reason or "family audit reference learner failed")
        return {
            "reference_algorithm": "random_forest",
            "requested_feature_count": len(features),
            "selected_features": list(candidate.feature_names),
            "development_economic_score": candidate.economic_score,
            "development_generalization_score": (
                candidate.generalization.score if candidate.generalization else None
            ),
            "development_composite_score": candidate.composite_score,
            "development_trade_count": (
                candidate.development_metrics.trade_count if candidate.development_metrics else 0
            ),
            "final_certification": (
                asdict(candidate.final_metrics) if candidate.final_metrics is not None else None
            ),
            "final_is_certification_only": True,
        }

    def run(self) -> dict[str, Any]:
        readiness = self.readiness()
        rows = self.training.samples.list_mature()
        report: dict[str, Any] = {
            "status": readiness.status,
            "readiness": asdict(readiness),
            "generation": FEATURE_SCHEMA_VERSION,
            "baseline_features": list(LEGACY_BASELINE_FEATURES),
            "families": {},
            "selection_rule": (
                "development chronological OOS family lift with fold-train-only ranking and one-SE + 8% Occam protection; "
                "final holdout is certification-only; this audit never mutates feature selection or active models"
            ),
        }
        if not rows:
            for family, names in AUDIT_FAMILIES.items():
                report["families"][family] = {
                    "features": list(names),
                    "coverage": {name: 0.0 for name in names},
                    "status": "INSUFFICIENT_DATA",
                }
            return report

        frame = self._frame(rows)
        for family, names in AUDIT_FAMILIES.items():
            row_coverage = self._row_coverage(frame, names)
            status = "WAITING_FOR_READINESS" if readiness.status != "READY" else "READY"
            if readiness.status == "READY" and row_coverage < self.min_family_row_coverage:
                status = "INSUFFICIENT_COVERAGE"
            report["families"][family] = {
                "features": list(names),
                "coverage": self._coverage(frame, names),
                "row_coverage": row_coverage,
                "minimum_row_coverage": self.min_family_row_coverage,
                "status": status,
            }
        if readiness.status != "READY":
            return report

        baseline = self._evaluate(frame, LEGACY_BASELINE_FEATURES)
        report["baseline"] = baseline
        baseline_score = baseline.get("development_composite_score")
        completed = 0
        for family, names in AUDIT_FAMILIES.items():
            if report["families"][family]["status"] != "READY":
                continue
            selected = tuple(dict.fromkeys((*LEGACY_BASELINE_FEATURES, *names)))
            try:
                evaluation = self._evaluate(frame, selected)
            except Exception as exc:
                report["families"][family]["status"] = "FAILED"
                report["families"][family]["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                continue
            score = evaluation.get("development_composite_score")
            lift = (
                float(score) - float(baseline_score)
                if score is not None and baseline_score is not None
                else None
            )
            evaluation["development_composite_lift_vs_baseline"] = lift
            selected_from_family = [
                name for name in evaluation.get("selected_features", []) if name in set(names)
            ]
            evaluation["selected_from_family"] = selected_from_family
            evaluation["development_recommendation"] = (
                "KEEP_FOR_CERTIFICATION"
                if lift is not None and lift > 0 and selected_from_family
                else "DELETE_FAMILY"
            )
            report["families"][family].update({"status": "COMPLETE", "evaluation": evaluation})
            completed += 1
        report["status"] = "COMPLETE" if completed == len(AUDIT_FAMILIES) else "PARTIAL"
        return report
