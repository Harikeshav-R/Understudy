"""Unit and property tests for deterministic inverse plan synthesis (B3.4).

Verifies:
- Deterministic inverse synthesis for all 6 action types in ActionType.
- Formal Invariant K9:
  plan.action != NO_ACTION ==> plan_has_inverse and inverse_targets = plan_targets.
- AGENTS.md §6.6 property: The inverse of an inverse is the original plan.
- Strict error handling on missing facts / parameters.
"""

import random
from datetime import UTC, datetime

import pytest

from understudy.common.errors import PlannerError
from understudy.contracts.enums import ActionType
from understudy.contracts.incident import (
    Alert,
    DependencyEdge,
    DependencyGraphSnapshot,
    DeployRef,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.planner.inverse import (
    invert_plan,
    synthesize_inverse,
)

FIXED_TIME = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def _sample_context() -> IncidentContext:
    """Build a deterministic fixture IncidentContext."""
    return IncidentContext(
        incident_id="inc_test_inv_001",
        alert=Alert(
            alert_id="alt_001",
            source="synthetic",
            title="Service latency breach",
            service="data-service",
            severity="critical",
            fired_at=FIXED_TIME,
        ),
        signatures=[],
        metrics_window=MetricWindow(
            service="data-service",
            start_time=FIXED_TIME,
            end_time=FIXED_TIME,
        ),
        recent_deploys=[
            DeployRef(
                commit_sha="c0ffee0000000000000000000000000000000000",
                image_digests={
                    "data-service": "sha256:data000",
                    "auth-service": "sha256:auth000",
                    "edge-gateway": "sha256:edge000",
                },
                deployed_at=FIXED_TIME,
            ),
            DeployRef(
                commit_sha="deadbeef00000000000000000000000000000000",
                image_digests={
                    "data-service": "sha256:data111",
                    "auth-service": "sha256:auth111",
                    "edge-gateway": "sha256:edge111",
                },
                deployed_at=FIXED_TIME,
            ),
        ],
        dependency_graph=DependencyGraphSnapshot(
            nodes=["edge-gateway", "data-service", "auth-service"],
            edges=[DependencyEdge(source="edge-gateway", target="data-service")],
            observed_at=FIXED_TIME,
        ),
        gathered_at=FIXED_TIME,
    )


# ---------------------------------------------------------------------------
# 1. Action Type Unit Tests
# ---------------------------------------------------------------------------


def test_synthesize_inverse_no_action() -> None:
    """NO_ACTION must return None per Invariant K9."""
    plan = RemediationPlan(
        plan_id="plan_no_action",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="data-service"),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="Do nothing",
        origin="planner",
    )
    assert synthesize_inverse(plan) is None


def test_invert_plan_no_action_raises() -> None:
    """invert_plan helper must raise PlannerError on NO_ACTION."""
    plan = RemediationPlan(
        plan_id="plan_no_action",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="data-service"),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="Do nothing",
        origin="planner",
    )
    with pytest.raises(PlannerError) as exc_info:
        invert_plan(plan)
    assert "no_action is explicitly non-reversible" in str(exc_info.value)


def test_synthesize_inverse_scale_workload() -> None:
    """SCALE_WORKLOAD inverts replica_delta symmetrically."""
    target_res = [ResourceRef(namespace="ust-twin", kind="Deployment", name="data-service")]
    plan = RemediationPlan(
        plan_id="plan_scale",
        candidate_index=1,
        action=ActionType.SCALE_WORKLOAD,
        params=ActionParams(workload="data-service", replica_delta=3),
        target_resources=target_res,
        declared_blast_set=["data-service"],
        inverse=None,
        rationale="Scale up",
        origin="planner",
    )
    inv = synthesize_inverse(plan)
    assert inv is not None
    assert inv.action == ActionType.SCALE_WORKLOAD
    assert inv.params.workload == "data-service"
    assert inv.params.replica_delta == -3
    assert inv.target_resources == target_res  # K9: inverse_targets = plan_targets
    assert inv.declared_blast_set == ["data-service"]
    assert "Scale data-service back by -3 replicas" in inv.rationale

    # Test invert_plan convenience function
    inv_direct = invert_plan(plan)
    assert inv_direct == inv


