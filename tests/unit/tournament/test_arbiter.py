"""Unit tests for tournament arbiter and deterministic outcome evaluation.

Implements build-plan step A4.5 and tests the permanent invariant:
- test_winner_derives_only_from_deterministic_scores (AGENTS.md §6.4)
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from understudy.common.clock import FrozenClock
from understudy.common.errors import ConfigError
from understudy.contracts.enums import ActionType, TournamentOutcome
from understudy.contracts.evidence import (
    CandidateEvidence,
    CandidateScore,
    ProbeSample,
)
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.contracts.twin import MirrorStats, TwinHandle
from understudy.tournament import (
    ArbiterConfig,
    RehearsalTournament,
    TournamentArbiter,
    arbitrate,
)
from understudy.tournament.arbiter import derive_winner_from_scores
from understudy.tournament.blast import BlastEvaluation, EnvironmentBaseline
from understudy.tournament.judge import JudgeAPIError, JudgeEvaluation
from understudy.tournament.probe import ProbeResult


def _make_score(
    plan_id: str,
    composite: float,
    disqualified: bool = False,
    disqualification_reason: str | None = None,
) -> CandidateScore:
    return CandidateScore(
        plan_id=plan_id,
        composite=composite,
        components={"recovery": composite},
        disqualified=disqualified,
        disqualification_reason=disqualification_reason,
    )


def _make_evidence(
    plan_id: str,
    evidence_complete: bool = True,
    recovered: bool = True,
    recovery_seconds: float = 12.0,
    delivered: int = 1000,
    dropped: int = 0,
) -> CandidateEvidence:
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    return CandidateEvidence(
        plan_id=plan_id,
        twin_id=f"twin_{plan_id}",
        applied_at=now,
        probes=[
            ProbeSample(
                at=now,
                healthy=recovered,
                p99_latency_ms=100.0,
                error_rate=0.0,
            )
        ],
        recovered=recovered,
        recovery_seconds=recovery_seconds,
        observed_blast_set=[],
        downstream_error_delta=0.0,
        invariant_violations=[],
        mirror_stats=MirrorStats(twin_id=f"twin_{plan_id}", delivered=delivered, dropped=dropped),
        evidence_complete=evidence_complete,
    )


def test_arbiter_config_from_settings() -> None:
    """ArbiterConfig loads default ambiguity margin from settings."""
    cfg = ArbiterConfig.from_settings()
    assert cfg.ambiguity_margin == 0.15


def test_arbiter_config_from_yaml(tmp_path: Path) -> None:
    """ArbiterConfig parses YAML with top-level or nested ambiguity margin."""
    yaml1 = tmp_path / "top_level.yaml"
    yaml1.write_text("ambiguity_margin: 0.20\n", encoding="utf-8")
    cfg1 = ArbiterConfig.from_yaml(yaml1)
    assert cfg1.ambiguity_margin == 0.20

    yaml2 = tmp_path / "nested.yaml"
    yaml2.write_text("scoring:\n  ambiguity_margin: 0.18\n", encoding="utf-8")
    cfg2 = ArbiterConfig.from_yaml(yaml2)
    assert cfg2.ambiguity_margin == 0.18

    yaml3 = tmp_path / "default_fallback.yaml"
    yaml3.write_text("other_key: 123\n", encoding="utf-8")
    cfg3 = ArbiterConfig.from_yaml(yaml3)
    assert cfg3.ambiguity_margin == 0.15


def test_arbiter_config_from_yaml_errors(tmp_path: Path) -> None:
    """ArbiterConfig raises ConfigError on missing, malformed, or invalid YAML."""
    non_existent = tmp_path / "does_not_exist.yaml"
    with pytest.raises(ConfigError, match="not found"):
        ArbiterConfig.from_yaml(non_existent)

    malformed = tmp_path / "bad.yaml"
    malformed.write_text("key: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Failed to parse arbiter config"):
        ArbiterConfig.from_yaml(malformed)

    not_dict = tmp_path / "list.yaml"
    not_dict.write_text("- item1\n- item2", encoding="utf-8")
    with pytest.raises(ConfigError, match="expected dictionary"):
        ArbiterConfig.from_yaml(not_dict)

    invalid_val = tmp_path / "bad_val.yaml"
    invalid_val.write_text("ambiguity_margin: not_a_float\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid ambiguity_margin value"):
        ArbiterConfig.from_yaml(invalid_val)


def test_arbitrate_empty_scores() -> None:
    """Empty scores yields NO_VIABLE_CANDIDATE outcome."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    res = arbitrate(scores=[], clock=clock)
    assert res.outcome == TournamentOutcome.NO_VIABLE_CANDIDATE
    assert res.winner_plan_id is None
    assert res.runner_up_plan_id is None
    assert res.margin is None
    assert res.scores == []
    assert res.decided_at == clock.now()


