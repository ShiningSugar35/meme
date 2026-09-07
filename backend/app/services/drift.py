from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Mapping, Sequence

import numpy as np

from ..collector.constants import FEATURE_SCHEMA_VERSION, LabelPolicy
from ..database import Database
from ..ml.decision_policy import DECISION_POLICY_VERSION
from ..ml.economics import theoretical_profit_units
from ..ml.features import materialize_entry_feature
from ..ml.types import PreparedDataset


DRIFT_POLICY_VERSION = "recent_vs_development_psi_v2"
RECENT_WINDOW_ROWS = 200
MIN_RECENT_ROWS = 60
PSI_CAUTION = 0.10
PSI_SEVERE = 0.25
LABEL_PRIOR_CAUTION_RELATIVE_DECLINE = 0.20
LABEL_PRIOR_SEVERE_RELATIVE_DECLINE = 0.35


@dataclass(frozen=True, slots=True)
class DriftDecision:
    state: str
    reasons: tuple[str, ...]
    label_prior_delta: float | None
    shifted_feature_fraction: float
    recent_rows: int
    recent_selected: int
    recent_precision: float | None
    recent_profit_units: float | None
    label_prior_relative_decline: float | None = None
    max_feature_psi: float = 0.0
    severe_features: tuple[str, ...] = ()
    caution_features: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite(values: Sequence[Any] | np.ndarray) -> np.ndarray:
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError):
        return np.asarray([], dtype=float)
    return array[np.isfinite(array)]


def _psi_reference(values: np.ndarray) -> dict[str, Any]:
    # Five quantile buckets are stable enough for ~1k rows and keep the artifact
    # compact. Duplicate quantiles are removed for near-constant features.
    quantiles = np.quantile(values, [0.2, 0.4, 0.6, 0.8])
    interior = np.unique(np.asarray(quantiles, dtype=float))
    bins = np.searchsorted(interior, values, side="right")
    counts = np.bincount(bins, minlength=len(interior) + 1).astype(float)
    proportions = counts / max(float(counts.sum()), 1.0)
    return {
        "psi_edges": [float(value) for value in interior],
        "reference_proportions": [float(value) for value in proportions],
        "reference_rows": int(len(values)),
        "median": float(np.median(values)),
    }


def _population_stability_index(values: Sequence[Any] | np.ndarray, stats: Mapping[str, Any]) -> float | None:
    recent = _finite(values)
    edges = np.asarray(stats.get("psi_edges") or (), dtype=float)
    reference = np.asarray(stats.get("reference_proportions") or (), dtype=float)
    if len(recent) < 20 or len(reference) != len(edges) + 1:
        return None
    bins = np.searchsorted(edges, recent, side="right")
    actual_counts = np.bincount(bins, minlength=len(edges) + 1).astype(float)
    actual = actual_counts / max(float(actual_counts.sum()), 1.0)
    epsilon = 1e-6
    expected = np.clip(reference, epsilon, None)
    actual = np.clip(actual, epsilon, None)
    return float(np.sum((actual - expected) * np.log(actual / expected)))


def _label_prior_diagnostics(recent_prior: float, reference_prior: float) -> tuple[float, float]:
    absolute_delta = float(recent_prior - reference_prior)
    if reference_prior <= 0:
        relative_decline = 0.0
    else:
        relative_decline = float(max(0.0, (reference_prior - recent_prior) / reference_prior))
    return absolute_delta, relative_decline


def build_drift_reference(
    dataset: PreparedDataset,
    indices: Sequence[int] | np.ndarray,
    feature_names: Sequence[str],
) -> dict[str, Any]:
    positions = np.asarray(indices, dtype=int)
    y = dataset.y.iloc[positions].to_numpy(dtype=int)
    stats: dict[str, dict[str, Any]] = {}
    frame = dataset.X.iloc[positions]
    for name in feature_names:
        if name not in frame.columns:
            continue
        values = _finite(frame[name].to_numpy())
        if len(values) < max(20, int(0.5 * len(positions))):
            continue
        stats[name] = _psi_reference(values)
    return {
        "version": DRIFT_POLICY_VERSION,
        "reference_kind": "development_pre_final_holdout",
        "reference_rows": len(positions),
        "reference_label_prior": float(np.mean(y)) if len(y) else None,
        "reference_start": dataset.timestamps.iloc[positions[0]].isoformat() if len(positions) else None,
        "reference_end": dataset.timestamps.iloc[positions[-1]].isoformat() if len(positions) else None,
        "feature_stats": stats,
        "psi_caution": PSI_CAUTION,
        "psi_severe": PSI_SEVERE,
        "certified": bool(len(positions) >= 100 and stats),
    }


