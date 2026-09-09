"""Contract enums for Understudy."""

from enum import StrEnum


class ActionType(StrEnum):
    """Enumeration of permitted remediation actions."""

    ROLLBACK_DEPLOY = "rollback_deploy"  # to a specific commit SHA / image digest
    RESTART_WORKLOAD = "restart_workload"  # rolling restart
    SCALE_WORKLOAD = "scale_workload"  # replicas delta
    DISABLE_FLAG = "disable_flag"  # feature flag off
    REVERT_CONFIG = "revert_config"  # ConfigMap key to prior value
    NO_ACTION = "no_action"  # explicit "do nothing" candidate


class FailureClass(StrEnum):
    """Classification of root-cause failures."""

    BAD_DEPLOY = "bad_deploy"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    DEPENDENCY_DEGRADATION = "dependency_degradation"
    CONFIG_DRIFT = "config_drift"


class KernelVerdictType(StrEnum):
    """Tri-state verdict returned by the safety kernel."""

    PASS = "pass"
    VETO = "veto"
    UNCERTAIN = "uncertain"


class TournamentOutcome(StrEnum):
    """Outcome of the candidate tournament."""

    DECIDED = "decided"
    AMBIGUOUS = "ambiguous"
    NO_VIABLE_CANDIDATE = "no_viable_candidate"


class RunOutcome(StrEnum):
    """Overall terminal outcome of an incident response run."""

    EXECUTED = "executed"
    ESCALATED = "escalated"
    FAILED = "failed"


class InvariantTier(StrEnum):
    """Tier of invariant checking."""

    PROOF = "proof"
    RUNTIME = "runtime"