def test_synthesize_inverse_restart_workload() -> None:
    """RESTART_WORKLOAD triggers rolling restart of same workload."""
    target_res = [ResourceRef(namespace="ust-twin", kind="Deployment", name="auth-service")]
    plan = RemediationPlan(
        plan_id="plan_restart",
        candidate_index=2,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload="auth-service"),
        target_resources=target_res,
        declared_blast_set=["auth-service"],
        inverse=None,
        rationale="Restart workload",
        origin="planner",
    )
    inv = synthesize_inverse(plan)
    assert inv is not None
    assert inv.action == ActionType.RESTART_WORKLOAD
    assert inv.params.workload == "auth-service"
    assert inv.target_resources == target_res
    assert inv.declared_blast_set == ["auth-service"]
    assert "Trigger rolling restart of auth-service" in inv.rationale


def test_synthesize_inverse_disable_flag() -> None:
    """DISABLE_FLAG inverts by re-enabling flag symmetrically."""
    target_res = [ResourceRef(namespace="ust-twin", kind="Deployment", name="data-service")]
    plan = RemediationPlan(
        plan_id="plan_flag",
        candidate_index=0,
        action=ActionType.DISABLE_FLAG,
        params=ActionParams(workload="data-service", flag_name="FEATURE_FAST_PATH"),
        target_resources=target_res,
        declared_blast_set=["data-service"],
        inverse=None,
        rationale="Disable fast path flag",
        origin="planner",
    )
    inv = synthesize_inverse(plan)
    assert inv is not None
    assert inv.action == ActionType.DISABLE_FLAG
    assert inv.params.flag_name == "FEATURE_FAST_PATH"
    assert inv.target_resources == target_res
    assert "Re-enable feature flag 'FEATURE_FAST_PATH'" in inv.rationale

    # Inverting an inverse (which has re-enable rationale) toggles to disable
    inv_inv = synthesize_inverse(inv)
    assert inv_inv is not None
    assert "Disable feature flag 'FEATURE_FAST_PATH'" in inv_inv.rationale


def test_synthesize_inverse_revert_config_resolution_sources() -> None:
    """REVERT_CONFIG resolves prior config value through all cascade tiers."""
    target_res = [ResourceRef(namespace="ust-twin", kind="ConfigMap", name="app-config")]

    # 1. Explicit current_config_value argument
    plan1 = RemediationPlan(
        plan_id="plan_cfg1",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="app-config", config_key="CUSTOM_KEY", config_value="new_val"),
        target_resources=target_res,
        declared_blast_set=["data-service"],
        inverse=None,
        rationale="Revert custom key",
        origin="planner",
    )
    inv1 = synthesize_inverse(plan1, current_config_value="prior_val")
    assert inv1 is not None
    assert inv1.params.config_value == "prior_val"

    # 2. Attached inverse is NOT trusted (deterministic safety)
    plan2 = plan1.model_copy(
        update={
            "inverse": plan1.model_copy(
                update={
                    "params": ActionParams(
                        workload="app-config",
                        config_key="CUSTOM_KEY",
                        config_value="attached_prior",
                    )
                }
            )
        }
    )
    with pytest.raises(PlannerError, match="pre-intervention config_value unknown"):
        synthesize_inverse(plan2)

    # 3. Config lookup map
    lookup = {"app-config": {"CUSTOM_KEY": "lookup_val"}}
    inv3 = synthesize_inverse(plan1, config_lookup=lookup)
    assert inv3 is not None
    assert inv3.params.config_value == "lookup_val"

    # 3b. Wildcard config lookup map
    lookup_wildcard = {"": {"CUSTOM_KEY": "wildcard_val"}}
    inv3b = synthesize_inverse(plan1, config_lookup=lookup_wildcard)
    assert inv3b is not None
    assert inv3b.params.config_value == "wildcard_val"

    # 4. Unknown config key without lookup raises PlannerError
    plan4 = RemediationPlan(
        plan_id="plan_cfg4",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="app-config", config_key="LOG_LEVEL", config_value="DEBUG"),
        target_resources=target_res,
        declared_blast_set=[],
        inverse=None,
        rationale="Set debug log level",
        origin="planner",
    )
    with pytest.raises(PlannerError, match="pre-intervention config_value unknown"):
        synthesize_inverse(plan4)

    # Resolving with explicit config_lookup succeeds deterministically
    inv4 = synthesize_inverse(plan4, config_lookup={"app-config": {"LOG_LEVEL": "INFO"}})
    assert inv4 is not None
    assert inv4.params.config_value == "INFO"
    assert "Restore configuration key 'LOG_LEVEL'" in inv4.rationale


