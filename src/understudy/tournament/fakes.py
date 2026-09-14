"""Deterministic fake tournament implementation."""

from collections.abc import Sequence
from datetime import datetime, timedelta

from understudy.common.clock import Clock, resolve_clock
from understudy.contracts.enums import TournamentOutcome
from understudy.contracts.evidence import (
    CandidateEvidence,
    CandidateScore,
    ProbeSample,
    TournamentResult,
)
from understudy.contracts.incident import MetricWindow
from understudy.contracts.plan import RemediationPlan
from understudy.contracts.twin import MirrorStats, TwinHandle
from understudy.tournament.api import BlastTracker, EnvironmentProbe, LLMJudge, Tournament
from understudy.tournament.blast import BlastEvaluation, EnvironmentBaseline
from understudy.tournament.judge import JudgeEvaluation
from understudy.tournament.probe import ProbeResult


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


class FakeEnvironmentProbe(EnvironmentProbe):
    """Deterministic fake environment SLO probe."""

    def __init__(
        self,
        recovered: bool = True,
        recovery_seconds: float = 12.0,
        seed: int = 42,
        clock: Clock | None = None,
    ) -> None:
        self.recovered = recovered
        self.recovery_seconds = recovery_seconds
        self.seed = seed
        self.clock: Clock = resolve_clock(clock)

    async def sample_once(
        self, namespace: str, target_service: str = "edge-gateway"
    ) -> ProbeSample:
        _ = (namespace, target_service)
        now = self.clock.now()
        return ProbeSample(
            at=now,
            healthy=self.recovered,
            p99_latency_ms=95.0 if self.recovered else 500.0,
            error_rate=0.0 if self.recovered else 0.05,
        )

    async def probe_environment(
        self,
        namespace: str,
        applied_at: datetime,
        forked_at: datetime | None = None,
        target_service: str = "edge-gateway",
    ) -> ProbeResult:
        _ = (applied_at, forked_at)
        sample = await self.sample_once(namespace, target_service)
        return ProbeResult(
            namespace=namespace,
            target_service=target_service,
            probes=[sample],
            recovered=self.recovered,
            recovery_seconds=self.recovery_seconds if self.recovered else None,
            timeout_exceeded=not self.recovered,
        )


class FakeBlastTracker(BlastTracker):
    """Deterministic fake blast tracker for testing and offline rehearsal."""

    def __init__(
        self,
        affected_services: set[str] | None = None,
        blast_radius: float = 0.0,
        downstream_error_delta: float = 0.0,
        clock: Clock | None = None,
    ) -> None:
        self.affected_services = affected_services or set()
        self.blast_radius = blast_radius
        self.downstream_error_delta = downstream_error_delta
        self.clock: Clock = resolve_clock(clock)

    async def capture_baseline(
        self,
        namespace: str,
        services: Sequence[str] | None = None,
        at: datetime | None = None,
        lookback_seconds: float | None = None,
    ) -> EnvironmentBaseline:
        now = at or self.clock.now()
        lookback = lookback_seconds or 30.0
        start = now - timedelta(seconds=lookback)
        svc_names = list(services) if services is not None else ["edge-gateway", "data-service"]
        windows = {
            s: MetricWindow(
                service=s,
                start_time=start,
                end_time=now,
                p99_latency_ms=100.0,
                error_rate=0.0,
            )
            for s in svc_names
        }
        return EnvironmentBaseline(
            namespace=namespace,
            captured_at=now,
            window_start=start,
            window_end=now,
            baselines=windows,
        )

    async def evaluate_environment(
        self,
        plan: RemediationPlan,
        namespace: str,
        baseline: EnvironmentBaseline,
        applied_at: datetime,
        until: datetime | None = None,
        services: Sequence[str] | None = None,
    ) -> BlastEvaluation:
        _ = (namespace, applied_at, until, services)
        target = plan.params.workload
        return BlastEvaluation(
            target_service=target,
            reachable_set=[target],
            affected_services=sorted(self.affected_services),
            observed_blast_set=sorted(self.affected_services),
            blast_radius=self.blast_radius,
            downstream_error_delta=self.downstream_error_delta,
            pre_apply_baselines=baseline.baselines,
            post_apply_windows={},
        )


class FakeLLMJudge(LLMJudge):
    """Deterministic fake advisory LLM judge for testing and simulation."""

    def __init__(
        self,
        ranking: list[str] | None = None,
        agree_with_first: bool = True,
        seed: int = 42,
    ) -> None:
        self.ranking = ranking
        self.agree_with_first = agree_with_first
        self.seed = seed

    async def evaluate(
        self,
        evidence: Sequence[CandidateEvidence],
    ) -> JudgeEvaluation:
        """Produce deterministic advisory ranking and reasons."""
        if not evidence:
            return JudgeEvaluation(model="fake-llm-judge")

        if self.ranking is not None:
            ranking = list(self.ranking)
            seen = set(ranking)
            for ev in evidence:
                if ev.plan_id not in seen:
                    ranking.append(ev.plan_id)
                    seen.add(ev.plan_id)
        elif self.agree_with_first:
            sorted_ev = sorted(
                evidence,
                key=lambda e: (not e.recovered, e.recovery_seconds or 999.0, e.plan_id),
            )
            ranking = [e.plan_id for e in sorted_ev]
        else:
            ranking = [e.plan_id for e in reversed(evidence)]

        reasons = {
            pid: f"Candidate {pid} ranked deterministically by FakeLLMJudge (seed={self.seed})."
            for pid in ranking
        }

        return JudgeEvaluation(
            ranking=ranking,
            reasons=reasons,
            rationale=f"Deterministic fake evaluation for {len(ranking)} candidates.",
            model="fake-llm-judge",
        )