def test_arbitrate_all_disqualified() -> None:
    """All disqualified candidates yields NO_VIABLE_CANDIDATE."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    s1 = _make_score("p1", 1.0, disqualified=True, disqualification_reason="twin_not_ready")
    s2 = _make_score("p2", 1.0, disqualified=True, disqualification_reason="evidence_incomplete")

    res = arbitrate(scores=[s1, s2], clock=clock)
    assert res.outcome == TournamentOutcome.NO_VIABLE_CANDIDATE
    assert res.winner_plan_id is None
    assert res.runner_up_plan_id is None
    assert res.margin is None


def test_arbitrate_evidence_incomplete_candidate_not_viable() -> None:
    """Candidate with evidence_complete=False is disqualified in scores and non-viable."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    s1 = _make_score("p1", 1.0, disqualified=True, disqualification_reason="evidence_incomplete")
    ev1 = _make_evidence("p1", evidence_complete=False)

    res = arbitrate(scores=[s1], evidence=[ev1], clock=clock)
    assert res.outcome == TournamentOutcome.NO_VIABLE_CANDIDATE
    assert res.winner_plan_id is None


def test_arbitrate_single_viable_candidate() -> None:
    """Single viable candidate wins clearly with calculated margin."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    s1 = _make_score("p1", 0.12, disqualified=False)

    # Sole candidate in tournament
    res_sole = arbitrate(scores=[s1], clock=clock)
    assert res_sole.outcome == TournamentOutcome.DECIDED
    assert res_sole.winner_plan_id == "p1"
    assert res_sole.runner_up_plan_id is None
    assert res_sole.margin == 0.88

    # Single viable candidate with disqualified candidate as runner-up
    s_disq = _make_score("p2", 1.0, disqualified=True, disqualification_reason="hard:K6")
    res_with_disq = arbitrate(scores=[s1, s_disq], clock=clock)
    assert res_with_disq.outcome == TournamentOutcome.DECIDED
    assert res_with_disq.winner_plan_id == "p1"
    assert res_with_disq.runner_up_plan_id == "p2"
    assert res_with_disq.margin == 0.88


def test_arbitrate_decided_with_clear_margin() -> None:
    """Candidates with score difference >= 0.15 produce DECIDED outcome."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    s1 = _make_score("p_rollback", 0.04)
    s2 = _make_score("p_restart", 0.22)
    s3 = _make_score("p_no_action", 0.40)

    cfg = ArbiterConfig(ambiguity_margin=0.15)
    res = arbitrate(scores=[s1, s2, s3], config=cfg, clock=clock)

    assert res.outcome == TournamentOutcome.DECIDED
    assert res.winner_plan_id == "p_rollback"
    assert res.runner_up_plan_id == "p_restart"
    assert res.margin == 0.18


def test_arbitrate_exact_margin_boundary() -> None:
    """Difference exactly equal to ambiguity_margin is DECIDED."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    s1 = _make_score("p1", 0.10)
    s2 = _make_score("p2", 0.25)  # margin = 0.15

    cfg = ArbiterConfig(ambiguity_margin=0.15)
    res = arbitrate(scores=[s1, s2], config=cfg, clock=clock)

    assert res.outcome == TournamentOutcome.DECIDED
    assert res.winner_plan_id == "p1"
    assert res.runner_up_plan_id == "p2"
    assert res.margin == 0.15


def test_arbitrate_ambiguous_near_tie() -> None:
    """Candidates with score difference < 0.15 produce AMBIGUOUS outcome without winner."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    s1 = _make_score("p_rollback", 0.10)
    s2 = _make_score("p_restart", 0.14)  # margin = 0.04 < 0.15

    cfg = ArbiterConfig(ambiguity_margin=0.15)
    res = arbitrate(scores=[s1, s2], config=cfg, clock=clock)

    assert res.outcome == TournamentOutcome.AMBIGUOUS
    assert res.winner_plan_id is None
    assert res.runner_up_plan_id is None
    assert res.margin == 0.04