def test_synthesize_inverse_rollback_deploy() -> None:
    """ROLLBACK_DEPLOY resolves pre-incident commit from context recent deploys."""
    ctx = _sample_context()
    head_sha = ctx.recent_deploys[0].commit_sha
    prior_sha = ctx.recent_deploys[1].commit_sha

    target_res = [ResourceRef(namespace="ust-twin", kind="Deployment", name="data-service")]
    plan = RemediationPlan(
        plan_id="plan_rb",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit=prior_sha),
        target_resources=target_res,
        declared_blast_set=["data-service"],
        inverse=None,
        rationale="Rollback to prior commit",
        origin="planner",
    )

    # Inverting a rollback plan rolls forward to head_sha
    inv = synthesize_inverse(plan, context=ctx)
    assert inv is not None
    assert inv.action == ActionType.ROLLBACK_DEPLOY
    assert inv.params.workload == "data-service"
    assert inv.params.target_commit == head_sha
    assert inv.target_resources == target_res
    assert (
        f"Roll forward data-service Deployment to pre-incident commit {head_sha[:7]}"
        in inv.rationale
    )

    # Explicit current_commit overrides
    inv_override = synthesize_inverse(plan, context=ctx, current_commit="custom_head_sha")
    assert inv_override is not None
    assert inv_override.params.target_commit == "custom_head_sha"

    # Attached inverse is NOT trusted (deterministic safety)
    plan_with_inv = plan.model_copy(
        update={
            "inverse": plan.model_copy(
                update={
                    "params": ActionParams(
                        workload="data-service", target_commit="existing_inv_sha"
                    )
                }
            )
        }
    )
    with pytest.raises(PlannerError, match="pre-incident commit unknown"):
        synthesize_inverse(plan_with_inv, context=None)

    inv_from_explicit = synthesize_inverse(
        plan_with_inv, context=None, current_commit="existing_inv_sha"
    )
    assert inv_from_explicit is not None
    assert inv_from_explicit.params.target_commit == "existing_inv_sha"


# ---------------------------------------------------------------------------
# 2. Error and Boundary Cases
# ---------------------------------------------------------------------------


def test_synthesize_inverse_missing_workload_raises() -> None:
    plan = RemediationPlan(
        plan_id="p1",
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload=""),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="r",
        origin="planner",
    )
    with pytest.raises(PlannerError) as exc:
        synthesize_inverse(plan)
    assert "workload name is empty" in str(exc.value)


def test_synthesize_inverse_workload_not_in_cluster_raises() -> None:
    plan = RemediationPlan(
        plan_id="p1",
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload="foreign-service"),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="r",
        origin="planner",
    )
    with pytest.raises(PlannerError) as exc:
        synthesize_inverse(plan, cluster_workloads={"data-service", "auth-service"})
    assert "not found in cluster workloads" in str(exc.value)


