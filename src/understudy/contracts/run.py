"""Run record and execution result contract models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from understudy.contracts.enums import RunOutcome
from understudy.contracts.evidence import CandidateEvidence, TournamentResult
from understudy.contracts.incident import IncidentContext
from understudy.contracts.kernel import KernelVerdict
from understudy.contracts.plan import RemediationPlan


class RunRecord(BaseModel):
    """Append-only record of an incident response run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    incident_id: str
    scenario_id: str | None = None  # set when driven by the eval harness
    started_at: datetime
    finished_at: datetime | None = None
    outcome: RunOutcome
    context: IncidentContext
    plans: list[RemediationPlan] = Field(default_factory=list)
    evidence: list[CandidateEvidence] = Field(default_factory=list)
    tournament: TournamentResult | None = None
    verdict: KernelVerdict | None = None
    prod_applied_plan_id: str | None = None
    prod_outcome: Literal["resolved", "not_resolved", "worsened"] | None = None
    escalation_reason: str | None = None
