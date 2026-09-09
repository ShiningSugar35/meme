from __future__ import annotations

from dataclasses import asdict, dataclass

from ..collector.constants import FEATURE_SCHEMA_VERSION, LabelPolicy
from ..config import Settings, get_settings
from ..database import Database, utc_now_iso


@dataclass(frozen=True, slots=True)
class ModelingReadiness:
    feature_schema_version: str
    mature_samples: int
    min_mature_samples: int
    ready: bool
    mode: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def modeling_readiness(
    database: Database,
    settings: Settings | None = None,
) -> ModelingReadiness:
    settings = settings or get_settings()
    row = database.fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM samples
        WHERE feature_schema_version=?
          AND label_version=?
          AND label_status='mature'
          AND tag IN (0,1)
          AND token_type IN ('new_creation','trending')
        """,
        (FEATURE_SCHEMA_VERSION, LabelPolicy().label_version),
    ) or {"n": 0}
    mature = int(row.get("n") or 0)
    minimum = int(settings.modeling_min_mature_samples)
    ready = mature >= minimum
    return ModelingReadiness(
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        mature_samples=mature,
        min_mature_samples=minimum,
        ready=ready,
        mode="modeling_enabled" if ready else "data_collection_rules_only",
        reason=(
            "mature_sample_floor_reached"
            if ready
            else f"mature_samples_below_floor:{mature}/{minimum}"
        ),
    )


def persist_modeling_readiness(
    database: Database,
    settings: Settings | None = None,
) -> ModelingReadiness:
    readiness = modeling_readiness(database, settings)
    payload = readiness.as_dict()
    payload["updated_at"] = utc_now_iso()
    database.set_runtime_state("modeling_readiness", payload)
    return readiness
