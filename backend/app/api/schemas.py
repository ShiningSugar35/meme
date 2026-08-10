from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class ConfirmActionRequest(BaseModel):
    challenge: str = Field(min_length=20, max_length=200)


class ImportRequest(BaseModel):
    force: bool = False


class TrainingRequest(BaseModel):
    reason: str = Field(default="manual", max_length=200)
    features: list[str] | None = Field(default=None, max_length=64)


class AgentProposalRequest(BaseModel):
    proposal_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentDecisionRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)
