"""Tournament package: candidate observation, scoring, and arbitration."""

from understudy.tournament.api import (
    BlastTracker,
    CandidateScorer,
    EnvironmentProbe,
    Tournament,
)
from understudy.tournament.blast import (
    BlastConfig,
    BlastCoordinator,
    BlastEvaluation,
    EnvironmentBaseline,
    compute_affected_services,
    compute_blast_radius,
    compute_downstream_error_delta,
    evaluate_blast,
)
from understudy.tournament.fakes import (
    FakeBlastTracker,
    FakeEnvironmentProbe,
    FakeTournament,
)
from understudy.tournament.probe import (
    ProbeConfig,
    ProbeResult,
    ProbeSampler,
    detect_recovery,
)
from understudy.tournament.scorer import (
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

__all__ = [
    "BlastConfig",
    "BlastCoordinator",
    "BlastEvaluation",
    "BlastTracker",
    "CandidateScorer",
    "DeterministicCandidateScorer",
    "EnvironmentBaseline",
    "EnvironmentProbe",
    "FakeBlastTracker",
    "FakeEnvironmentProbe",
    "FakeTournament",
    "ProbeConfig",
    "ProbeResult",
    "ProbeSampler",
    "ScoringConfig",
    "Tournament",
    "check_disqualification",
    "compute_affected_services",
    "compute_blast_radius",
    "compute_blast_subscore",
    "compute_downstream_error_delta",
    "compute_downstream_subscore",
    "compute_recovery_subscore",
    "compute_violations_subscore",
    "detect_recovery",
    "evaluate_blast",
    "load_scoring_config",
    "score_candidate",
    "score_candidates",
]