def test_synthesize_inverse_scale_invalid_delta_raises() -> None:
    # 0 delta
    plan_zero = RemediationPlan(
        plan_id="p1",
        candidate_index=0,
        action=ActionType.SCALE_WORKLOAD,
        params=ActionParams(workload="data-service", replica_delta=0),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="r",
        origin="planner",
    )
    with pytest.raises(PlannerError) as exc1:
        synthesize_inverse(plan_zero)
    assert "replica_delta must be non-zero" in str(exc1.value)

    # None delta
    plan_none = RemediationPlan(
        plan_id="p2",
        candidate_index=0,
        action=ActionType.SCALE_WORKLOAD,
        params=ActionParams(workload="data-service", replica_delta=None),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="r",
        origin="planner",
    )
    with pytest.raises(PlannerError) as exc2:
        synthesize_inverse(plan_none)
    assert "replica_delta must be non-zero" in str(exc2.value)


def test_synthesize_inverse_disable_flag_missing_flag_raises() -> None:
    plan = RemediationPlan(
        plan_id="p1",
        candidate_index=0,
        action=ActionType.DISABLE_FLAG,
        params=ActionParams(workload="data-service", flag_name=None),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="r",
        origin="planner",
    )
    with pytest.raises(PlannerError) as exc:
        synthesize_inverse(plan)
    assert "flag_name required" in str(exc.value)


def test_synthesize_inverse_revert_config_missing_params_raises() -> None:
    # Missing config_key
    plan_no_key = RemediationPlan(
        plan_id="p1",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="app-config", config_key=None, config_value="v"),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="r",
        origin="planner",
    )
    with pytest.raises(PlannerError) as exc1:
        synthesize_inverse(plan_no_key)
    assert "config_key and config_value required" in str(exc1.value)

    # Missing config_value
    plan_no_val = RemediationPlan(
        plan_id="p2",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="app-config", config_key="k", config_value=None),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="r",
        origin="planner",
    )
    with pytest.raises(PlannerError) as exc2:
        synthesize_inverse(plan_no_val)
    assert "config_key and config_value required" in str(exc2.value)

    # Unknown pre-intervention value
    plan_unresolvable = RemediationPlan(
        plan_id="p3",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="app-config", config_key="UNKNOWN_SECRET", config_value="1"),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="r",
        origin="planner",
    )
    with pytest.raises(PlannerError) as exc3:
        synthesize_inverse(plan_unresolvable)
    assert "pre-intervention config_value unknown" in str(exc3.value)


def test_synthesize_inverse_rollback_deploy_missing_params_raises() -> None:
    # Missing target_commit
    plan_no_commit = RemediationPlan(
        plan_id="p1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit=None),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="r",
        origin="planner",
    )
    with pytest.raises(PlannerError) as exc1:
        synthesize_inverse(plan_no_commit)
    assert "target_commit required" in str(exc1.value)

    # Unresolvable commit (no context, no current_commit, no attached inverse)
    plan_unresolvable = RemediationPlan(
        plan_id="p2",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit="deadbeef"),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="r",
        origin="planner",
    )
    with pytest.raises(PlannerError) as exc2:
        synthesize_inverse(plan_unresolvable)
    assert "pre-incident commit unknown" in str(exc2.value)


# ---------------------------------------------------------------------------
# 3. Formal Invariant K9 Enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "params"),
    [
        (ActionType.SCALE_WORKLOAD, ActionParams(workload="data-service", replica_delta=2)),
        (ActionType.RESTART_WORKLOAD, ActionParams(workload="data-service")),
        (
            ActionType.DISABLE_FLAG,
            ActionParams(workload="data-service", flag_name="EXPERIMENTAL_API"),
        ),
        (
            ActionType.REVERT_CONFIG,
            ActionParams(workload="data-service", config_key="LOG_LEVEL", config_value="DEBUG"),
        ),
        (
            ActionType.ROLLBACK_DEPLOY,
            ActionParams(
                workload="data-service",
                target_commit="deadbeef00000000000000000000000000000000",
            ),
        ),
    ],
)
def test_k9_reversibility_target_equality(action: ActionType, params: ActionParams) -> None:
    """Assert Invariant K9: inverse target set strictly equals plan target set."""
    ctx = _sample_context()
    target_resources = [
        ResourceRef(namespace="ust-twin-1", kind="Deployment", name="data-service"),
        ResourceRef(namespace="ust-twin-1", kind="ConfigMap", name="app-config"),
    ]
    plan = RemediationPlan(
        plan_id="plan_k9_test",
        candidate_index=0,
        action=action,
        params=params,
        target_resources=target_resources,
        declared_blast_set=["data-service", "edge-gateway"],
        inverse=None,
        rationale="Test plan for K9 invariant",
        origin="planner",
    )

    inv = synthesize_inverse(
        plan, context=ctx, config_lookup={"data-service": {"LOG_LEVEL": "INFO"}}
    )
    assert inv is not None

    # SMT assertion: plan.action != NO_ACTION ==>
    #   plan_has_inverse and inverse_targets = plan_targets
    assert inv.target_resources == plan.target_resources
    assert inv.declared_blast_set == plan.declared_blast_set
    assert inv.candidate_index == plan.candidate_index
    assert inv.plan_id == f"{plan.plan_id}_inv"
    assert inv.inverse is None


