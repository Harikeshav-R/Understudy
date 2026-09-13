"""Tournament component protocol interfaces."""

from datetime import datetime
from typing import Protocol, runtime_checkable

from understudy.contracts.evidence import (
    CandidateEvidence,
    CandidateScore,
    ProbeSample,
    TournamentResult,
)
from understudy.contracts.plan import RemediationPlan
from understudy.contracts.twin import TwinHandle
from understudy.tournament.probe import ProbeResult


@runtime_checkable
class EnvironmentProbe(Protocol):
    """Protocol for environment SLO sampling and recovery detection."""

    async def sample_once(
        self, namespace: str, target_service: str = "edge-gateway"
    ) -> ProbeSample:
        """Sample a single probe measurement from an environment."""
        raise NotImplementedError

    async def probe_environment(
        self,
        namespace: str,
        applied_at: datetime,
        forked_at: datetime | None = None,
        target_service: str = "edge-gateway",
    ) -> ProbeResult:
        """Run probe loop against environment until recovery or timeout."""
        raise NotImplementedError


@runtime_checkable
class Tournament(Protocol):
    """Orchestrates candidate observation, scoring, and arbitration."""

    async def observe_and_score(
        self, twins: list[TwinHandle], plans: list[RemediationPlan]
    ) -> tuple[list[CandidateEvidence], TournamentResult]:
        """Observe twins under mirrored traffic and compute candidate scores and result."""
        raise NotImplementedError

    def arbitrate(
        self, scores: list[CandidateScore], evidence: list[CandidateEvidence]
    ) -> TournamentResult:
        """Deterministically determine tournament outcome and winning plan."""
        raise NotImplementedError
