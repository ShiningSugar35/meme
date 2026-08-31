from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .sparse_budget import GLOBAL_PROBABILITY_FLOOR


DECISION_POLICY_VERSION = "phase19_expected_return_paper_v1"
DEPLOYMENT_CERTIFICATION_VERSION = "phase19_expected_return_certification_v1"

AGE_POLICY_ADMISSION_ONLY = "age_admission_only_v1"
AGE_POLICY_CANDIDATES = (AGE_POLICY_ADMISSION_ONLY,)
DEFAULT_AGE_POLICY_VERSION = AGE_POLICY_ADMISSION_ONLY


@dataclass(frozen=True, slots=True)
class AgeGateDecision:
    allowed: bool
    threshold_delta: float
    reason: str


def deployment_certification_is_current(
    certification: Mapping[str, Any] | None,
) -> bool:
    """Require an explicit current-version deployment certificate."""
    return bool(
        isinstance(certification, Mapping)
        and str(certification.get("version") or "")
        == DEPLOYMENT_CERTIFICATION_VERSION
        and str(certification.get("deployment_fit_scope") or "")
        == "final_train_only_certified_instance"
    )


def deployment_model_is_qualified(
    certification: Mapping[str, Any] | None,
) -> bool:
    """Only a model with its own positive final evidence may open entries."""
    return bool(
        deployment_certification_is_current(certification)
        and certification.get("qualified_deployment_evidence") is True
    )


def age_gate(
    age_minutes: float | int | None,
    policy_version: str = DEFAULT_AGE_POLICY_VERSION,
) -> AgeGateDecision:
    if policy_version not in AGE_POLICY_CANDIDATES:
        return AgeGateDecision(False, 0.0, "age_policy_unknown")
    if age_minutes is None:
        return AgeGateDecision(False, 0.0, "age_missing")
    age = float(age_minutes)
    if not 2.0 < age < 300.0:
        return AgeGateDecision(False, 0.0, "age_outside_admission_contract")
    return AgeGateDecision(True, 0.0, "age_admission_only")


def age_adjusted_threshold(
    policy_base_threshold: float,
    age: AgeGateDecision,
) -> float:
    """Return the model's development-frozen threshold without age reweighting."""
    return float(max(GLOBAL_PROBABILITY_FLOOR, float(policy_base_threshold)))


def clamp_adaptive_threshold(
    policy_base_threshold: float,
    age_threshold: float,
    adaptive_candidate_threshold: float,
) -> float:
    """Adaptive policy may tighten, but never undercut model/age hard floors."""
    return float(max(
        GLOBAL_PROBABILITY_FLOOR,
        float(policy_base_threshold),
        float(age_threshold),
        float(adaptive_candidate_threshold),
    ))