# ---------------------------------------------------------------------------
# 4. Property Test: AGENTS.md §6.6
# "The inverse of an inverse is the original plan."
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delta", [-5, -3, -1, 1, 2, 4, 10])
def test_property_scale_workload_inverse_of_inverse(delta: int) -> None:
    """Property test: inverse of inverse for SCALE_WORKLOAD is the original plan."""
    plan = RemediationPlan(
        plan_id="plan_scale_prop",
        candidate_index=1,
        action=ActionType.SCALE_WORKLOAD,
        params=ActionParams(workload="data-service", replica_delta=delta),
        target_resources=[
            ResourceRef(namespace="ust-twin", kind="Deployment", name="data-service")
        ],
        declared_blast_set=["data-service"],
        inverse=None,
        rationale=f"Scale by {delta}",
        origin="planner",
    )
    inv = synthesize_inverse(plan)
    assert inv is not None
    inv_inv = synthesize_inverse(inv)
    assert inv_inv is not None

    assert inv_inv.action == plan.action
    assert inv_inv.params.replica_delta == plan.params.replica_delta
    assert inv_inv.params.workload == plan.params.workload
    assert inv_inv.target_resources == plan.target_resources
    assert inv_inv.declared_blast_set == plan.declared_blast_set


@pytest.mark.parametrize(
    "flag_name",
    ["FEATURE_FLAG_1", "ENABLE_V2", "BETA_PATH", "experimental_query_v3"],
)
def test_property_disable_flag_inverse_of_inverse(flag_name: str) -> None:
    """Property test: inverse of inverse for DISABLE_FLAG is the original plan."""
    plan = RemediationPlan(
        plan_id="plan_flag_prop",
        candidate_index=0,
        action=ActionType.DISABLE_FLAG,
        params=ActionParams(workload="data-service", flag_name=flag_name),
        target_resources=[
            ResourceRef(namespace="ust-twin", kind="Deployment", name="data-service")
        ],
        declared_blast_set=["data-service"],
        inverse=None,
        rationale="Disable flag",
        origin="planner",
    )
    inv = synthesize_inverse(plan)
    assert inv is not None
    inv_inv = synthesize_inverse(inv)
    assert inv_inv is not None

    assert inv_inv.action == plan.action
    assert inv_inv.params == plan.params
    assert inv_inv.target_resources == plan.target_resources
    assert inv_inv.declared_blast_set == plan.declared_blast_set


@pytest.mark.parametrize("workload", ["data-service", "auth-service", "edge-gateway", "worker"])
def test_property_restart_workload_inverse_of_inverse(workload: str) -> None:
    """Property test: inverse of inverse for RESTART_WORKLOAD is the original plan."""
    plan = RemediationPlan(
        plan_id="plan_restart_prop",
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload=workload),
        target_resources=[ResourceRef(namespace="ust-twin", kind="Deployment", name=workload)],
        declared_blast_set=[workload],
        inverse=None,
        rationale="Restart workload",
        origin="planner",
    )
    inv = synthesize_inverse(plan)
    assert inv is not None
    inv_inv = synthesize_inverse(inv)
    assert inv_inv is not None

    assert inv_inv.action == plan.action
    assert inv_inv.params == plan.params
    assert inv_inv.target_resources == plan.target_resources
    assert inv_inv.declared_blast_set == plan.declared_blast_set


