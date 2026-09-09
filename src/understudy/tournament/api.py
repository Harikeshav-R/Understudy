"""Tournament component protocol interfaces."""

from typing import Protocol, runtime_checkable

from understudy.contracts.evidence import (
    CandidateEvidence,
    CandidateScore,
    TournamentResult,
)
from understudy.contracts.plan import RemediationPlan
from understudy.contracts.twin import TwinHandle


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
