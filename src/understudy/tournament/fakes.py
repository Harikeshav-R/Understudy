"""Deterministic fake tournament implementation."""

from understudy.common.clock import Clock, resolve_clock
from understudy.contracts.enums import TournamentOutcome
from understudy.contracts.evidence import (
    CandidateEvidence,
    CandidateScore,
    ProbeSample,
    TournamentResult,
)
from understudy.contracts.plan import RemediationPlan
from understudy.contracts.twin import MirrorStats, TwinHandle
from understudy.tournament.api import Tournament


class FakeTournament(Tournament):
    """Deterministic tournament observer, scorer, and arbiter."""

    def __init__(
        self,
        force_ambiguous: bool = False,
        seed: int = 42,
        clock: Clock | None = None,
    ) -> None:
        self.force_ambiguous = force_ambiguous
        self.seed = seed
        self.clock: Clock = resolve_clock(clock)

    async def observe_and_score(
        self, twins: list[TwinHandle], plans: list[RemediationPlan]
    ) -> tuple[list[CandidateEvidence], TournamentResult]:
        """Produce deterministic rehearsal evidence and tournament result."""
        now = self.clock.now()
        evidences: list[CandidateEvidence] = []
        scores: list[CandidateScore] = []

        for idx, (twin, plan) in enumerate(zip(twins, plans, strict=False)):
            recovered = idx == 0
            rec_seconds = 12.0 if recovered else None
            probe = ProbeSample(
                at=now,
                healthy=recovered,
                p99_latency_ms=95.0 if recovered else 500.0,
                error_rate=0.0 if recovered else 0.05,
            )
            ev = CandidateEvidence(
                plan_id=plan.plan_id,
                twin_id=twin.twin_id,
                applied_at=now,
                probes=[probe],
                recovered=recovered,
                recovery_seconds=rec_seconds,
                observed_blast_set=list(plan.declared_blast_set),
                downstream_error_delta=0.0,
                invariant_violations=[],
                mirror_stats=MirrorStats(twin_id=twin.twin_id, delivered=100, dropped=0),
                evidence_complete=True,
            )
            evidences.append(ev)

            composite = 0.10 + (0.30 * idx) + ((self.seed % 10) * 0.001)
            score = CandidateScore(
                plan_id=plan.plan_id,
                composite=composite,
                components={"recovery": 0.05 * idx, "blast": 0.05 * idx},
                disqualified=False,
            )
            scores.append(score)

        result = self.arbitrate(scores, evidences)
        return evidences, result

    def arbitrate(
        self, scores: list[CandidateScore], evidence: list[CandidateEvidence]
    ) -> TournamentResult:
        """Arbitrate tournament result deterministically."""
        _ = evidence
        now = self.clock.now()
        incident_id = "inc_default"
        if not scores:
            return TournamentResult(
                incident_id=incident_id,
                outcome=TournamentOutcome.NO_VIABLE_CANDIDATE,
                scores=[],
                decided_at=now,
            )

        sorted_scores = sorted(scores, key=lambda s: s.composite)
        winner = sorted_scores[0]
        runner_up = sorted_scores[1] if len(sorted_scores) > 1 else None
        margin = (runner_up.composite - winner.composite) if runner_up else 1.0

        if self.force_ambiguous:
            return TournamentResult(
                incident_id=incident_id,
                outcome=TournamentOutcome.AMBIGUOUS,
                scores=scores,
                llm_ranking=[s.plan_id for s in sorted_scores],
                llm_agreement=False,
                winner_plan_id=None,
                runner_up_plan_id=None,
                margin=margin,
                decided_at=now,
            )

        return TournamentResult(
            incident_id=incident_id,
            outcome=TournamentOutcome.DECIDED,
            scores=scores,
            llm_ranking=[s.plan_id for s in sorted_scores],
            llm_agreement=True,
            winner_plan_id=winner.plan_id,
            runner_up_plan_id=runner_up.plan_id if runner_up else None,
            margin=margin,
            decided_at=now,
        )
