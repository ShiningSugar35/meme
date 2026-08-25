from __future__ import annotations

from dataclasses import dataclass

from .sparse_budget import GLOBAL_PROBABILITY_FLOOR


DECISION_POLICY_VERSION = "phase16_age_execution_sparse_v1"
RISK_CEILING_NORMAL = 0.40
RISK_CEILING_CAUTION = 0.35


@dataclass(frozen=True, slots=True)
class AgeGateDecision:
    allowed: bool
    probability_floor: float
    reason: str


def age_gate(age_minutes: float | int | None) -> AgeGateDecision:
    if age_minutes is None:
        return AgeGateDecision(False, GLOBAL_PROBABILITY_FLOOR, "age_missing")
    age = float(age_minutes)
    if not 2.0 < age < 300.0:
        return AgeGateDecision(False, GLOBAL_PROBABILITY_FLOOR, "age_outside_admission_contract")
    if age < 10.0:
        return AgeGateDecision(True, 0.29, "age_2_10")
    if age < 30.0:
        return AgeGateDecision(True, 0.29, "age_10_30")
    if age < 60.0:
        return AgeGateDecision(True, 0.22, "age_30_60")
    if age < 120.0:
        return AgeGateDecision(False, GLOBAL_PROBABILITY_FLOOR, "age_60_120_abstain")
    return AgeGateDecision(True, GLOBAL_PROBABILITY_FLOOR, "age_120_300")


def clamp_adaptive_threshold(
    policy_base_threshold: float,
    age_probability_floor: float,
    adaptive_candidate_threshold: float,
) -> float:
    """Adaptive policy may tighten, but never undercut Phase16 hard floors."""
    return float(max(
        GLOBAL_PROBABILITY_FLOOR,
        float(policy_base_threshold),
        float(age_probability_floor),
        float(adaptive_candidate_threshold),
    ))
