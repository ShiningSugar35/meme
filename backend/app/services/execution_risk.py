from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Mapping

import numpy as np
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ..collector.constants import FEATURE_SCHEMA_VERSION
from ..database import Database
from ..ml.calibration import SigmoidCalibrator, fit_sigmoid_calibrator
from ..ml.features import AVAILABLE_MODEL_FEATURES, materialize_entry_feature


EXECUTION_RISK_VERSION = "severe_stop_gap_entry_pit_v2_unconditional"
MIN_OBSERVATIONS = 300
MIN_POSITIVES = 40
MIN_NEGATIVES = 80
MIN_AUC = 0.60
MIN_AP_LIFT = 0.05


def _has_training_feature_coverage(
    values: list[float | None],
    *,
    train_rows: int,
    minimum_fraction: float = 0.70,
) -> bool:
    if train_rows <= 0:
        return False
    required = max(1, int(np.ceil(float(minimum_fraction) * train_rows)))
    return sum(value is not None for value in values[:train_rows]) >= required


@dataclass(slots=True)
class ExecutionRiskModel:
    version: str
    algorithm: str
    feature_names: tuple[str, ...]
    medians: dict[str, float]
    estimator: Any
    training_hash: str
    calibrator: SigmoidCalibrator | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    certified: bool = True

    def _vector(self, row: Mapping[str, Any]) -> np.ndarray:
        source = row.get("features_json") if isinstance(row, dict) else None
        if isinstance(source, str):
            try:
                source = json.loads(source)
            except json.JSONDecodeError:
                source = {}
        if not isinstance(source, Mapping):
            source = row
        else:
            source = dict(source)
            raw = row.get("raw_json") if isinstance(row, Mapping) else None
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError:
                    raw = {}
            if isinstance(raw, Mapping):
                for key in ("_public_social_signals", "_account_social_signals"):
                    provenance = raw.get(key)
                    if isinstance(provenance, Mapping):
                        source[key] = dict(provenance)
        values: list[float] = []
        for name in self.feature_names:
            if name == "age_minutes":
                value = row.get("age_minutes")
            else:
                value = materialize_entry_feature(
                    name,
                    source,
                    entry_price=row.get("entry_price"),
                )
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = self.medians[name]
            if not np.isfinite(numeric):
                numeric = self.medians[name]
            values.append(numeric)
        return np.asarray([values], dtype=float)

    def predict_probability(self, row: Mapping[str, Any]) -> float:
        if not self.certified:
            raise RuntimeError("execution-risk model is not certified")
        raw_score = float(self.estimator.predict_proba(self._vector(row))[0, 1])
        if not np.isfinite(raw_score):
            raise RuntimeError("execution-risk model returned a non-finite score")
        calibrator = getattr(self, "calibrator", None)
        if calibrator is None:
            probability = raw_score
        else:
            probability = float(calibrator.transform([raw_score])[0])
        if not np.isfinite(probability):
            raise RuntimeError("execution-risk calibration returned a non-finite probability")
        return float(np.clip(probability, 0.0, 1.0))

    def provenance(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "algorithm": self.algorithm,
            "feature_names": list(self.feature_names),
            "training_hash": self.training_hash,
            "calibration": getattr(self, "calibrator", None).provenance() if getattr(self, "calibrator", None) is not None else None,
            "certified": bool(self.certified),
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class ExecutionRiskTrainingResult:
    available: bool
    certified: bool
    model: ExecutionRiskModel | None
    observations: int
    positives: int
    negatives: int
    prevalence: float
    reason: str
    training_cutoff_entry_time_exclusive: int | None = None
    training_cutoff_exit_time_exclusive: str | None = None
    candidates: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("model", None)
        if self.model is not None:
            payload["model"] = self.model.provenance()
        return payload


class ExecutionRiskTrainer:
    """Chronological severe stop-gap classifier using entry-time PIT features only."""

    def __init__(self, database: Database, *, random_state: int = 42) -> None:
        self.database = database
        self.random_state = random_state

    def _rows(
        self,
        *,
        max_entry_time_exclusive: int | None = None,
        max_exit_time_exclusive: str | None = None,
    ) -> list[dict[str, Any]]:
        cutoff_clauses: list[str] = []
        parameters: list[Any] = [FEATURE_SCHEMA_VERSION]
        if max_entry_time_exclusive is not None:
            cutoff_clauses.append("s.entry_time < ?")
            parameters.append(int(max_entry_time_exclusive))
        if max_exit_time_exclusive is not None:
            cutoff_clauses.append("p.exit_time < ?")
            parameters.append(str(max_exit_time_exclusive))
        cutoff_clause = (
            " AND " + " AND ".join(cutoff_clauses)
            if cutoff_clauses
            else ""
        )
        return self.database.fetch_all(
            f"""
            SELECT p.id AS position_id,p.sample_id,p.stop_loss_price,p.exit_reason,p.metadata_json,
                   s.entry_time,s.entry_price,s.age_minutes,s.features_json,s.raw_json
            FROM positions p
            JOIN samples s ON s.id=p.sample_id
            WHERE p.account_kind='simulation'
              AND p.strategy_key='rules_only'
              AND p.status='closed'
              AND p.stop_loss_price>0
              AND p.sample_id IS NOT NULL
              AND s.feature_schema_version=?
              AND s.token_type IN ('new_creation','trending')
              AND COALESCE(json_extract(p.metadata_json,'$.performance_excluded'),0)=0
              {cutoff_clause}
            ORDER BY s.entry_time,p.id
            """,
            tuple(parameters),
        )

    @staticmethod
    def _target(row: Mapping[str, Any]) -> int | None:
        # This is an entry-time risk head, so the target must be unconditional
        # across the whole clean rules-only opportunity set. The previous v1
        # sampled only rows that had already stopped out, estimating
        # P(severe gap | stop-loss) and then using it as P(severe gap).
        if not str(row.get("exit_reason") or "").startswith("stop_loss"):
            return 0
        try:
            metadata = json.loads(str(row.get("metadata_json") or "{}"))
            trigger_fact = metadata.get("exit_trigger_reference_price")
            if trigger_fact is None:
                # Legacy closed rows predate the explicit trigger-reference audit
                # field; ``last_market_price`` was the persisted trigger price in
                # that implementation. New rows never need this fallback.
                trigger_fact = metadata.get("last_market_price")
            trigger_reference = float(trigger_fact)
            stop_price = float(row.get("stop_loss_price"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not np.isfinite([trigger_reference, stop_price]).all() or stop_price <= 0:
            return None
        return int(trigger_reference <= stop_price * 0.90)

    @staticmethod
    def _numeric(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if np.isfinite(number) else None

    def train(
        self,
        *,
        max_entry_time_exclusive: int | None = None,
        max_exit_time_exclusive: str | None = None,
    ) -> ExecutionRiskTrainingResult:
        entry_cutoff = (
            None
            if max_entry_time_exclusive is None
            else int(max_entry_time_exclusive)
        )
        exit_cutoff = (
            None
            if max_exit_time_exclusive is None
            else str(max_exit_time_exclusive)
        )
        raw_rows = self._rows(
            max_entry_time_exclusive=entry_cutoff,
            max_exit_time_exclusive=exit_cutoff,
        )
        labelled: list[tuple[dict[str, Any], int]] = []
        for row in raw_rows:
            target = self._target(row)
            if target is not None:
                labelled.append((row, target))
        y = np.asarray([target for _, target in labelled], dtype=int)
        observations = len(labelled)
        positives = int(y.sum()) if observations else 0
        negatives = observations - positives
        prevalence = positives / observations if observations else 0.0
        if observations < MIN_OBSERVATIONS or positives < MIN_POSITIVES or negatives < MIN_NEGATIVES:
            return ExecutionRiskTrainingResult(
                available=False,
                certified=False,
                model=None,
                observations=observations,
                positives=positives,
                negatives=negatives,
                prevalence=prevalence,
                reason="minimum_data_gate_not_met",
                training_cutoff_entry_time_exclusive=entry_cutoff,
                training_cutoff_exit_time_exclusive=exit_cutoff,
            )

        train_end = max(1, int(observations * 0.70))
        calibrate_end = max(train_end + 1, int(observations * 0.85))
        calibrate_end = min(calibrate_end, observations - 1)

        # Build only entry-time materialized features. No exit/label/path field is
        # ever exposed to X. A raw age column is included because the research
        # showed a strong age-dependent stop-gap mechanism. Feature availability
        # is decided from the training slice only; future missingness patterns may
        # not choose the risk feature set.
        candidate_names = ("age_minutes", *AVAILABLE_MODEL_FEATURES)
        materialized: dict[str, list[float | None]] = {name: [] for name in candidate_names}
        for row, _ in labelled:
            try:
                source = json.loads(str(row.get("features_json") or "{}"))
            except json.JSONDecodeError:
                source = {}
            if not isinstance(source, dict):
                source = {}
            try:
                raw = json.loads(str(row.get("raw_json") or "{}"))
            except json.JSONDecodeError:
                raw = {}
            if isinstance(raw, Mapping):
                for key in ("_public_social_signals", "_account_social_signals"):
                    provenance = raw.get(key)
                    if isinstance(provenance, Mapping):
                        source[key] = dict(provenance)
            for name in candidate_names:
                value = (
                    row.get("age_minutes")
                    if name == "age_minutes"
                    else materialize_entry_feature(name, source, entry_price=row.get("entry_price"))
                )
                materialized[name].append(self._numeric(value))
        usable = [
            name
            for name, values in materialized.items()
            if _has_training_feature_coverage(values, train_rows=train_end)
        ]
        if len(usable) < 3:
            return ExecutionRiskTrainingResult(
                available=False,
                certified=False,
                model=None,
                observations=observations,
                positives=positives,
                negatives=negatives,
                prevalence=prevalence,
                reason="insufficient_entry_feature_coverage",
                training_cutoff_entry_time_exclusive=entry_cutoff,
                training_cutoff_exit_time_exclusive=exit_cutoff,
            )
        X = np.empty((observations, len(usable)), dtype=float)
        medians: dict[str, float] = {}
        for column_index, name in enumerate(usable):
            values = np.asarray([
                np.nan if value is None else float(value) for value in materialized[name]
            ], dtype=float)
            median = float(np.nanmedian(values[:train_end]))
            if not np.isfinite(median):
                median = 0.0
            medians[name] = median
            values[~np.isfinite(values)] = median
            X[:, column_index] = values
        y_train = y[:train_end]
        y_calibration = y[train_end:calibrate_end]
        y_certification = y[calibrate_end:]
        X_train = X[:train_end]
        X_calibration = X[train_end:calibrate_end]
        X_certification = X[calibrate_end:]
        if any(len(np.unique(part)) < 2 for part in (y_train, y_calibration, y_certification)):
            return ExecutionRiskTrainingResult(
                available=False,
                certified=False,
                model=None,
                observations=observations,
                positives=positives,
                negatives=negatives,
                prevalence=prevalence,
                reason="chronological_holdout_class_degenerate",
                training_cutoff_entry_time_exclusive=entry_cutoff,
                training_cutoff_exit_time_exclusive=exit_cutoff,
            )

        candidates: list[tuple[str, Any, SigmoidCalibrator, dict[str, Any]]] = []
        factories = {
            "extra_trees": lambda: ExtraTreesClassifier(
                n_estimators=400,
                max_depth=6,
                min_samples_leaf=12,
                max_features="sqrt",
                random_state=self.random_state,
                n_jobs=1,
            ),
            "logistic": lambda: Pipeline(
                [
                    ("scale", StandardScaler()),
                    (
                        "model",
                        LogisticRegression(
                            C=0.5,
                            max_iter=2_000,
                            random_state=self.random_state,
                        ),
                    ),
                ]
            ),
        }
        calibration_prevalence = float(np.mean(y_calibration))
        certification_prevalence = float(np.mean(y_certification))
        for name, factory in factories.items():
            estimator = factory()
            estimator.fit(X_train, y_train)
            calibration_raw = np.asarray(
                estimator.predict_proba(X_calibration)[:, 1], dtype=float
            )
            calibrator = fit_sigmoid_calibrator(
                calibration_raw,
                y_calibration,
                source_indices=range(train_end, calibrate_end),
                source_start=str(labelled[train_end][0].get("entry_time")),
                source_end=str(labelled[calibrate_end - 1][0].get("entry_time")),
            )
            certification_raw = np.asarray(
                estimator.predict_proba(X_certification)[:, 1], dtype=float
            )
            probabilities = calibrator.transform(certification_raw)
            metrics = {
                "calibration_auc": float(roc_auc_score(y_calibration, calibration_raw)),
                "calibration_ap": float(average_precision_score(y_calibration, calibration_raw)),
                "calibration_prevalence": calibration_prevalence,
                "calibration_rows": len(y_calibration),
                "auc": float(roc_auc_score(y_certification, probabilities)),
                "ap": float(average_precision_score(y_certification, probabilities)),
                "brier": float(brier_score_loss(y_certification, probabilities)),
                "raw_brier": float(brier_score_loss(y_certification, certification_raw)),
                "holdout_prevalence": certification_prevalence,
                "holdout_rows": len(y_certification),
                "calibrated_probability_mean": float(np.mean(probabilities)),
            }
            metrics["certified"] = bool(
                metrics["auc"] >= MIN_AUC
                and metrics["ap"] >= certification_prevalence + MIN_AP_LIFT
            )
            candidates.append((name, estimator, calibrator, metrics))
        selected = max(
            candidates,
            key=lambda item: (item[3]["calibration_ap"], item[3]["calibration_auc"]),
        )
        public_candidates = tuple(
            {"algorithm": name, **metrics}
            for name, _, _, metrics in candidates
        )
        if not selected[3]["certified"]:
            return ExecutionRiskTrainingResult(
                available=True,
                certified=False,
                model=None,
                observations=observations,
                positives=positives,
                negatives=negatives,
                prevalence=prevalence,
                reason="selected_candidate_certification_failed",
                training_cutoff_entry_time_exclusive=entry_cutoff,
                training_cutoff_exit_time_exclusive=exit_cutoff,
                candidates=public_candidates,
            )
        name, estimator, calibrator, metrics = selected
        digest = hashlib.sha256()
        digest.update(
            (
                f"{EXECUTION_RISK_VERSION}|entry_cutoff={entry_cutoff}|"
                f"exit_cutoff={exit_cutoff}|features={','.join(usable)}\n"
            ).encode()
        )
        for (row, target) in labelled[:calibrate_end]:
            digest.update(f"{row['position_id']}|{row['sample_id']}|{target}\n".encode())
        model = ExecutionRiskModel(
            version=EXECUTION_RISK_VERSION,
            algorithm=name,
            feature_names=tuple(usable),
            medians=medians,
            estimator=estimator,
            training_hash=digest.hexdigest(),
            calibrator=calibrator,
            metrics={
                **metrics,
                "observations": observations,
                "positives": positives,
                "negatives": negatives,
                "prevalence": prevalence,
                "train_rows": train_end,
                "calibration_rows": len(y_calibration),
                "certification_rows": len(y_certification),
                "target": "all clean rules-only entries; severe=stop_loss and trigger_reference_price <= stop_loss_price * 0.90",
                "training_cutoff_entry_time_exclusive": entry_cutoff,
                "training_cutoff_exit_time_exclusive": exit_cutoff,
                "pit_only": True,
            },
        )
        return ExecutionRiskTrainingResult(
            available=True,
            certified=True,
            model=model,
            observations=observations,
            positives=positives,
            negatives=negatives,
            prevalence=prevalence,
            reason="certified",
            training_cutoff_entry_time_exclusive=entry_cutoff,
            training_cutoff_exit_time_exclusive=exit_cutoff,
            candidates=public_candidates,
        )