def test_arbitrate_exact_tie_is_ambiguous() -> None:
    """Identical top scores (margin 0.0) produce AMBIGUOUS with no winner."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    s1 = _make_score("p1", 0.15)
    s2 = _make_score("p2", 0.15)

    res = arbitrate(scores=[s1, s2], clock=clock)
    assert res.outcome == TournamentOutcome.AMBIGUOUS
    assert res.winner_plan_id is None
    assert res.margin == 0.0


def test_winner_derives_only_from_deterministic_scores() -> None:
    """Assert winner derives strictly from scores and never from advisory LLM ranking.

    Permanent invariant test mandated by AGENTS.md §6.4.
    Even if an LLM advisory judge ranks a non-winner or disqualified candidate first,
    the deterministic winner is guaranteed to be chosen.
    """
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))

    s_best = _make_score("plan_best", 0.05)
    s_runner_up = _make_score("plan_runner_up", 0.30)
    s_disq = _make_score("plan_disq", 1.0, disqualified=True, disqualification_reason="K6")
    scores = [s_best, s_runner_up, s_disq]

    # Case 1: Advisory LLM judge ranks the runner-up as #1
    judge_disagree = JudgeEvaluation(
        ranking=["plan_runner_up", "plan_best", "plan_disq"],
        reasons={"plan_runner_up": "LLM preferred this"},
        rationale="Advisory LLM disagreed with telemetry",
    )
    res_disagree = arbitrate(
        scores=scores,
        judge_evaluation=judge_disagree,
        clock=clock,
    )
    assert res_disagree.outcome == TournamentOutcome.DECIDED
    assert res_disagree.winner_plan_id == "plan_best"
    assert res_disagree.runner_up_plan_id == "plan_runner_up"
    assert res_disagree.llm_agreement is False
    assert res_disagree.llm_ranking == ["plan_runner_up", "plan_best", "plan_disq"]

    # Case 2: Advisory LLM judge ranks disqualified candidate as #1
    judge_disq = JudgeEvaluation(
        ranking=["plan_disq", "plan_best", "plan_runner_up"],
        reasons={"plan_disq": "Hallucinated choice"},
    )
    res_disq = arbitrate(
        scores=scores,
        judge_evaluation=judge_disq,
        clock=clock,
    )
    assert res_disq.outcome == TournamentOutcome.DECIDED
    assert res_disq.winner_plan_id == "plan_best"
    assert res_disq.llm_agreement is False

    # Case 3: Advisory LLM judge agrees with deterministic top candidate
    judge_agree = JudgeEvaluation(
        ranking=["plan_best", "plan_runner_up", "plan_disq"],
    )
    res_agree = arbitrate(
        scores=scores,
        judge_evaluation=judge_agree,
        clock=clock,
    )
    assert res_agree.outcome == TournamentOutcome.DECIDED
    assert res_agree.winner_plan_id == "plan_best"
    assert res_agree.llm_agreement is True

    # Case 4: Explicit llm_ranking sequence override
    res_override = arbitrate(
        scores=scores,
        llm_ranking=["plan_runner_up", "plan_best"],
        clock=clock,
    )
    assert res_override.winner_plan_id == "plan_best"
    assert res_override.llm_agreement is False
    assert res_override.llm_ranking == ["plan_runner_up", "plan_best"]

    # Case 5: No LLM judge ranking provided
    res_no_llm = arbitrate(
        scores=scores,
        clock=clock,
    )
    assert res_no_llm.winner_plan_id == "plan_best"
    assert res_no_llm.llm_ranking is None
    assert res_no_llm.llm_agreement is None


def test_tournament_arbiter_class() -> None:
    """TournamentArbiter wraps arbitrate with injected config and clock."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    arbiter_instance = TournamentArbiter(
        config=ArbiterConfig(ambiguity_margin=0.20),
        clock=clock,
    )

    s1 = _make_score("p1", 0.05)
    s2 = _make_score("p2", 0.22)  # margin 0.17 < 0.20

    res = arbiter_instance.arbitrate(scores=[s1, s2], evidence=[])
    assert res.outcome == TournamentOutcome.AMBIGUOUS
    assert res.winner_plan_id is None
    assert res.margin == 0.17