def test_property_rollback_deploy_inverse_of_inverse() -> None:
    """Property test: inverse of inverse for ROLLBACK_DEPLOY rolls back to original commit."""
    ctx = _sample_context()
    prior_sha = ctx.recent_deploys[1].commit_sha

    plan = RemediationPlan(
        plan_id="plan_rb_prop",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit=prior_sha),
        target_resources=[
            ResourceRef(namespace="ust-twin", kind="Deployment", name="data-service")
        ],
        declared_blast_set=["data-service"],
        inverse=None,
        rationale="Rollback to prior commit",
        origin="planner",
    )

    inv = synthesize_inverse(plan, context=ctx)
    assert inv is not None
    inv_inv = synthesize_inverse(inv, context=ctx)
    assert inv_inv is not None

    assert inv_inv.action == plan.action
    assert inv_inv.params.target_commit == plan.params.target_commit
    assert inv_inv.target_resources == plan.target_resources
    assert inv_inv.declared_blast_set == plan.declared_blast_set


def test_property_revert_config_inverse_of_inverse() -> None:
    """Property test: inverse of inverse for REVERT_CONFIG restores original config value."""
    plan = RemediationPlan(
        plan_id="plan_cfg_prop",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="app-config", config_key="LOG_LEVEL", config_value="DEBUG"),
        target_resources=[ResourceRef(namespace="ust-twin", kind="ConfigMap", name="app-config")],
        declared_blast_set=["data-service"],
        inverse=None,
        rationale="Set debug log level",
        origin="planner",
    )

    # First inverse sets to "INFO" via config_lookup
    inv = synthesize_inverse(plan, config_lookup={"app-config": {"LOG_LEVEL": "INFO"}})
    assert inv is not None
    assert inv.params.config_value == "INFO"

    # Inverting the inverse with current_config_value="DEBUG" yields the original plan value
    inv_inv = synthesize_inverse(inv, current_config_value="DEBUG")
    assert inv_inv is not None
    assert inv_inv.action == plan.action
    assert inv_inv.params == plan.params
    assert inv_inv.target_resources == plan.target_resources
    assert inv_inv.declared_blast_set == plan.declared_blast_set


def test_property_seeded_random_generative_exploration() -> None:
    """Generative property test over 50 seeded pseudo-random plans."""
    rng = random.Random(42)
    ctx = _sample_context()

    actions = [
        ActionType.SCALE_WORKLOAD,
        ActionType.RESTART_WORKLOAD,
        ActionType.DISABLE_FLAG,
        ActionType.ROLLBACK_DEPLOY,
        ActionType.REVERT_CONFIG,
    ]

    for _ in range(50):
        action = rng.choice(actions)
        workload = rng.choice(["data-service", "auth-service", "edge-gateway"])

        current_val: str | None = None
        if action == ActionType.SCALE_WORKLOAD:
            delta = rng.choice([-5, -3, -2, -1, 1, 2, 3, 5])
            params = ActionParams(workload=workload, replica_delta=delta)
        elif action == ActionType.RESTART_WORKLOAD:
            params = ActionParams(workload=workload)
        elif action == ActionType.DISABLE_FLAG:
            params = ActionParams(workload=workload, flag_name=f"FLAG_{rng.randint(1, 100)}")
        elif action == ActionType.ROLLBACK_DEPLOY:
            params = ActionParams(workload=workload, target_commit=ctx.recent_deploys[1].commit_sha)
        elif action == ActionType.REVERT_CONFIG:
            current_val = "VAL_A"
            params = ActionParams(workload=workload, config_key="LOG_LEVEL", config_value="VAL_B")

        plan = RemediationPlan(
            plan_id="plan_rand",
            candidate_index=0,
            action=action,
            params=params,
            target_resources=[ResourceRef(namespace="ust-twin", kind="Deployment", name=workload)],
            declared_blast_set=[workload],
            inverse=None,
            rationale="Generative plan",
            origin="planner",
        )

        inv = synthesize_inverse(plan, context=ctx, current_config_value=current_val)
        assert inv is not None
        assert inv.target_resources == plan.target_resources

        inv_inv = synthesize_inverse(
            inv, context=ctx, current_config_value="VAL_B" if current_val else None
        )
        assert inv_inv is not None
        assert inv_inv.action == plan.action
        assert inv_inv.params == plan.params
        assert inv_inv.target_resources == plan.target_resources
        assert inv_inv.declared_blast_set == plan.declared_blast_set


