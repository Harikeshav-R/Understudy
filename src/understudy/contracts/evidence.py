"""Tournament evidence and scoring contract models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from understudy.contracts.enums import TournamentOutcome
from understudy.contracts.twin import MirrorStats


class ProbeSample(BaseModel):
    """A single SLO probe sample observed against an environment."""

    model_config = ConfigDict(frozen=True)

    at: datetime
    healthy: bool
    p99_latency_ms: float
    error_rate: float


class CandidateEvidence(BaseModel):
    """Empirical evidence gathered during rehearsal of a remediation plan."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    twin_id: str
    applied_at: datetime
    probes: list[ProbeSample] = Field(default_factory=list)
    recovered: bool
    recovery_seconds: float | None = None  # None if never recovered
    observed_blast_set: list[str] = Field(default_factory=list)
    downstream_error_delta: float = 0.0  # post-apply error rate minus pre-apply, downstream only
    invariant_violations: list[str] = Field(
        default_factory=list
    )  # runtime-tier violations observed in the twin
    mirror_stats: MirrorStats
    evidence_complete: bool  # false if drop ratio or sample count fails fidelity check


class CandidateScore(BaseModel):
    """Authoritative deterministic score computed for a candidate."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    composite: float  # 0.0 best .. 1.0 worst
    components: dict[str, float] = Field(default_factory=dict)  # named subscores, all normalised
    disqualified: bool = False
    disqualification_reason: str | None = None


class TournamentResult(BaseModel):
    """Outcome and rankings from the candidate tournament."""

    model_config = ConfigDict(frozen=True)

    incident_id: str
    outcome: TournamentOutcome
    scores: list[CandidateScore] = Field(default_factory=list)  # deterministic, authoritative
    llm_ranking: list[str] | None = None  # advisory, ordered plan_ids
    llm_agreement: bool | None = None  # top-1 agreement with deterministic
    winner_plan_id: str | None = None
    runner_up_plan_id: str | None = None
    margin: float | None = None
    decided_at: datetime
