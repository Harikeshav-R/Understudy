"""Tournament arbiter and deterministic outcome evaluation.

Implements build-plan step A4.5, ADR-017, ADR-018, and architecture §2.7, §2.8:
- Applies the 0.15 ambiguity margin (AMBIGUITY_MARGIN).
- Deterministically decides the tournament outcome (DECIDED, AMBIGUOUS, NO_VIABLE_CANDIDATE).
- Guarantees and asserts that the winner derives strictly from deterministic scores.
- Incorporates advisory LLM judge evaluations strictly without score influence.
- Implements RehearsalTournament conforming to the Tournament protocol.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, ConfigDict

from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import get_settings
from understudy.common.errors import ConfigError, UnderstudyError
from understudy.common.logging import get_logger
from understudy.contracts.enums import TournamentOutcome
from understudy.contracts.evidence import (
    CandidateEvidence,
    CandidateScore,
    TournamentResult,
)
from understudy.contracts.plan import RemediationPlan
from understudy.contracts.twin import MirrorStats, TwinHandle
from understudy.mirror.api import MirrorRegistry
from understudy.tournament.api import (
    BlastTracker,
    CandidateScorer,
    EnvironmentProbe,
    LLMJudge,
    Tournament,
)
from understudy.tournament.judge import (
    JudgeError,
    JudgeEvaluation,
    compute_judge_agreement,
)
from understudy.tournament.scorer import DeterministicCandidateScorer

logger = get_logger(__name__)


class ArbiterConfig(BaseModel):
    """Configuration parameters for tournament arbitration."""

    model_config = ConfigDict(frozen=True)

    ambiguity_margin: float = 0.15

    @classmethod
    def from_settings(cls) -> "ArbiterConfig":
        """Load ArbiterConfig from global system settings."""
        settings = get_settings()
        return cls(
            ambiguity_margin=float(settings.scoring.ambiguity_margin),
        )

    @classmethod
    def from_yaml(cls, path: Path | str) -> "ArbiterConfig":
        """Load ArbiterConfig from a YAML file."""
        file_path = Path(path)
        if not file_path.is_file():
            raise ConfigError(f"Arbiter config YAML not found at: {file_path}")
        try:
            with file_path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (yaml.YAMLError, OSError) as exc:
            raise ConfigError(f"Failed to parse arbiter config YAML at {file_path}: {exc}") from exc

        if not isinstance(data, dict):
            raise ConfigError(f"Invalid YAML structure in {file_path}: expected dictionary")

        margin: Any = 0.15
        if "ambiguity_margin" in data:
            margin = data["ambiguity_margin"]
        elif "scoring" in data and isinstance(data["scoring"], dict):
            margin = data["scoring"].get("ambiguity_margin", 0.15)

        try:
            return cls(ambiguity_margin=float(margin))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid ambiguity_margin value in {file_path}: {exc}") from exc


def derive_winner_from_scores(
    scores: Sequence[CandidateScore],
    ambiguity_margin: float = 0.15,
) -> str | None:
    """Pure mathematical derivation of tournament winner solely from deterministic scores.

    Guarantees that winner derivation is completely independent of advisory LLM rankings,
    telemetry representations, or any non-score context per architecture §2.8.
    """
    viable = [s for s in scores if not s.disqualified]
    if not viable:
        return None
    sorted_viable = sorted(viable, key=lambda s: (s.composite, s.plan_id))
    top = sorted_viable[0]

    if len(sorted_viable) > 1:
        runner_up = sorted_viable[1]
        margin = runner_up.composite - top.composite
    else:
        other_scores = [s for s in scores if s.plan_id != top.plan_id]
        if other_scores:
            runner_up = min(other_scores, key=lambda s: (s.composite, s.plan_id))
            margin = runner_up.composite - top.composite
        else:
            margin = 1.0 - top.composite

    if margin < ambiguity_margin - 1e-6:
        return None
    return top.plan_id


def arbitrate(
    scores: Sequence[CandidateScore],
    evidence: Sequence[CandidateEvidence] | None = None,
    llm_ranking: Sequence[str] | None = None,
    judge_evaluation: JudgeEvaluation | None = None,
    incident_id: str = "inc_default",
    config: ArbiterConfig | None = None,
    clock: Clock | None = None,
) -> TournamentResult:
    """Deterministically arbitrate candidate scores and rehearsal evidence.

    Per ADR-017, ADR-018, and architecture §2.7-§2.8:
    1. Only non-disqualified candidates are viable (scoring encodes evidence completeness).
    2. Candidates are ranked deterministically by composite score (lower is better).
    3. The winner must beat the runner-up by at least ambiguity_margin (default 0.15).
       If margin < 0.15, the outcome is AMBIGUOUS and winner_plan_id is None.
    4. If no candidate is viable, the outcome is NO_VIABLE_CANDIDATE.
    5. The winner derives STRICTLY from deterministic scores; advisory LLM rankings
       are recorded for agreement metrics but never influence winner_plan_id.

    Args:
        scores: Deterministic candidate scores from CandidateScorer.
        evidence: Optional empirical rehearsal evidence gathered from digital twins.
        llm_ranking: Optional ordered list of plan IDs from advisory LLM judge.
        judge_evaluation: Optional JudgeEvaluation from AdvisoryLLMJudge.
        incident_id: Incident correlation ID.
        config: Arbitration configuration with ambiguity margin threshold.
        clock: Time provider for stamping decided_at.

    Returns:
        TournamentResult containing outcome, scores, rankings, winner, and margin.
    """
    cfg = config or ArbiterConfig.from_settings()
    now = resolve_clock(clock).now()
    del evidence  # Winner derives purely from scores per ADR-017 / architecture §2.8

    if not scores:
        logger.info("tournament_no_viable_candidate", reason="empty_scores")
        return TournamentResult(
            incident_id=incident_id,
            outcome=TournamentOutcome.NO_VIABLE_CANDIDATE,
            scores=[],
            decided_at=now,
        )

    # Determine effective advisory LLM ranking if provided
    effective_llm_ranking: list[str] | None = None
    if llm_ranking is not None:
        effective_llm_ranking = list(llm_ranking)
    elif judge_evaluation is not None and judge_evaluation.ranking:
        effective_llm_ranking = list(judge_evaluation.ranking)

    # Filter viable candidates: non-disqualified (scoring already encodes evidence completeness)
    viable_scores = [s for s in scores if not s.disqualified]

    if not viable_scores:
        logger.info(
            "tournament_no_viable_candidate",
            candidate_count=len(scores),
            reason="all_disqualified_or_incomplete",
        )
        return TournamentResult(
            incident_id=incident_id,
            outcome=TournamentOutcome.NO_VIABLE_CANDIDATE,
            scores=list(scores),
            llm_ranking=effective_llm_ranking,
            llm_agreement=None,
            winner_plan_id=None,
            runner_up_plan_id=None,
            margin=None,
            decided_at=now,
        )

    # Sort viable candidates deterministically: composite ascending (lower is better)
    sorted_viable = sorted(viable_scores, key=lambda s: (s.composite, s.plan_id))
    top_candidate = sorted_viable[0]

    # Determine runner-up candidate and score margin
    runner_up_id: str | None = None
    runner_up_candidate: CandidateScore | None = None
    if len(sorted_viable) > 1:
        runner_up_candidate = sorted_viable[1]
        runner_up_id = runner_up_candidate.plan_id
        margin = round(runner_up_candidate.composite - top_candidate.composite, 4)
    else:
        # Single viable candidate; evaluate against disqualified runner-up if present
        other_scores = [s for s in scores if s.plan_id != top_candidate.plan_id]
        if other_scores:
            sorted_others = sorted(other_scores, key=lambda s: (s.composite, s.plan_id))
            runner_up_candidate = sorted_others[0]
            runner_up_id = runner_up_candidate.plan_id
            margin = round(runner_up_candidate.composite - top_candidate.composite, 4)
        else:
            runner_up_candidate = None
            runner_up_id = None
            margin = round(1.0 - top_candidate.composite, 4)

    # Evaluate ambiguity margin (ADR-018):
    # Winner must beat the runner-up (or baseline 1.0) by at least ambiguity_margin.
    is_ambiguous = margin < (cfg.ambiguity_margin - 1e-6)

    if is_ambiguous:
        outcome = TournamentOutcome.AMBIGUOUS
        winner_plan_id = None
        runner_up_plan_id = None
    else:
        outcome = TournamentOutcome.DECIDED
        winner_plan_id = top_candidate.plan_id
        runner_up_plan_id = runner_up_id

    # Prime Directive (ADR-017 & architecture §2.8):
    # Winner derives strictly and solely from deterministic scores.
    score_only_winner = derive_winner_from_scores(scores, cfg.ambiguity_margin)
    assert winner_plan_id == score_only_winner, (
        f"Tournament winner '{winner_plan_id}' does not match deterministic score-only winner "
        f"'{score_only_winner}'"
    )

    # Compute top-1 agreement between advisory LLM ranking and deterministic winner
    llm_agreement = (
        compute_judge_agreement(effective_llm_ranking, winner_plan_id)
        if winner_plan_id is not None
        else None
    )

    if outcome == TournamentOutcome.DECIDED:
        logger.info(
            "tournament_decided",
            winner=winner_plan_id,
            runner_up=runner_up_plan_id,
            margin=margin,
            llm_agreement=llm_agreement,
        )
    else:
        logger.info(
            "tournament_ambiguous",
            margin=margin,
            ambiguity_margin=cfg.ambiguity_margin,
            candidate_count=len(scores),
        )

    return TournamentResult(
        incident_id=incident_id,
        outcome=outcome,
        scores=list(scores),
        llm_ranking=effective_llm_ranking,
        llm_agreement=llm_agreement,
        winner_plan_id=winner_plan_id,
        runner_up_plan_id=runner_up_plan_id,
        margin=margin,
        decided_at=now,
    )


class TournamentArbiter:
    """Standalone deterministic arbiter for candidate rehearsal tournaments."""

    def __init__(
        self,
        config: ArbiterConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config or ArbiterConfig.from_settings()
        self.clock = resolve_clock(clock)

    def arbitrate(
        self,
        scores: list[CandidateScore],
        evidence: list[CandidateEvidence] | None = None,
        llm_ranking: Sequence[str] | None = None,
        judge_evaluation: JudgeEvaluation | None = None,
        incident_id: str = "inc_default",
    ) -> TournamentResult:
        """Arbitrate candidate scores and rehearsal evidence."""
        return arbitrate(
            scores=scores,
            evidence=evidence,
            llm_ranking=llm_ranking,
            judge_evaluation=judge_evaluation,
            incident_id=incident_id,
            config=self.config,
            clock=self.clock,
        )


class RehearsalTournament(Tournament):
    """Production tournament orchestrating candidate observation, scoring, and arbitration."""

    def __init__(
        self,
        probe: EnvironmentProbe | None = None,
        blast: BlastTracker | None = None,
        scorer: CandidateScorer | None = None,
        judge: LLMJudge | None = None,
        mirror: MirrorRegistry | None = None,
        arbiter: TournamentArbiter | None = None,
        config: ArbiterConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.clock = resolve_clock(clock)
        self.config = config or ArbiterConfig.from_settings()
        self.probe = probe
        self.blast = blast
        self.scorer = scorer or DeterministicCandidateScorer()
        self.judge = judge
        self.mirror = mirror
        self.arbiter = arbiter or TournamentArbiter(config=self.config, clock=self.clock)

    async def observe_and_score(
        self, twins: list[TwinHandle], plans: list[RemediationPlan]
    ) -> tuple[list[CandidateEvidence], TournamentResult]:
        """Observe twins under mirrored traffic and compute candidate scores and result."""
        now = self.clock.now()
        if not twins or not plans:
            empty_res = self.arbiter.arbitrate([], [])
            return [], empty_res

        if self.probe is None or self.blast is None:
            raise ConfigError(
                "RehearsalTournament requires EnvironmentProbe and BlastTracker to observe twins"
            )

        evidences: list[CandidateEvidence] = []
        for twin, plan in zip(twins, plans, strict=False):
            applied_at = twin.ready_at or now

            # 1. Capture pre-apply telemetry baseline BEFORE candidate rehearsal (architecture §2.6)
            baseline = await self.blast.capture_baseline(namespace=twin.namespace, at=applied_at)

            # 2. Probe environment under mirrored traffic until recovery or timeout
            probe_result = await self.probe.probe_environment(
                namespace=twin.namespace,
                applied_at=applied_at,
                forked_at=twin.forked_from_snapshot_at,
            )

            # 3. Evaluate post-apply blast radius against baseline
            blast_result = await self.blast.evaluate_environment(
                plan=plan,
                namespace=twin.namespace,
                baseline=baseline,
                applied_at=applied_at,
            )

            if self.mirror is not None:
                mirror_stats = await self.mirror.get_stats(twin.twin_id)
            else:
                mirror_stats = MirrorStats(twin_id=twin.twin_id, delivered=0, dropped=0)

            evidence_complete = (
                probe_result.recovered or len(probe_result.probes) >= 60
            ) and mirror_stats.drop_ratio <= 0.05

            ev = CandidateEvidence(
                plan_id=plan.plan_id,
                twin_id=twin.twin_id,
                applied_at=applied_at,
                probes=probe_result.probes,
                recovered=probe_result.recovered,
                recovery_seconds=probe_result.recovery_seconds,
                observed_blast_set=blast_result.observed_blast_set,
                downstream_error_delta=blast_result.downstream_error_delta,
                invariant_violations=[],
                mirror_stats=mirror_stats,
                evidence_complete=evidence_complete,
            )
            evidences.append(ev)

        scores = self.scorer.score(evidences)

        judge_eval: JudgeEvaluation | None = None
        if self.judge is not None:
            try:
                judge_eval = await self.judge.evaluate(evidences)
            except (JudgeError, httpx.HTTPError, UnderstudyError) as exc:
                logger.warning("llm_judge_evaluation_failed", error=str(exc))
                judge_eval = None

        incident_id = twins[0].incident_id if twins else "inc_default"
        result = self.arbiter.arbitrate(
            scores=scores,
            evidence=evidences,
            judge_evaluation=judge_eval,
            incident_id=incident_id,
        )
        return evidences, result

    def arbitrate(
        self, scores: list[CandidateScore], evidence: list[CandidateEvidence]
    ) -> TournamentResult:
        """Deterministically determine tournament outcome and winning plan."""
        return self.arbiter.arbitrate(scores=scores, evidence=evidence)