def test_synthesize_inverse_unsupported_action_raises() -> None:
    """Unsupported action type raises PlannerError."""
    plan = RemediationPlan(
        plan_id="plan_unsupported",
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload="data-service"),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="test",
        origin="planner",
    )
    # Force an invalid action value to test the fallback branch
    object.__setattr__(plan, "action", "unsupported_action")
    with pytest.raises(PlannerError) as exc:
        synthesize_inverse(plan)
    assert "Unsupported action type" in str(exc.value)


def test_synthesize_inverse_config_lookup_wildcard_miss() -> None:
    """Config lookup with wildcard but missing key continues to fallback."""
    plan = RemediationPlan(
        plan_id="p_cfg_miss",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="app-config", config_key="LOG_LEVEL", config_value="DEBUG"),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="r",
        origin="planner",
    )
    # Wildcard map present, but does not contain LOG_LEVEL
    lookup = {"": {"OTHER_KEY": "other_val"}}
    with pytest.raises(PlannerError, match="pre-intervention config_value unknown"):
        synthesize_inverse(plan, config_lookup=lookup)


def test_synthesize_inverse_rollback_all_deploys_match_head() -> None:
    """If all deploys in history match target commit, attached inverse is NOT trusted.

    Raises PlannerError unless current_commit given.
    """
    head_sha = "c0ffee0000000000000000000000000000000000"
    ctx_dup = IncidentContext(
        incident_id="inc_dup",
        alert=Alert(
            alert_id="a1",
            source="synthetic",
            title="t",
            service="data-service",
            severity="critical",
            fired_at=FIXED_TIME,
        ),
        signatures=[],
        metrics_window=MetricWindow(
            service="data-service", start_time=FIXED_TIME, end_time=FIXED_TIME
        ),
        recent_deploys=[
            DeployRef(
                commit_sha=head_sha, image_digests={"data-service": "s1"}, deployed_at=FIXED_TIME
            ),
            DeployRef(
                commit_sha=head_sha, image_digests={"data-service": "s1"}, deployed_at=FIXED_TIME
            ),
        ],
        dependency_graph=DependencyGraphSnapshot(
            nodes=["data-service"], edges=[], observed_at=FIXED_TIME
        ),
        gathered_at=FIXED_TIME,
    )
    # Even if attached inverse has a commit, it is NOT trusted (deterministic safety)
    plan_with_inv = RemediationPlan(
        plan_id="p_dup",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit=head_sha),
        target_resources=[],
        declared_blast_set=[],
        inverse=RemediationPlan(
            plan_id="p_inv",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=ActionParams(workload="data-service", target_commit="prior_sha"),
            target_resources=[],
            declared_blast_set=[],
            inverse=None,
            rationale="inv",
            origin="planner",
        ),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(PlannerError, match="pre-incident commit unknown"):
        synthesize_inverse(plan_with_inv, context=ctx_dup)

    # When explicit current_commit is supplied, it resolves deterministically
    inv_explicit = synthesize_inverse(plan_with_inv, context=ctx_dup, current_commit="prior_sha")
    assert inv_explicit is not None
    assert inv_explicit.params.target_commit == "prior_sha"
