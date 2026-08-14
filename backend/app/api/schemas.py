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
    gmgn_base_url: str | None = Field(default=None, max_length=500)
    jupiter_quote_url: str | None = Field(default=None, max_length=500)


class ProviderCredentialRequest(BaseModel):
    credential: SecretStr
