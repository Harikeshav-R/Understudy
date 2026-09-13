"""Formal property-based tests for deterministic tournament scoring.

Implements build-plan step A4.3 and Checkpoint A4 assertions:
- Monotone in recovery time: faster recovery strictly yields equal or better score
- Disqualified candidate never wins: disqualified candidates score 1.0 and
  never beat viable candidates
- NO_ACTION can win: when active interventions cause blast/degradation, doing nothing wins
- Monotone in blast radius and downstream degradation
- Permutation invariance across candidate evaluation ordering
"""

import random
from datetime import UTC, datetime

from understudy.contracts.evidence import CandidateEvidence, ProbeSample
from understudy.contracts.twin import MirrorStats
from understudy.tournament.scorer import (
    ScoringConfig,
    score_candidate,
    score_candidates,
)


def _base_evidence(
    plan_id: str = "plan_p",
    recovered: bool = True,
    recovery_seconds: float | None = 15.0,
    observed_blast_set: list[str] | None = None,
    downstream_error_delta: float = 0.0,
    invariant_violations: list[str] | None = None,
    delivered: int = 1000,
    dropped: int = 0,
    evidence_complete: bool = True,
    probe_count: int = 60,
) -> CandidateEvidence:
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    probes = [
        ProbeSample(at=now, healthy=True, p99_latency_ms=100.0, error_rate=0.0)
        for _ in range(probe_count)
    ]
    return CandidateEvidence(
        plan_id=plan_id,
        twin_id=f"twin_{plan_id}",
        applied_at=now,
        probes=probes,
        recovered=recovered,
        recovery_seconds=recovery_seconds,
        observed_blast_set=observed_blast_set or [],
        downstream_error_delta=downstream_error_delta,
        invariant_violations=invariant_violations or [],
        mirror_stats=MirrorStats(twin_id=f"twin_{plan_id}", delivered=delivered, dropped=dropped),
        evidence_complete=evidence_complete,
    )


def test_property_monotone_in_recovery_time() -> None:
    """Assert that composite score is strictly monotone non-decreasing in recovery time.

    For any t1 <= t2:
        score(t1).composite <= score(t2).composite
    """
    cfg = ScoringConfig(recovery_timeout_seconds=180.0)
    # Dense sweep across boundary points and linear intervals
    sweep_times = [
        0.0,
        0.1,
        1.0,
        5.0,
        15.0,
        30.0,
        60.0,
        90.0,
        120.0,
        150.0,
        179.9,
        180.0,
        180.1,
        250.0,
        500.0,
    ]

    scores = [
        score_candidate(
            _base_evidence(plan_id=f"plan_{t}", recovery_seconds=t, recovered=True),
            config=cfg,
        ).composite
        for t in sweep_times
    ]

    # Monotonicity check
    for i in range(len(scores) - 1):
        assert scores[i] <= scores[i + 1], (
            f"Monotonicity violated at {sweep_times[i]} vs {sweep_times[i + 1]}"
        )

    # Unrecovered candidate should score >= any recovered candidate
    unrecovered_score = score_candidate(
        _base_evidence(plan_id="plan_unrec", recovered=False, recovery_seconds=None),
        config=cfg,
    ).composite
    assert unrecovered_score >= max(scores)


def test_property_monotone_in_recovery_time_seeded_fuzz() -> None:
    """Fuzz random pairs of recovery times and assert monotonicity."""
    rng = random.Random(42)
    cfg = ScoringConfig()

    for _ in range(200):
        t1 = rng.uniform(0.0, 300.0)
        t2 = rng.uniform(0.0, 300.0)
        t_low, t_high = min(t1, t2), max(t1, t2)

        ev_low = _base_evidence(recovery_seconds=t_low, recovered=True)
        ev_high = _base_evidence(recovery_seconds=t_high, recovered=True)

        s_low = score_candidate(ev_low, config=cfg).composite
        s_high = score_candidate(ev_high, config=cfg).composite

        assert s_low <= s_high + 1e-6, f"Score for {t_low}s ({s_low}) > {t_high}s ({s_high})"


def test_property_disqualified_never_wins() -> None:
    """Assert that a disqualified candidate scores 1.0 and never beats any qualified candidate."""
    cfg = ScoringConfig()
    rng = random.Random(1337)

    # Disqualification scenarios:
    disqualifications = [
        _base_evidence(plan_id="disq_incomplete", evidence_complete=False),
        _base_evidence(plan_id="disq_drop", dropped=200, delivered=1000),  # 200/1200 > 0.05
        _base_evidence(plan_id="disq_probes", probe_count=20),  # < 60
        _base_evidence(plan_id="disq_hard_k6", invariant_violations=["K6"]),
        _base_evidence(plan_id="disq_hard_k10", invariant_violations=["K10"]),
        _base_evidence(plan_id="disq_hard_prefix", invariant_violations=["hard:isolation"]),
    ]

    for disq_ev in disqualifications:
        disq_score = score_candidate(disq_ev, config=cfg)
        assert disq_score.disqualified is True
        assert disq_score.composite == 1.0

        # Compare against 50 randomized viable candidates
        for _ in range(50):
            viable_ev = _base_evidence(
                plan_id="viable",
                recovery_seconds=rng.uniform(1.0, 170.0),
                downstream_error_delta=rng.uniform(0.0, 0.08),
                observed_blast_set=["data-service"] if rng.random() > 0.5 else [],
                invariant_violations=["soft_warning"] if rng.random() > 0.5 else [],
            )
            viable_score = score_candidate(viable_ev, config=cfg)
            assert viable_score.disqualified is False
            assert viable_score.composite < 1.0
            assert viable_score.composite < disq_score.composite


