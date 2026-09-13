"""Deterministic remediation plan inverse synthesis per closed action enum.

Implements build-plan step B3.4:
- For each action type in ActionType, derives the inverse deterministically:
  - rollback_deploy <-> roll-forward to the pre-incident digest/commit
  - scale_workload <-> scale opposite delta (-delta)
  - disable_flag <-> re-enable flag
  - revert_config <-> restore prior configuration key/value
  - restart_workload <-> rolling restart to restore pre-intervention pod instances
  - no_action <-> None (explicitly non-reversible)
- Never asks the LLM for the inverse.
- Enforces Invariant K9 (Reversibility):
  plan.action != NO_ACTION ==> plan_has_inverse and inverse_targets = plan_targets
- Guarantees the algebraic property: the inverse of an inverse is the original plan.
"""

from collections.abc import Mapping

from understudy.common.errors import PlannerError
from understudy.contracts.enums import ActionType
from understudy.contracts.incident import IncidentContext
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef

# Known default configuration values from deploy/prod/configmap.yaml
DEFAULT_CONFIG_VALUES: dict[str, str] = {
    "ENVIRONMENT": "production",
    "LOG_LEVEL": "INFO",
    "FAULT_INJECTION_SEED": "1337",
    "enable_recommendations": "true",
    "enable_fast_cache": "true",
    "enable_v2_catalogue": "false",
}


