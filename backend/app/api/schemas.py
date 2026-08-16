from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field, SecretStr


class ConfirmActionRequest(BaseModel):
    challenge: str = Field(min_length=20, max_length=200)


class ImportRequest(BaseModel):
    force: bool = False


class TrainingRequest(BaseModel):
    reason: str = Field(default="manual", max_length=200)
    features: list[str] | None = Field(default=None, max_length=64)


class FeatureSelectionRequest(BaseModel):
    features: list[str] = Field(min_length=1, max_length=64)


class AgentProposalRequest(BaseModel):
    proposal_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentDecisionRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class PlatformRuntimeConfigRequest(BaseModel):
    position_monitor_poll_seconds: float = Field(ge=1.0, le=60.0)
    gmgn_global_rps: float = Field(gt=0, le=50.0)
    regime_poll_seconds: int = Field(default=60, ge=15, le=3600)
    adaptive_action_interval_minutes: int = Field(default=15, ge=5, le=60)
    adaptive_min_confidence: float = Field(default=0.55, ge=0, le=1)
    adaptive_exploration_rate: float = Field(default=0.0, ge=0, le=0.05)
    gmgn_base_url: str | None = Field(default=None, max_length=500)
    jupiter_quote_url: str | None = Field(default=None, max_length=500)


class ProviderCredentialRequest(BaseModel):
    credential: SecretStr