def evaluate_final_certification_drift(
    dataset: PreparedDataset,
    indices: Sequence[int] | np.ndarray,
    reference: Mapping[str, Any],
) -> dict[str, Any]:
    """Certification-only final-window drift; never feeds model/budget selection."""
    positions = np.asarray(indices, dtype=int)
    if not len(positions) or not reference or not bool(reference.get("certified")):
        return {
            "state": "severe",
            "reasons": ["drift_reference_uncertified_or_final_empty"],
            "rows": int(len(positions)),
            "label_prior_relative_decline": None,
            "max_feature_psi": None,
            "feature_psi": {},
        }
    y = dataset.y.iloc[positions].to_numpy(dtype=int)
    recent_prior = float(np.mean(y))
    reference_prior = float(reference.get("reference_label_prior") or 0.0)
    delta, relative_decline = _label_prior_diagnostics(recent_prior, reference_prior)
    feature_psi: dict[str, float] = {}
    frame = dataset.X.iloc[positions]
    for name, stats in (reference.get("feature_stats") or {}).items():
        if name not in frame.columns:
            continue
        psi = _population_stability_index(frame[name].to_numpy(), stats)
        if psi is not None:
            feature_psi[name] = psi
    max_psi = max(feature_psi.values(), default=0.0)
    reasons: list[str] = []
    state = "normal"
    if relative_decline > LABEL_PRIOR_SEVERE_RELATIVE_DECLINE:
        state = "severe"
        reasons.append("label_prior_relative_decline_severe")
    elif relative_decline > LABEL_PRIOR_CAUTION_RELATIVE_DECLINE:
        state = "caution"
        reasons.append("label_prior_relative_decline_caution")
    if max_psi >= PSI_SEVERE:
        state = "severe"
        reasons.append("feature_psi_severe")
    elif max_psi >= PSI_CAUTION and state != "severe":
        state = "caution"
        reasons.append("feature_psi_caution")
    return {
        "state": state,
        "reasons": reasons or ["within_development_reference_bounds"],
        "rows": int(len(positions)),
        "reference_label_prior": reference_prior,
        "recent_label_prior": recent_prior,
        "label_prior_delta": delta,
        "label_prior_relative_decline": relative_decline,
        "max_feature_psi": float(max_psi),
        "feature_psi": feature_psi,
    }