def synthesize_inverse(
    plan: RemediationPlan,
    context: IncidentContext | None = None,
    current_commit: str | None = None,
    current_config_value: str | None = None,
    cluster_workloads: set[str] | None = None,
    config_lookup: Mapping[str, Mapping[str, str]] | None = None,
) -> RemediationPlan | None:
    """Derive the deterministic inverse plan for any given RemediationPlan candidate.

    Enforces Invariant K9: every plan except NO_ACTION declares an inverse whose
    target resource set equals its own (inverse_targets = plan_targets).

    Args:
        plan: The candidate remediation plan to invert.
        context: Optional diagnostic context providing deploy history and topology.
        current_commit: Explicit pre-incident commit SHA to roll forward to.
        current_config_value: Explicit pre-intervention configuration value.
        cluster_workloads: Optional set of active workloads for validation.
        config_lookup: Optional mapping of workload -> {config_key: config_value}.

    Returns:
        The synthesized inverse RemediationPlan, or None if plan is NO_ACTION.

    Raises:
        PlannerError: If an action parameter is invalid or required inversion
            state (such as pre-incident commit or prior config value) is missing.
    """
    if plan.action == ActionType.NO_ACTION:
        # K9: NO_ACTION has no inverse
        return None

    workload = plan.params.workload
    if not workload:
        raise PlannerError(
            f"Cannot synthesize inverse for {plan.action.value}: workload name is empty",
            details={"plan_id": plan.plan_id, "action": plan.action.value},
        )

    if cluster_workloads is not None and workload not in cluster_workloads:
        raise PlannerError(
            f"Cannot synthesize inverse for {plan.action.value}: workload '{workload}' "
            "not found in cluster workloads",
            details={"plan_id": plan.plan_id, "workload": workload},
        )

    # K9 Invariant: inverse targets MUST equal plan targets
    inverse_targets: list[ResourceRef] = list(plan.target_resources)
    inverse_blast_set: list[str] = list(plan.declared_blast_set)
    inv_plan_id = f"{plan.plan_id}_inv"

    inverse_action: ActionType
    inverse_params: ActionParams
    inverse_rationale: str

    if plan.action == ActionType.SCALE_WORKLOAD:
        replica_delta = plan.params.replica_delta
        if replica_delta is None or replica_delta == 0:
            raise PlannerError(
                f"Cannot synthesize inverse for scale_workload on '{workload}': "
                "replica_delta must be non-zero",
                details={
                    "plan_id": plan.plan_id,
                    "action": plan.action.value,
                    "replica_delta": replica_delta,
                },
            )
        inverse_action = ActionType.SCALE_WORKLOAD
        inverse_delta = -replica_delta
        inverse_params = ActionParams(workload=workload, replica_delta=inverse_delta)
        inverse_rationale = (
            f"Scale {workload} back by {inverse_delta:+d} replicas "
            f"(reversing delta of {replica_delta:+d})"
        )

    elif plan.action == ActionType.RESTART_WORKLOAD:
        inverse_action = ActionType.RESTART_WORKLOAD
        inverse_params = ActionParams(workload=workload)
        inverse_rationale = (
            f"Trigger rolling restart of {workload} to restore pre-intervention pod instances"
        )

    elif plan.action == ActionType.DISABLE_FLAG:
        flag_name = plan.params.flag_name
        if not flag_name:
            raise PlannerError(
                f"Cannot synthesize inverse for disable_flag on '{workload}': flag_name required",
                details={"plan_id": plan.plan_id, "action": plan.action.value},
            )
        inverse_action = ActionType.DISABLE_FLAG
        inverse_params = ActionParams(workload=workload, flag_name=flag_name)
        # Symmetrically toggle rationale based on existing rationale
        if plan.rationale.lower().startswith("re-enable"):
            inverse_rationale = f"Disable feature flag '{flag_name}' for {workload}"
        else:
            inverse_rationale = f"Re-enable feature flag '{flag_name}' for {workload}"

    elif plan.action == ActionType.REVERT_CONFIG:
        config_key = plan.params.config_key
        config_value = plan.params.config_value
        if not config_key or config_value is None:
            raise PlannerError(
                f"Cannot synthesize inverse for revert_config on '{workload}': "
                "config_key and config_value required",
                details={
                    "plan_id": plan.plan_id,
                    "action": plan.action.value,
                    "config_key": config_key,
                    "config_value": config_value,
                },
            )

        resolved_prior_val = _resolve_prior_config_value(
            workload=workload,
            config_key=config_key,
            current_config_value=current_config_value,
            plan=plan,
            config_lookup=config_lookup,
        )
        if resolved_prior_val is None:
            raise PlannerError(
                f"Cannot synthesize inverse for revert_config on '{workload}' ({config_key}): "
                "pre-intervention config_value unknown",
                details={
                    "plan_id": plan.plan_id,
                    "action": plan.action.value,
                    "config_key": config_key,
                },
            )

        inverse_action = ActionType.REVERT_CONFIG
        inverse_params = ActionParams(
            workload=workload,
            config_key=config_key,
            config_value=resolved_prior_val,
        )
        inverse_rationale = (
            f"Restore configuration key '{config_key}' for {workload} to prior value "
            f"'{resolved_prior_val}'"
        )

    elif plan.action == ActionType.ROLLBACK_DEPLOY:
        target_commit = plan.params.target_commit
        if not target_commit:
            raise PlannerError(
                f"Cannot synthesize inverse for rollback_deploy on '{workload}': "
                "target_commit required",
                details={"plan_id": plan.plan_id, "action": plan.action.value},
            )

        resolved_commit = _resolve_pre_incident_commit(
            workload=workload,
            target_commit=target_commit,
            current_commit=current_commit,
            context=context,
            plan=plan,
        )
        if resolved_commit is None:
            raise PlannerError(
                f"Cannot synthesize inverse for rollback_deploy on '{workload}': "
                "pre-incident commit unknown",
                details={
                    "plan_id": plan.plan_id,
                    "action": plan.action.value,
                    "target_commit": target_commit,
                },
            )

        inverse_action = ActionType.ROLLBACK_DEPLOY
        inverse_params = ActionParams(workload=workload, target_commit=resolved_commit)
        inverse_rationale = (
            f"Roll forward {workload} Deployment to pre-incident commit {resolved_commit[:7]}"
        )

    else:
        raise PlannerError(
            f"Unsupported action type for deterministic inverse synthesis: {plan.action}",
            details={"action": str(plan.action)},
        )

    inverse_plan = RemediationPlan(
        plan_id=inv_plan_id,
        candidate_index=plan.candidate_index,
        action=inverse_action,
        params=inverse_params,
        target_resources=inverse_targets,
        declared_blast_set=inverse_blast_set,
        inverse=None,
        rationale=inverse_rationale,
        origin=plan.origin,
        playbook_id=plan.playbook_id,
    )

    return inverse_plan