@pytest.mark.asyncio
async def test_rehearsal_tournament_empty() -> None:
    """RehearsalTournament with empty inputs returns empty evidences and NO_VIABLE_CANDIDATE."""
    tournament = RehearsalTournament()
    evs, res = await tournament.observe_and_score([], [])
    assert evs == []
    assert res.outcome == TournamentOutcome.NO_VIABLE_CANDIDATE

    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    twins = [
        TwinHandle(
            twin_id="twin_0",
            incident_id="inc_test",
            candidate_index=0,
            namespace="ust-twin-0",
            database="twin_db_0",
            forked_from_snapshot_at=now,
            state="ready",
        )
    ]
    plans = [
        RemediationPlan(
            plan_id="plan_0",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=ActionParams(workload="data-service"),
            origin="planner",
            rationale="Rollback",
        )
    ]
    with pytest.raises(ConfigError, match="requires EnvironmentProbe and BlastTracker"):
        await tournament.observe_and_score(twins, plans)


@pytest.mark.asyncio
async def test_rehearsal_tournament_observe_and_score() -> None:
    """RehearsalTournament executes probe, blast, scorer, judge, and arbiter pipeline."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    now = clock.now()

    mock_probe = AsyncMock()
    mock_probe.probe_environment.return_value = ProbeResult(
        namespace="ust-twin-0",
        target_service="edge-gateway",
        probes=[
            ProbeSample(
                at=now,
                healthy=True,
                p99_latency_ms=90.0,
                error_rate=0.0,
            )
            for _ in range(60)
        ],
        recovered=True,
        recovery_seconds=15.0,
        timeout_exceeded=False,
    )

    mock_blast = AsyncMock()
    mock_blast.capture_baseline.return_value = EnvironmentBaseline(
        namespace="ust-twin-0",
        captured_at=now,
        window_start=now,
        window_end=now,
        baselines={},
    )
    mock_blast.evaluate_environment.return_value = BlastEvaluation(
        target_service="data-service",
        reachable_set=["data-service"],
        affected_services=[],
        observed_blast_set=[],
        blast_radius=0.0,
        downstream_error_delta=0.0,
        pre_apply_baselines={},
        post_apply_windows={},
    )

    mock_judge = AsyncMock()
    mock_judge.evaluate.return_value = JudgeEvaluation(
        ranking=["plan_0"],
        reasons={"plan_0": "Clean recovery"},
    )

    mock_mirror = AsyncMock()
    mock_mirror.get_stats.return_value = MirrorStats(twin_id="twin_0", delivered=1000, dropped=10)

    tournament = RehearsalTournament(
        probe=mock_probe,
        blast=mock_blast,
        judge=mock_judge,
        mirror=mock_mirror,
        clock=clock,
    )

    twins = [
        TwinHandle(
            twin_id="twin_0",
            incident_id="inc_test",
            candidate_index=0,
            namespace="ust-twin-0",
            database="twin_db_0",
            forked_from_snapshot_at=now,
            ready_at=now,
            state="ready",
        )
    ]
    plans = [
        RemediationPlan(
            plan_id="plan_0",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=ActionParams(workload="data-service"),
            origin="planner",
            rationale="Rollback bad deploy",
        )
    ]

    evidences, result = await tournament.observe_and_score(twins, plans)
    assert len(evidences) == 1
    assert evidences[0].plan_id == "plan_0"
    assert evidences[0].recovered is True
    assert result.outcome == TournamentOutcome.DECIDED
    assert result.winner_plan_id == "plan_0"
    assert result.llm_agreement is True

    # Test direct arbitrate delegation
    delegated = tournament.arbitrate(result.scores, evidences)
    assert delegated.winner_plan_id == "plan_0"


@pytest.mark.asyncio
async def test_rehearsal_tournament_judge_failure_handled_gracefully() -> None:
    """When advisory LLM judge raises an error, tournament proceeds safely without it."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    now = clock.now()

    mock_probe = AsyncMock()
    mock_probe.probe_environment.return_value = ProbeResult(
        namespace="ust-twin-0",
        target_service="edge-gateway",
        probes=[
            ProbeSample(
                at=now,
                healthy=True,
                p99_latency_ms=90.0,
                error_rate=0.0,
            )
            for _ in range(60)
        ],
        recovered=True,
        recovery_seconds=15.0,
        timeout_exceeded=False,
    )

    mock_blast = AsyncMock()
    mock_blast.capture_baseline.return_value = EnvironmentBaseline(
        namespace="ust-twin-0",
        captured_at=now,
        window_start=now,
        window_end=now,
        baselines={},
    )
    mock_blast.evaluate_environment.return_value = BlastEvaluation(
        target_service="data-service",
        reachable_set=["data-service"],
        affected_services=[],
        observed_blast_set=[],
        blast_radius=0.0,
        downstream_error_delta=0.0,
        pre_apply_baselines={},
        post_apply_windows={},
    )

    mock_judge = AsyncMock()
    mock_judge.evaluate.side_effect = JudgeAPIError("OpenRouter 503 Service Unavailable")

    tournament = RehearsalTournament(
        probe=mock_probe,
        blast=mock_blast,
        judge=mock_judge,
        clock=clock,
    )

    twins = [
        TwinHandle(
            twin_id="twin_0",
            incident_id="inc_test",
            candidate_index=0,
            namespace="ust-twin-0",
            database="twin_db_0",
            forked_from_snapshot_at=now,
            ready_at=now,
            state="ready",
        )
    ]
    plans = [
        RemediationPlan(
            plan_id="plan_0",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=ActionParams(workload="data-service"),
            origin="planner",
            rationale="Rollback bad deploy",
        )
    ]

    evidences, result = await tournament.observe_and_score(twins, plans)
    assert len(evidences) == 1
    assert result.outcome == TournamentOutcome.DECIDED
    assert result.winner_plan_id == "plan_0"
    assert result.llm_ranking is None
    assert result.llm_agreement is None


