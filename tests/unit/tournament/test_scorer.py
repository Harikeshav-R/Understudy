"""Comprehensive unit tests for deterministic candidate scorer and disqualification rules."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from understudy.common.errors import ConfigError
from understudy.contracts.evidence import CandidateEvidence, CandidateScore, ProbeSample
from understudy.contracts.incident import DependencyGraphSnapshot
from understudy.contracts.twin import MirrorStats
from understudy.graph.api import DependencyGraph
from understudy.tournament.api import CandidateScorer
from understudy.tournament.scorer import (
    DEFAULT_SERVICE_REQUEST_SHARES,
    DeterministicCandidateScorer,
    ScoringConfig,
    check_disqualification,
    compute_blast_subscore,
    compute_downstream_subscore,
    compute_recovery_subscore,
    compute_violations_subscore,
    load_scoring_config,
    score_candidate,
    score_candidates,
)


def _make_evidence(
    plan_id: str = "plan_test",
    twin_id: str = "twin_test",
    recovered: bool = True,
    recovery_seconds: float | None = 18.0,
    observed_blast_set: list[str] | None = None,
    downstream_error_delta: float = 0.02,
    invariant_violations: list[str] | None = None,
    delivered: int = 1000,
    dropped: int = 10,
    evidence_complete: bool = True,
    probe_count: int = 60,
) -> CandidateEvidence:
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    probes = [
        ProbeSample(
            at=now,
            healthy=True,
            p99_latency_ms=120.0,
            error_rate=0.0,
        )
        for _ in range(probe_count)
    ]
    return CandidateEvidence(
        plan_id=plan_id,
        twin_id=twin_id,
        applied_at=now,
        probes=probes,
        recovered=recovered,
        recovery_seconds=recovery_seconds,
        observed_blast_set=observed_blast_set or [],
        downstream_error_delta=downstream_error_delta,
        invariant_violations=invariant_violations or [],
        mirror_stats=MirrorStats(twin_id=twin_id, delivered=delivered, dropped=dropped),
        evidence_complete=evidence_complete,
    )


class _DummyGraph(DependencyGraph):
    def dependents(self, service: str) -> set[str]:
        _ = service
        return set()

    def reachable_set(self, service: str) -> set[str]:
        _ = service
        return set()

    def request_share(self, service: str) -> float:
        shares = {"edge-gateway": 0.50, "auth-service": 0.50}
        return shares.get(service, 0.0)

    def snapshot(self) -> DependencyGraphSnapshot:
        return DependencyGraphSnapshot(nodes=[], edges=[], observed_at=datetime.now(UTC))


def test_scoring_config_defaults_and_validation() -> None:
    cfg = ScoringConfig()
    assert cfg.recovery_weight == 0.40
    assert cfg.blast_weight == 0.25
    assert cfg.downstream_weight == 0.20
    assert cfg.violations_weight == 0.15
    assert cfg.recovery_timeout_seconds == 180.0
    assert cfg.downstream_error_ceiling == 0.10
    assert cfg.violation_ceiling == 5
    assert cfg.mirror_drop_ceiling == 0.05
    assert cfg.min_probe_samples == 60
    assert cfg.hard_invariants == ("K6", "K10")


def test_scoring_config_weights_must_sum_to_one() -> None:
    with pytest.raises(ConfigError, match=r"Scoring weights must sum to 1.0"):
        ScoringConfig(
            recovery_weight=0.50,
            blast_weight=0.20,
            downstream_weight=0.20,
            violations_weight=0.20,
        )


def test_scoring_config_from_settings() -> None:
    cfg = ScoringConfig.from_settings()
    assert cfg.recovery_weight == 0.40
    assert cfg.blast_weight == 0.25
    assert cfg.downstream_weight == 0.20
    assert cfg.violations_weight == 0.15


def test_scoring_config_from_yaml_valid(tmp_path: Path) -> None:
    yaml_file = tmp_path / "custom_scoring.yaml"
    yaml_file.write_text(
        "recovery_weight: 0.30\n"
        "blast_weight: 0.30\n"
        "downstream_weight: 0.20\n"
        "violations_weight: 0.20\n"
        "recovery_timeout_seconds: 120.0\n"
        "downstream_error_ceiling: 0.08\n"
        "violation_ceiling: 4\n"
        "mirror_drop_ceiling: 0.04\n"
        "min_probe_samples: 50\n"
    )
    cfg = ScoringConfig.from_yaml(yaml_file)
    assert cfg.recovery_weight == 0.30
    assert cfg.blast_weight == 0.30
    assert cfg.recovery_timeout_seconds == 120.0
    assert cfg.downstream_error_ceiling == 0.08
    assert cfg.violation_ceiling == 4
    assert cfg.mirror_drop_ceiling == 0.04
    assert cfg.min_probe_samples == 50


def test_scoring_config_from_yaml_nested_structure(tmp_path: Path) -> None:
    yaml_file = tmp_path / "nested_scoring.yaml"
    yaml_file.write_text(
        "weights:\n"
        "  recovery: 0.35\n"
        "  blast: 0.25\n"
        "  downstream: 0.25\n"
        "  violations: 0.15\n"
        "thresholds:\n"
        "  recovery_timeout_seconds: 150.0\n"
        "  downstream_error_ceiling: 0.15\n"
    )
    cfg = ScoringConfig.from_yaml(yaml_file)
    assert cfg.recovery_weight == 0.35
    assert cfg.blast_weight == 0.25
    assert cfg.downstream_weight == 0.25
    assert cfg.violations_weight == 0.15
    assert cfg.recovery_timeout_seconds == 150.0
    assert cfg.downstream_error_ceiling == 0.15


def test_scoring_config_from_yaml_errors(tmp_path: Path) -> None:
    missing_file = tmp_path / "nonexistent.yaml"
    with pytest.raises(ConfigError, match=r"YAML not found"):
        ScoringConfig.from_yaml(missing_file)

    bad_syntax = tmp_path / "bad.yaml"
    bad_syntax.write_text("weights: [unclosed\n")
    with pytest.raises(ConfigError, match=r"Failed to parse scoring config"):
        ScoringConfig.from_yaml(bad_syntax)

    non_dict = tmp_path / "scalar.yaml"
    non_dict.write_text("just-a-string\n")
    with pytest.raises(ConfigError, match=r"expected dictionary"):
        ScoringConfig.from_yaml(non_dict)

    invalid_val = tmp_path / "invalid_weights.yaml"
    invalid_val.write_text(
        "recovery_weight: 0.90\n"
        "blast_weight: 0.90\n"
        "downstream_weight: 0.0\n"
        "violations_weight: 0.0\n"
    )
    with pytest.raises(ConfigError, match=r"Failed to validate scoring configuration"):
        ScoringConfig.from_yaml(invalid_val)


def test_load_scoring_config_fallback(tmp_path: Path) -> None:
    p = tmp_path / "exists.yaml"
    p.write_text(
        "recovery_weight: 0.40\n"
        "blast_weight: 0.25\n"
        "downstream_weight: 0.20\n"
        "violations_weight: 0.15\n"
    )
    loaded = load_scoring_config(p)
    assert loaded.recovery_weight == 0.40

    non_existent = tmp_path / "not_there.yaml"
    fallback = load_scoring_config(non_existent)
    assert fallback.recovery_weight == 0.40


def test_compute_recovery_subscore() -> None:
    # Not recovered
    assert compute_recovery_subscore(recovered=False, recovery_seconds=10.0) == 1.0
    assert compute_recovery_subscore(recovered=True, recovery_seconds=None) == 1.0

    # Zero timeout edge case
    assert (
        compute_recovery_subscore(recovered=True, recovery_seconds=10.0, timeout_seconds=0.0) == 1.0
    )

    # Normal recovery within bounds
    assert (
        compute_recovery_subscore(recovered=True, recovery_seconds=0.0, timeout_seconds=180.0)
        == 0.0
    )
    assert (
        compute_recovery_subscore(recovered=True, recovery_seconds=90.0, timeout_seconds=180.0)
        == 0.5
    )
    assert (
        compute_recovery_subscore(recovered=True, recovery_seconds=180.0, timeout_seconds=180.0)
        == 1.0
    )

    # Over timeout boundary clamping
    assert (
        compute_recovery_subscore(recovered=True, recovery_seconds=200.0, timeout_seconds=180.0)
        == 1.0
    )

    # Negative recovery seconds clamped to 0.0
    assert (
        compute_recovery_subscore(recovered=True, recovery_seconds=-5.0, timeout_seconds=180.0)
        == 0.0
    )


def test_compute_blast_subscore() -> None:
    # Explicit blast_radius takes precedence
    assert compute_blast_subscore(observed_blast_set=["data-service"], blast_radius=0.15) == 0.15
    assert compute_blast_subscore(observed_blast_set=[], blast_radius=1.5) == 1.0
    assert compute_blast_subscore(observed_blast_set=[], blast_radius=-0.2) == 0.0

    # Empty blast set yields 0.0
    assert compute_blast_subscore(observed_blast_set=[]) == 0.0

    # Using DEFAULT_SERVICE_REQUEST_SHARES: edge-gateway: 0.40, auth-service: 0.30
    assert (
        compute_blast_subscore(observed_blast_set=["edge-gateway"])
        == DEFAULT_SERVICE_REQUEST_SHARES["edge-gateway"]
    )

    assert compute_blast_subscore(observed_blast_set=["edge-gateway", "auth-service"]) == 0.70
    assert (
        compute_blast_subscore(
            observed_blast_set=["edge-gateway", "auth-service", "data-service", "worker"]
        )
        == 1.00
    )

    # Unknown service in DEFAULT fallback gets 0.25 default
    assert compute_blast_subscore(observed_blast_set=["custom-service"]) == 0.25

    # Caller-specified request shares
    custom_shares = {"s1": 0.2, "s2": 0.3}
    assert (
        compute_blast_subscore(observed_blast_set=["s1", "s2"], request_shares=custom_shares) == 0.5
    )
    assert (
        compute_blast_subscore(observed_blast_set=["unknown"], request_shares=custom_shares) == 0.0
    )

    # Graph-backed calculation
    dummy_graph = _DummyGraph()
    assert compute_blast_subscore(observed_blast_set=["edge-gateway"], graph=dummy_graph) == 0.50


def test_compute_downstream_subscore() -> None:
    # Zero or negative delta
    assert compute_downstream_subscore(downstream_error_delta=-0.05, ceiling=0.10) == 0.0
    assert compute_downstream_subscore(downstream_error_delta=0.0, ceiling=0.10) == 0.0

    # Positive delta within ceiling
    assert compute_downstream_subscore(downstream_error_delta=0.05, ceiling=0.10) == 0.50
    assert compute_downstream_subscore(downstream_error_delta=0.10, ceiling=0.10) == 1.00

    # Delta exceeding ceiling clamped to 1.0
    assert compute_downstream_subscore(downstream_error_delta=0.20, ceiling=0.10) == 1.00

    # Degenerate zero ceiling edge case
    assert compute_downstream_subscore(downstream_error_delta=0.01, ceiling=0.0) == 1.0
    assert compute_downstream_subscore(downstream_error_delta=0.0, ceiling=0.0) == 0.0


def test_compute_violations_subscore() -> None:
    # Zero violations
    assert compute_violations_subscore(runtime_violation_count=0, ceiling=5) == 0.0

    # Violations within ceiling
    assert compute_violations_subscore(runtime_violation_count=1, ceiling=5) == 0.20
    assert compute_violations_subscore(runtime_violation_count=3, ceiling=5) == 0.60
    assert compute_violations_subscore(runtime_violation_count=5, ceiling=5) == 1.00

    # Violations exceeding ceiling clamped to 1.0
    assert compute_violations_subscore(runtime_violation_count=10, ceiling=5) == 1.00

    # Degenerate zero ceiling edge case
    assert compute_violations_subscore(runtime_violation_count=1, ceiling=0) == 1.0
    assert compute_violations_subscore(runtime_violation_count=0, ceiling=0) == 0.0


def test_disqualification_twin_not_ready() -> None:
    cfg = ScoringConfig()
    ev = _make_evidence()
    disq, reason = check_disqualification(evidence=ev, config=cfg, twin_ready=False)
    assert disq is True
    assert reason == "twin_not_ready"

    ev_violation = _make_evidence(invariant_violations=["twin_not_ready"])
    disq2, reason2 = check_disqualification(evidence=ev_violation, config=cfg, twin_ready=True)
    assert disq2 is True
    assert reason2 == "twin_not_ready"


def test_disqualification_hard_invariant_violations() -> None:
    cfg = ScoringConfig(hard_invariants=("K6", "K10"))

    # K6 hard violation
    ev_k6 = _make_evidence(invariant_violations=["K6"])
    disq, reason = check_disqualification(evidence=ev_k6, config=cfg)
    assert disq is True
    assert reason == "hard_invariant_violation: K6"

    # hard: prefix violation
    ev_hard = _make_evidence(invariant_violations=["hard:custom_policy"])
    disq_h, reason_h = check_disqualification(evidence=ev_hard, config=cfg)
    assert disq_h is True
    assert reason_h == "hard_invariant_violation: hard:custom_policy"

    # Soft violation does NOT disqualify
    ev_soft = _make_evidence(invariant_violations=["soft:latency_warning"])
    disq_s, reason_s = check_disqualification(evidence=ev_soft, config=cfg)
    assert disq_s is False
    assert reason_s is None


def test_disqualification_evidence_incomplete() -> None:
    cfg = ScoringConfig(mirror_drop_ceiling=0.05, min_probe_samples=60)

    # Flag explicit false
    ev_incomplete = _make_evidence(evidence_complete=False)
    disq, reason = check_disqualification(evidence=ev_incomplete, config=cfg)
    assert disq is True
    assert reason == "evidence_incomplete"

    # Drop ratio exceeds ceiling (100 drops / 1100 total = 0.0909 > 0.05)
    ev_drop = _make_evidence(delivered=1000, dropped=100)
    disq_d, reason_d = check_disqualification(evidence=ev_drop, config=cfg)
    assert disq_d is True
    assert reason_d == "evidence_incomplete"

    # Probe count below minimum (59 < 60)
    ev_probes = _make_evidence(probe_count=59)
    disq_p, reason_p = check_disqualification(evidence=ev_probes, config=cfg)
    assert disq_p is True
    assert reason_p == "evidence_incomplete"


def test_score_candidate_qualified() -> None:
    cfg = ScoringConfig(
        recovery_weight=0.40,
        blast_weight=0.25,
        downstream_weight=0.20,
        violations_weight=0.15,
        recovery_timeout_seconds=180.0,
        downstream_error_ceiling=0.10,
        violation_ceiling=5,
    )
    # recovery = 18.0 / 180.0 = 0.10
    # blast = observed ["data-service"] => DEFAULT_DEMO_REQUEST_SHARES["data-service"] = 0.20
    # downstream = 0.02 / 0.10 = 0.20
    # violations = 1 / 5 = 0.20
    # expected composite = 0.40*0.10 + 0.25*0.20 + 0.20*0.20 + 0.15*0.20
    #                    = 0.04 + 0.05 + 0.04 + 0.03 = 0.16
    ev = _make_evidence(
        plan_id="plan_1",
        recovered=True,
        recovery_seconds=18.0,
        observed_blast_set=["data-service"],
        downstream_error_delta=0.02,
        invariant_violations=["soft_warning"],
    )

    score = score_candidate(evidence=ev, config=cfg)
    assert isinstance(score, CandidateScore)
    assert score.plan_id == "plan_1"
    assert score.disqualified is False
    assert score.disqualification_reason is None
    assert score.composite == 0.16
    assert score.components == {
        "recovery": 0.10,
        "blast": 0.20,
        "downstream": 0.20,
        "violations": 0.20,
    }


def test_score_candidate_disqualified_sets_composite_to_one() -> None:
    cfg = ScoringConfig()
    ev = _make_evidence(
        plan_id="plan_bad",
        recovered=True,
        recovery_seconds=5.0,
        delivered=1000,
        dropped=200,  # drop ratio = 200/1200 = 0.166 > 0.05
    )
    score = score_candidate(evidence=ev, config=cfg)
    assert score.disqualified is True
    assert score.disqualification_reason == "evidence_incomplete"
    assert score.composite == 1.0
    # Raw components are preserved for auditability
    assert score.components["recovery"] == round(5.0 / 180.0, 4)


def test_score_candidates_batch() -> None:
    cfg = ScoringConfig()
    ev1 = _make_evidence(plan_id="p1", recovery_seconds=10.0)
    ev2 = _make_evidence(plan_id="p2", recovery_seconds=30.0)
    ev3 = _make_evidence(plan_id="p3", evidence_complete=False)

    scores = score_candidates(
        evidences=[ev1, ev2, ev3],
        config=cfg,
        twins_ready={"p1": True, "p2": True, "p3": True},
        blast_scores={"p1": 0.1, "p2": 0.2, "p3": 0.0},
    )

    assert len(scores) == 3
    assert scores[0].plan_id == "p1"
    assert scores[1].plan_id == "p2"
    assert scores[2].plan_id == "p3"
    assert scores[0].composite < scores[1].composite
    assert scores[2].disqualified is True
    assert scores[2].composite == 1.0


def test_deterministic_candidate_scorer_class_conformance() -> None:
    cfg = ScoringConfig()
    scorer = DeterministicCandidateScorer(config=cfg)
    assert isinstance(scorer, CandidateScorer)

    ev = _make_evidence(plan_id="plan_single", recovery_seconds=20.0)
    single_score = scorer.score_candidate(ev)
    assert single_score.plan_id == "plan_single"
    assert single_score.disqualified is False

    batch_scores = scorer.score([ev])
    assert len(batch_scores) == 1
    assert batch_scores[0] == single_score