def test_property_no_action_can_win() -> None:
    """Assert that NO_ACTION can win the tournament when all other interventions are worse.

    Per architecture §2.7:
    NO_ACTION is always available as a candidate and is scored identically.
    If doing nothing wins, doing nothing wins.
    """
    cfg = ScoringConfig()

    # NO_ACTION candidate: does nothing, causes 0 blast, 0 downstream errors, 0 violations
    no_action = _base_evidence(
        plan_id="plan_no_action",
        recovered=True,
        recovery_seconds=50.0,
        observed_blast_set=[],
        downstream_error_delta=0.0,
        invariant_violations=[],
    )

    # Active Candidate 1: Restart workload - causes high downstream error delta and large blast
    restart_candidate = _base_evidence(
        plan_id="plan_restart",
        recovered=True,
        recovery_seconds=40.0,
        observed_blast_set=["edge-gateway", "auth-service", "data-service"],
        downstream_error_delta=0.09,
        invariant_violations=["transient_unavailable"],
    )

    # Active Candidate 2: Scale workload - mask latency but causes violation and fails to recover
    scale_candidate = _base_evidence(
        plan_id="plan_scale",
        recovered=False,
        recovery_seconds=None,
        observed_blast_set=["data-service"],
        downstream_error_delta=0.05,
    )

    # Active Candidate 3: Bad deploy rollback across migration - causes hard violation
    bad_rollback = _base_evidence(
        plan_id="plan_bad_rollback",
        recovered=True,
        recovery_seconds=10.0,
        invariant_violations=["K6"],  # hard invariant
    )

    all_candidates = [no_action, restart_candidate, scale_candidate, bad_rollback]
    scores = score_candidates(all_candidates, config=cfg)

    no_action_score = next(s for s in scores if s.plan_id == "plan_no_action")
    restart_score = next(s for s in scores if s.plan_id == "plan_restart")
    scale_score = next(s for s in scores if s.plan_id == "plan_scale")
    bad_rollback_score = next(s for s in scores if s.plan_id == "plan_bad_rollback")

    assert bad_rollback_score.disqualified is True
    assert bad_rollback_score.composite == 1.0

    # NO_ACTION has the lowest composite score and is the clear winner
    assert no_action_score.composite < restart_score.composite
    assert no_action_score.composite < scale_score.composite
    assert no_action_score.composite < bad_rollback_score.composite

    winner = min(scores, key=lambda s: s.composite)
    assert winner.plan_id == "plan_no_action"


def test_property_monotone_in_blast_and_downstream() -> None:
    """Assert that scores never improve as blast radius or downstream errors increase."""
    cfg = ScoringConfig()

    # Monotonicity in blast sets
    ev_blast_0 = _base_evidence(observed_blast_set=[])
    ev_blast_1 = _base_evidence(observed_blast_set=["worker"])  # share 0.10
    ev_blast_2 = _base_evidence(observed_blast_set=["worker", "data-service"])  # share 0.30
    ev_blast_3 = _base_evidence(
        observed_blast_set=["worker", "data-service", "auth-service"]
    )  # share 0.60

    s0 = score_candidate(ev_blast_0, config=cfg).composite
    s1 = score_candidate(ev_blast_1, config=cfg).composite
    s2 = score_candidate(ev_blast_2, config=cfg).composite
    s3 = score_candidate(ev_blast_3, config=cfg).composite

    assert s0 <= s1 <= s2 <= s3

    # Monotonicity in downstream error delta
    deltas = [-0.05, 0.0, 0.02, 0.05, 0.08, 0.10, 0.15, 0.30]
    downstream_scores = [
        score_candidate(_base_evidence(downstream_error_delta=d), config=cfg).composite
        for d in deltas
    ]
    for i in range(len(downstream_scores) - 1):
        assert downstream_scores[i] <= downstream_scores[i + 1]


def test_property_permutation_invariance() -> None:
    """Assert candidate scores are independent of the ordering in which candidates are evaluated."""
    cfg = ScoringConfig()
    c1 = _base_evidence(plan_id="c1", recovery_seconds=10.0)
    c2 = _base_evidence(plan_id="c2", recovery_seconds=30.0, downstream_error_delta=0.04)
    c3 = _base_evidence(plan_id="c3", evidence_complete=False)

    order1 = [c1, c2, c3]
    order2 = [c3, c1, c2]
    order3 = [c2, c3, c1]

    scores1 = {s.plan_id: s for s in score_candidates(order1, config=cfg)}
    scores2 = {s.plan_id: s for s in score_candidates(order2, config=cfg)}
    scores3 = {s.plan_id: s for s in score_candidates(order3, config=cfg)}

    for cid in ["c1", "c2", "c3"]:
        assert scores1[cid] == scores2[cid] == scores3[cid]
