"""Tournament component protocol interfaces."""

from collections.abc import Sequence
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
from understudy.tournament.blast import BlastEvaluation, EnvironmentBaseline
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
class BlastTracker(Protocol):
    """Protocol for capturing pre-apply environment baselines and evaluating blast radius."""

    async def capture_baseline(
        self,
        namespace: str,
        services: Sequence[str] | None = None,
        at: datetime | None = None,
        lookback_seconds: float | None = None,
    ) -> EnvironmentBaseline:
        """Capture pre-apply telemetry metrics baseline for an environment."""
        raise NotImplementedError

    async def evaluate_environment(
        self,
        plan: RemediationPlan,
        namespace: str,
        baseline: EnvironmentBaseline,
        applied_at: datetime,
        until: datetime | None = None,
        services: Sequence[str] | None = None,
    ) -> BlastEvaluation:
        """Capture post-apply telemetry and evaluate blast radius against baseline."""
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


@runtime_checkable
class CandidateScorer(Protocol):
    """Protocol for deterministic candidate scoring."""

    def score(
        self,
        evidences: Sequence[CandidateEvidence],
        twins_ready: dict[str, bool] | None = None,
        blast_scores: dict[str, float] | None = None,
    ) -> list[CandidateScore]:
        """Score candidate evidence deterministically."""
        raise NotImplementedError