class DriftGateService:
    """Lightweight recent-vs-development PSI gate; severe freezes model entries only."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def evaluate(self, *, model_id: str, reference: Mapping[str, Any]) -> DriftDecision:
        if not reference or not bool(reference.get("certified")):
            return DriftDecision(
                state="severe",
                reasons=("drift_reference_uncertified",),
                label_prior_delta=None,
                shifted_feature_fraction=1.0,
                recent_rows=0,
                recent_selected=0,
                recent_precision=None,
                recent_profit_units=None,
            )
        recent = self.database.fetch_all(
            """
            SELECT id,entry_time,entry_price,features_json,raw_json,tag
            FROM samples
            WHERE feature_schema_version=?
              AND label_version=?
              AND label_status='mature' AND tag IN (0,1)
              AND token_type IN ('new_creation','near_completion')
            ORDER BY entry_time DESC,id DESC
            LIMIT ?
            """,
            (FEATURE_SCHEMA_VERSION, LabelPolicy().label_version, RECENT_WINDOW_ROWS),
        )
        recent.reverse()
        reasons: list[str] = []
        state = "normal"
        if len(recent) < MIN_RECENT_ROWS:
            state = "caution"
            reasons.append("recent_label_evidence_sparse")
        label_prior_delta: float | None = None
        relative_decline: float | None = None
        reference_prior = reference.get("reference_label_prior")
        if recent and reference_prior is not None:
            recent_prior = float(np.mean([int(row["tag"]) for row in recent]))
            label_prior_delta, relative_decline = _label_prior_diagnostics(
                recent_prior, float(reference_prior)
            )
            if relative_decline > LABEL_PRIOR_SEVERE_RELATIVE_DECLINE:
                state = "severe"
                reasons.append("label_prior_relative_decline_severe")
            elif relative_decline > LABEL_PRIOR_CAUTION_RELATIVE_DECLINE and state != "severe":
                state = "caution"
                reasons.append("label_prior_relative_decline_caution")

        feature_stats = reference.get("feature_stats") or {}
        feature_psi: dict[str, float] = {}
        for name, stats in feature_stats.items():
            values: list[float] = []
            for row in recent:
                try:
                    source = json.loads(row.get("features_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    source = {}
                if not isinstance(source, dict):
                    source = {}
                try:
                    raw = json.loads(row.get("raw_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    raw = {}
                if isinstance(raw, dict):
                    for key in ("_public_social_signals", "_account_social_signals"):
                        provenance = raw.get(key)
                        if isinstance(provenance, dict):
                            source[key] = provenance
                value = materialize_entry_feature(name, source, entry_price=row.get("entry_price"))
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if np.isfinite(numeric):
                    values.append(numeric)
            psi = _population_stability_index(values, stats)
            if psi is not None:
                feature_psi[name] = psi
        caution_features = tuple(sorted(name for name, psi in feature_psi.items() if psi >= PSI_CAUTION))
        severe_features = tuple(sorted(name for name, psi in feature_psi.items() if psi >= PSI_SEVERE))
        comparable = len(feature_psi)
        shifted_fraction = len(caution_features) / comparable if comparable else 0.0
        max_psi = max(feature_psi.values(), default=0.0)
        if severe_features or (comparable and shifted_fraction >= 0.50):
            state = "severe"
            reasons.append("feature_psi_severe")
        elif caution_features and state != "severe":
            state = "caution"
            reasons.append("feature_psi_caution")

        recent_start = int(recent[0]["entry_time"]) if recent else 0
        recent_end = int(recent[-1]["entry_time"]) if recent else 0
        evidence = self.database.fetch_one(
            """
            SELECT COUNT(*) AS mature_rows,
                   SUM(CASE WHEN p.selected=1 THEN 1 ELSE 0 END) AS selected_count,
                   SUM(CASE WHEN p.selected=1 AND s.tag=1 THEN 1 ELSE 0 END) AS tp
            FROM predictions p JOIN samples s ON s.id=p.sample_id
            WHERE p.model_id=?
              AND p.decision_policy_version=?
              AND s.feature_schema_version=? AND s.label_version=?
              AND s.label_status='mature' AND s.tag IN (0,1)
              AND s.token_type IN ('new_creation','near_completion')
              AND s.entry_time BETWEEN ? AND ?
            """,
            (
                model_id, DECISION_POLICY_VERSION, FEATURE_SCHEMA_VERSION,
                LabelPolicy().label_version, recent_start, recent_end,
            ),
        ) or {}
        selected_count = int(evidence.get("selected_count") or 0)
        tp = int(evidence.get("tp") or 0)
        precision = (tp / selected_count) if selected_count else None
        profit_units = (
            theoretical_profit_units(tp, selected_count - tp)
            if selected_count
            else None
        )
        if selected_count >= 20 and (profit_units is not None and profit_units < 0):
            state = "severe"
            reasons.append("recent_model_economic_evidence_negative")
        elif selected_count >= 12 and precision is not None and precision < 0.25 and state != "severe":
            state = "caution"
            reasons.append("recent_model_precision_below_floor")
        if not reasons:
            reasons.append("within_development_reference_bounds")
        return DriftDecision(
            state=state,
            reasons=tuple(reasons),
            label_prior_delta=label_prior_delta,
            shifted_feature_fraction=float(shifted_fraction),
            recent_rows=len(recent),
            recent_selected=selected_count,
            recent_precision=precision,
            recent_profit_units=float(profit_units) if profit_units is not None else None,
            label_prior_relative_decline=relative_decline,
            max_feature_psi=float(max_psi),
            severe_features=severe_features,
            caution_features=caution_features,
        )