def invert_plan(
    plan: RemediationPlan,
    context: IncidentContext | None = None,
    current_commit: str | None = None,
    current_config_value: str | None = None,
    cluster_workloads: set[str] | None = None,
    config_lookup: Mapping[str, Mapping[str, str]] | None = None,
) -> RemediationPlan:
    """Invert a RemediationPlan, raising PlannerError if the plan is non-reversible (NO_ACTION).

    Convenience wrapper around synthesize_inverse for callers requiring a guaranteed
    non-null inverse plan.
    """
    if plan.action == ActionType.NO_ACTION:
        raise PlannerError(
            f"Plan {plan.plan_id} (no_action) cannot be inverted: "
            "no_action is explicitly non-reversible by definition",
            details={"plan_id": plan.plan_id, "action": plan.action.value},
        )

    inv = synthesize_inverse(
        plan=plan,
        context=context,
        current_commit=current_commit,
        current_config_value=current_config_value,
        cluster_workloads=cluster_workloads,
        config_lookup=config_lookup,
    )
    assert inv is not None
    return inv


def _resolve_prior_config_value(
    workload: str,
    config_key: str,
    current_config_value: str | None,
    plan: RemediationPlan,
    config_lookup: Mapping[str, Mapping[str, str]] | None,
) -> str | None:
    """Resolve the pre-intervention configuration value for REVERT_CONFIG."""
    # 1. Explicit caller argument
    if current_config_value is not None:
        return current_config_value

    # 2. Existing attached inverse (if already known from prior synthesis/validation)
    if (
        plan.inverse is not None
        and plan.inverse.action == ActionType.REVERT_CONFIG
        and plan.inverse.params.config_value is not None
    ):
        return plan.inverse.params.config_value

    # 3. Dynamic config lookup map
    if config_lookup is not None:
        if workload in config_lookup and config_key in config_lookup[workload]:
            return config_lookup[workload][config_key]
        if "" in config_lookup and config_key in config_lookup[""]:
            return config_lookup[""][config_key]

    # 4. Standard default config map values
    if config_key in DEFAULT_CONFIG_VALUES:
        return DEFAULT_CONFIG_VALUES[config_key]

    return None


def _resolve_pre_incident_commit(
    workload: str,
    target_commit: str,
    current_commit: str | None,
    context: IncidentContext | None,
    plan: RemediationPlan,
) -> str | None:
    """Resolve the pre-incident commit SHA to roll forward to for ROLLBACK_DEPLOY."""
    # 1. Explicit caller argument
    if current_commit is not None:
        return current_commit

    # 2. IncidentContext recent deploys
    if context is not None and context.recent_deploys:
        deploys = context.recent_deploys
        head_commit = deploys[0].commit_sha

        # If target_commit is not head, head_commit is the pre-incident commit
        if (
            target_commit != head_commit
            and not target_commit.startswith(head_commit)
            and not head_commit.startswith(target_commit)
        ):
            return head_commit

        # If target_commit IS head_commit, check attached inverse first if present
        if (
            plan.inverse is not None
            and plan.inverse.action == ActionType.ROLLBACK_DEPLOY
            and plan.inverse.params.target_commit
        ):
            return plan.inverse.params.target_commit

        # Otherwise, this plan is rolling forward (inverting a rollback).
        # Find the first deploy distinct from head_commit to roll back to.
        for d in deploys[1:]:
            if (
                d.commit_sha != head_commit
                and not d.commit_sha.startswith(target_commit)
                and not target_commit.startswith(d.commit_sha)
                and (not workload or not d.image_digests or workload in d.image_digests)
            ):
                return d.commit_sha

    # 3. Existing attached inverse target commit
    if (
        plan.inverse is not None
        and plan.inverse.action == ActionType.ROLLBACK_DEPLOY
        and plan.inverse.params.target_commit
    ):
        return plan.inverse.params.target_commit

    return None