@pytest.mark.asyncio
async def test_rehearsal_tournament_without_judge() -> None:
    """RehearsalTournament operates normally when judge is None."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    now = clock.now()

    mock_probe = AsyncMock()
    mock_probe.probe_environment.return_value = ProbeResult(
        namespace="ust-twin-0",
        target_service="edge-gateway",
        probes=[
            ProbeSample(
                at=now,
                healthy=True,
                p99_latency_ms=90.0,
                error_rate=0.0,
            )
            for _ in range(60)
        ],
        recovered=True,
        recovery_seconds=15.0,
        timeout_exceeded=False,
    )

    mock_blast = AsyncMock()
    mock_blast.capture_baseline.return_value = EnvironmentBaseline(
        namespace="ust-twin-0",
        captured_at=now,
        window_start=now,
        window_end=now,
        baselines={},
    )
    mock_blast.evaluate_environment.return_value = BlastEvaluation(
        target_service="data-service",
        reachable_set=["data-service"],
        affected_services=[],
        observed_blast_set=[],
        blast_radius=0.0,
        downstream_error_delta=0.0,
        pre_apply_baselines={},
        post_apply_windows={},
    )

    tournament = RehearsalTournament(
        probe=mock_probe,
        blast=mock_blast,
        judge=None,
        clock=clock,
    )

    twins = [
        TwinHandle(
            twin_id="twin_0",
            incident_id="inc_test",
            candidate_index=0,
            namespace="ust-twin-0",
            database="twin_db_0",
            forked_from_snapshot_at=now,
            ready_at=now,
            state="ready",
        )
    ]
    plans = [
        RemediationPlan(
            plan_id="plan_0",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=ActionParams(workload="data-service"),
            origin="planner",
            rationale="Rollback bad deploy",
        )
    ]

    evidences, result = await tournament.observe_and_score(twins, plans)
    assert len(evidences) == 1
    assert result.outcome == TournamentOutcome.DECIDED
    assert result.winner_plan_id == "plan_0"
    assert result.llm_ranking is None
    assert result.llm_agreement is None


def test_derive_winner_from_scores_all_disqualified() -> None:
    """derive_winner_from_scores returns None when all candidates are disqualified or empty."""
    scores = [_make_score("p1", 0.2, disqualified=True, disqualification_reason="sla_breach")]
    assert derive_winner_from_scores(scores) is None
    assert derive_winner_from_scores([]) is None
