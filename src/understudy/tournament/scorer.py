"""Deterministic candidate scorer and disqualification rules for rehearsal tournaments.

Implements build-plan step A4.3 and architecture §2.7:
components (each normalised to [0,1], lower is better):
  recovery   = 1.0 if not recovered else recovery_seconds / RECOVERY_TIMEOUT_SECONDS
  blast      = blast radius as computed above (Σ request_share(s) for s in observed_blast_set)
  downstream = clamp(downstream_error_delta / DOWNSTREAM_ERROR_CEILING, 0, 1)
  violations = min(runtime_violation_count / VIOLATION_CEILING, 1.0)

composite = 0.40*recovery + 0.25*blast + 0.20*downstream + 0.15*violations

disqualification (composite := 1.0, disqualified := true) if ANY of:
  - evidence_complete == false (mirror drop ratio > 0.05 or probe samples < 60)
  - twin failed to reach ready state
  - candidate caused a hard invariant violation in the twin
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from understudy.common.config import get_settings
from understudy.common.errors import ConfigError
from understudy.contracts.evidence import CandidateEvidence, CandidateScore
from understudy.graph.api import DependencyGraph
from understudy.tournament.api import CandidateScorer

# Default canonical request shares for demo services per architecture §2.6
DEFAULT_SERVICE_REQUEST_SHARES: dict[str, float] = {
    "edge-gateway": 0.40,
    "auth-service": 0.30,
    "data-service": 0.20,
    "worker": 0.10,
}


class ScoringConfig(BaseModel):
    """Configuration weights, ceilings, and thresholds for deterministic scoring."""

    model_config = ConfigDict(frozen=True)

    recovery_weight: float = 0.40
    blast_weight: float = 0.25
    downstream_weight: float = 0.20
    violations_weight: float = 0.15

    recovery_timeout_seconds: float = 180.0
    downstream_error_ceiling: float = 0.10
    violation_ceiling: int = 5
    mirror_drop_ceiling: float = 0.05
    min_probe_samples: int = 60
    hard_invariants: tuple[str, ...] = Field(default=("K6", "K10"))

    @model_validator(mode="after")
    def _validate_weights(self) -> "ScoringConfig":
        """Verify that scoring component weights sum to 1.0."""
        total = (
            self.recovery_weight
            + self.blast_weight
            + self.downstream_weight
            + self.violations_weight
        )
        if abs(total - 1.0) > 1e-4:
            raise ConfigError(
                f"Scoring weights must sum to 1.0; got {total:.4f} "
                f"(recovery={self.recovery_weight}, blast={self.blast_weight}, "
                f"downstream={self.downstream_weight}, violations={self.violations_weight})"
            )
        return self

    @classmethod
    def from_settings(cls) -> "ScoringConfig":
        """Construct ScoringConfig loaded from global system settings."""
        settings = get_settings()
        s = settings.scoring
        return cls(
            recovery_weight=s.recovery_weight,
            blast_weight=s.blast_weight,
            downstream_weight=s.downstream_weight,
            violations_weight=s.violations_weight,
            recovery_timeout_seconds=float(s.recovery_timeout_seconds),
            downstream_error_ceiling=s.downstream_error_ceiling,
            violation_ceiling=s.violation_ceiling,
            mirror_drop_ceiling=s.mirror_drop_ceiling,
            min_probe_samples=s.min_probe_samples,
        )

    @classmethod
    def from_yaml(cls, path: Path | str) -> "ScoringConfig":
        """Load and parse scoring configuration from a YAML file."""
        file_path = Path(path)
        if not file_path.is_file():
            raise ConfigError(f"Scoring config YAML not found at: {file_path}")
        try:
            with file_path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as exc:
            raise ConfigError(f"Failed to parse scoring config YAML at {file_path}: {exc}") from exc

        if not isinstance(data, dict):
            raise ConfigError(f"Invalid YAML structure in {file_path}: expected dictionary")

        flattened: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    if k == "weights" and not sub_k.endswith("_weight"):
                        flattened[f"{sub_k}_weight"] = sub_v
                    else:
                        flattened[sub_k] = sub_v
            else:
                flattened[k] = v

        try:
            return cls.model_validate(flattened)
        except Exception as exc:
            raise ConfigError(f"Failed to validate scoring configuration: {exc}") from exc


def load_scoring_config(path: Path | str = "config/scoring.yaml") -> ScoringConfig:
    """Load scoring configuration from YAML file with fallback to global system settings."""
    p = Path(path)
    if p.is_file():
        return ScoringConfig.from_yaml(p)
    return ScoringConfig.from_settings()


def compute_recovery_subscore(
    recovered: bool,
    recovery_seconds: float | None,
    timeout_seconds: float = 180.0,
) -> float:
    """Calculate normalized recovery subscore in [0.0, 1.0] (lower is better).

    Per architecture §2.7:
    recovery = 1.0 if not recovered else recovery_seconds / RECOVERY_TIMEOUT_SECONDS
    """
    if not recovered or recovery_seconds is None:
        return 1.0
    if timeout_seconds <= 0:
        return 1.0
    return min(max(float(recovery_seconds) / timeout_seconds, 0.0), 1.0)


def compute_blast_subscore(
    observed_blast_set: Sequence[str],
    request_shares: dict[str, float] | None = None,
    graph: DependencyGraph | None = None,
    blast_radius: float | None = None,
) -> float:
    """Calculate normalized blast radius subscore in [0.0, 1.0] (lower is better).

    Per architecture §2.6 and §2.7:
    blast = Σ over services s in reachable_set(target, dep_graph) request_share(s) * affected(s)
    where observed_blast_set is {s : affected(s) = 1}.
    """
    if blast_radius is not None:
        return min(max(float(blast_radius), 0.0), 1.0)
    if not observed_blast_set:
        return 0.0

    if graph is not None:
        val = sum(graph.request_share(s) for s in observed_blast_set)
    elif request_shares is not None:
        val = sum(request_shares.get(s, 0.0) for s in observed_blast_set)
    else:
        val = sum(DEFAULT_SERVICE_REQUEST_SHARES.get(s, 0.25) for s in observed_blast_set)

    return min(max(float(val), 0.0), 1.0)


def compute_downstream_subscore(
    downstream_error_delta: float,
    ceiling: float = 0.10,
) -> float:
    """Calculate normalized downstream error delta subscore in [0.0, 1.0] (lower is better).

    Per architecture §2.7:
    downstream = clamp(downstream_error_delta / DOWNSTREAM_ERROR_CEILING, 0, 1)
    """
    if ceiling <= 0:
        return 1.0 if downstream_error_delta > 0 else 0.0
    return min(max(float(downstream_error_delta) / ceiling, 0.0), 1.0)


def compute_violations_subscore(
    runtime_violation_count: int,
    ceiling: int = 5,
) -> float:
    """Calculate normalized runtime invariant violations subscore in [0.0, 1.0] (lower is better).

    Per architecture §2.7:
    violations = min(runtime_violation_count / VIOLATION_CEILING, 1.0)
    """
    if ceiling <= 0:
        return 1.0 if runtime_violation_count > 0 else 0.0
    return min(max(float(runtime_violation_count) / ceiling, 0.0), 1.0)


def check_disqualification(
    evidence: CandidateEvidence,
    config: ScoringConfig,
    twin_ready: bool = True,
) -> tuple[bool, str | None]:
    """Evaluate whether a candidate must be disqualified per architecture §2.7.

    Disqualification triggers:
    1. twin failed to reach ready state
    2. candidate caused a hard invariant violation in the twin
    3. evidence_complete == false (mirror drop ratio > ceiling or probe samples < min)

    Returns:
        tuple[bool, str | None]: (disqualified, disqualification_reason)
    """
    if not twin_ready or "twin_not_ready" in evidence.invariant_violations:
        return True, "twin_not_ready"

    for violation in evidence.invariant_violations:
        if violation in config.hard_invariants or violation.startswith("hard:"):
            return True, f"hard_invariant_violation: {violation}"

    if not evidence.evidence_complete:
        return True, "evidence_incomplete"

    if evidence.mirror_stats.drop_ratio > config.mirror_drop_ceiling:
        return True, "evidence_incomplete"

    if len(evidence.probes) < config.min_probe_samples:
        return True, "evidence_incomplete"

    return False, None


def score_candidate(
    evidence: CandidateEvidence,
    config: ScoringConfig | None = None,
    request_shares: dict[str, float] | None = None,
    graph: DependencyGraph | None = None,
    twin_ready: bool = True,
    blast_radius: float | None = None,
) -> CandidateScore:
    """Score a single candidate's rehearsal evidence deterministically.

    Computes normalized subscores, checks disqualification rules, and returns CandidateScore.
    """
    cfg = config or load_scoring_config()

    rec_score = compute_recovery_subscore(
        recovered=evidence.recovered,
        recovery_seconds=evidence.recovery_seconds,
        timeout_seconds=cfg.recovery_timeout_seconds,
    )
    blast_score = compute_blast_subscore(
        observed_blast_set=evidence.observed_blast_set,
        request_shares=request_shares,
        graph=graph,
        blast_radius=blast_radius,
    )
    downstream_score = compute_downstream_subscore(
        downstream_error_delta=evidence.downstream_error_delta,
        ceiling=cfg.downstream_error_ceiling,
    )
    violations_score = compute_violations_subscore(
        runtime_violation_count=len(evidence.invariant_violations),
        ceiling=cfg.violation_ceiling,
    )

    components = {
        "recovery": round(rec_score, 4),
        "blast": round(blast_score, 4),
        "downstream": round(downstream_score, 4),
        "violations": round(violations_score, 4),
    }

    disqualified, reason = check_disqualification(
        evidence=evidence,
        config=cfg,
        twin_ready=twin_ready,
    )

    if disqualified:
        composite = 1.0
    else:
        raw_composite = (
            (cfg.recovery_weight * rec_score)
            + (cfg.blast_weight * blast_score)
            + (cfg.downstream_weight * downstream_score)
            + (cfg.violations_weight * violations_score)
        )
        composite = min(max(raw_composite, 0.0), 1.0)

    return CandidateScore(
        plan_id=evidence.plan_id,
        composite=round(composite, 4),
        components=components,
        disqualified=disqualified,
        disqualification_reason=reason,
    )


def score_candidates(
    evidences: Sequence[CandidateEvidence],
    config: ScoringConfig | None = None,
    request_shares: dict[str, float] | None = None,
    graph: DependencyGraph | None = None,
    twins_ready: dict[str, bool] | None = None,
    blast_scores: dict[str, float] | None = None,
) -> list[CandidateScore]:
    """Score multiple candidate rehearsal evidences deterministically."""
    cfg = config or load_scoring_config()
    scores: list[CandidateScore] = []
    for ev in evidences:
        twin_ready = twins_ready.get(ev.plan_id, True) if twins_ready else True
        blast_val = blast_scores.get(ev.plan_id) if blast_scores else None
        scores.append(
            score_candidate(
                evidence=ev,
                config=cfg,
                request_shares=request_shares,
                graph=graph,
                twin_ready=twin_ready,
                blast_radius=blast_val,
            )
        )
    return scores


class DeterministicCandidateScorer(CandidateScorer):
    """Authoritative deterministic scorer evaluating candidate rehearsals."""

    def __init__(
        self,
        config: ScoringConfig | None = None,
        graph: DependencyGraph | None = None,
        request_shares: dict[str, float] | None = None,
    ) -> None:
        self.config = config or load_scoring_config()
        self.graph = graph
        self.request_shares = request_shares

    def score_candidate(
        self,
        evidence: CandidateEvidence,
        twin_ready: bool = True,
        blast_radius: float | None = None,
    ) -> CandidateScore:
        """Score a single candidate's rehearsal evidence deterministically."""
        return score_candidate(
            evidence=evidence,
            config=self.config,
            request_shares=self.request_shares,
            graph=self.graph,
            twin_ready=twin_ready,
            blast_radius=blast_radius,
        )

    def score(
        self,
        evidences: Sequence[CandidateEvidence],
        twins_ready: dict[str, bool] | None = None,
        blast_scores: dict[str, float] | None = None,
    ) -> list[CandidateScore]:
        """Score multiple candidate rehearsal evidences deterministically."""
        return score_candidates(
            evidences=evidences,
            config=self.config,
            request_shares=self.request_shares,
            graph=self.graph,
            twins_ready=twins_ready,
            blast_scores=blast_scores,
        )
