"""Safety kernel contract models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from understudy.contracts.enums import InvariantTier, KernelVerdictType


class Fact(BaseModel):
    """An immutable atomic fact about the system state or tournament."""

    model_config = ConfigDict(frozen=True)

    name: str
    value: Any
    source: Literal["k8s", "github", "tournament", "config", "graph"]
    observed_at: datetime


class InvariantResult(BaseModel):
    """Result of checking an individual invariant against a candidate."""

    model_config = ConfigDict(frozen=True)

    invariant_id: str
    tier: InvariantTier
    satisfied: bool | None  # None == could not decide
    unsat_core: list[str] | None = None
    reason: str


class KernelVerdict(BaseModel):
    """Comprehensive safety kernel evaluation of a candidate plan."""

    model_config = ConfigDict(frozen=True)

    incident_id: str
    plan_id: str
    verdict: KernelVerdictType
    results: list[InvariantResult] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)
    solver_ms: float
    human_reason: str  # rendered for Slack/PagerDuty
