from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class OPEEstimate:
    method: str
    estimate: float | None
    effective_rows: int


def full_information_replay(rows: Iterable[Mapping[str, float]], reward_key: str = "counterfactual_reward") -> OPEEstimate:
    rewards = [float(row[reward_key]) for row in rows if row.get(reward_key) is not None and math.isfinite(float(row[reward_key]))]
    return OPEEstimate("full_information_replay", sum(rewards) / len(rewards) if rewards else None, len(rewards))


def ips_estimate(
    rows: Iterable[Mapping[str, float]],
    *,
    reward_key: str = "reward",
    target_propensity_key: str = "target_propensity",
    logging_propensity_key: str = "logging_propensity",
) -> OPEEstimate:
    weighted: list[float] = []
    for row in rows:
        reward = row.get(reward_key)
        target = row.get(target_propensity_key)
        logging = row.get(logging_propensity_key)
        if reward is None or target is None or logging is None:
            continue
        reward, target, logging = float(reward), float(target), float(logging)
        if logging <= 0 or not all(math.isfinite(value) for value in (reward, target, logging)):
            continue
        weighted.append(reward * target / logging)
    return OPEEstimate("ips", sum(weighted) / len(weighted) if weighted else None, len(weighted))


def doubly_robust_estimate(
    rows: Iterable[Mapping[str, float]],
    *,
    reward_key: str = "reward",
    target_propensity_key: str = "target_propensity",
    logging_propensity_key: str = "logging_propensity",
    logged_model_key: str = "logged_reward_model",
    target_model_key: str = "target_reward_model",
) -> OPEEstimate:
    values: list[float] = []
    for row in rows:
        fields = [row.get(key) for key in (reward_key, target_propensity_key, logging_propensity_key, logged_model_key, target_model_key)]
        if any(value is None for value in fields):
            continue
        reward, target_p, logging_p, logged_q, target_q = map(float, fields)
        if logging_p <= 0 or not all(math.isfinite(value) for value in (reward, target_p, logging_p, logged_q, target_q)):
            continue
        values.append(target_q + (target_p / logging_p) * (reward - logged_q))
    return OPEEstimate("doubly_robust", sum(values) / len(values) if values else None, len(values))
