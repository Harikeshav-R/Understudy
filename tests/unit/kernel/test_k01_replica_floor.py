"""Unit tests for Invariant K1 (Replica floor) formal proof verification.

Verifies:
- Invariant protocol conformance and metadata.
- Positive cases (proved safe, solver unsat on negation):
  - Scale workload up.
  - Scale workload down within floor.
  - Restart workload when healthy replicas exceed floor.
  - Rollback deploy, no-action, disable flag, revert config.
  - Plans with non-deployment resources (e.g. ConfigMap only).
  - Multi-workload plans where all targets satisfy the floor.
- Negative cases (veto, solver sat on negation, counterexample asserted per §6.4 and §6.5):
  - Scale workload to zero.
  - Scale workload below configured min_replicas.
  - Scale workload down during partial outage.
  - Restart workload during partial outage (transient reduction causes veto).
  - Restart workload on a single-replica service.
  - Multi-workload plans where one target violates the floor.
- Missing fact cases (zero-default safety per Rule 5.6):
  - Missing replicas fact raises MissingFact.
  - Missing min_replicas fact raises MissingFact.
  - Missing healthy_replicas fact raises MissingFact.
- Integration with fixtures/facts_ok.json.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import z3

from understudy.common.errors import MissingFact
from understudy.contracts.enums import ActionType, InvariantTier
from understudy.contracts.kernel import Fact
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.kernel.dsl import Invariant, KernelContext
from understudy.kernel.facts import load_facts_json
from understudy.kernel.invariants.k01_replica_floor import K1ReplicaFloor


def _make_plan(
    *,
    action: ActionType = ActionType.SCALE_WORKLOAD,
    workload: str = "data-service",
    replica_delta: int | None = 1,
    target_resources: list[ResourceRef] | None = None,
) -> RemediationPlan:
    """Helper to create a test RemediationPlan."""
    targets = target_resources
    if targets is None:
        targets = [
            ResourceRef(
                kind="Deployment",
                name=workload,
                namespace="ust-prod",
            )
        ]
    return RemediationPlan(
        plan_id="plan_k1_test",
        candidate_index=0,
        action=action,
        params=ActionParams(
            workload=workload,
            replica_delta=replica_delta,
        ),
        target_resources=targets,
        declared_blast_set=[workload],
        rationale="K1 unit test candidate plan",
        origin="planner",
    )


def _make_facts(
    *,
    service: str = "data-service",
    replicas: int = 2,
    healthy_replicas: int = 2,
    min_replicas: int = 1,
) -> list[Fact]:
    """Helper to create standard replica facts for a service."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    return [
        Fact(name=f"replicas[{service}]", value=replicas, source="k8s", observed_at=now),
        Fact(
            name=f"healthy_replicas[{service}]",
            value=healthy_replicas,
            source="k8s",
            observed_at=now,
        ),
        Fact(
            name=f"min_replicas[{service}]",
            value=min_replicas,
            source="config",
            observed_at=now,
        ),
    ]


def test_k1_protocol_conformance() -> None:
    """Test that K1ReplicaFloor satisfies the Invariant protocol."""
    k1 = K1ReplicaFloor()
    assert isinstance(k1, Invariant)
    assert k1.id == "K1"
    assert k1.tier == InvariantTier.PROOF
    assert "No plan may leave any service with fewer healthy replicas" in k1.statement
    assert "replicas[svc]" in k1.required_facts
    assert "min_replicas[svc]" in k1.required_facts
    assert "healthy_replicas[svc]" in k1.required_facts


# =============================================================================
# Positive Test Cases (Proved Safe -> unsat on negation)
# =============================================================================


def test_k1_positive_scale_up() -> None:
    """Scale workload up: replicas=2, healthy=2, min=1, delta=+2 -> safe (4 >= 1)."""
    plan = _make_plan(action=ActionType.SCALE_WORKLOAD, replica_delta=2)
    facts = _make_facts(replicas=2, healthy_replicas=2, min_replicas=1)
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k1_positive_scale_down_within_floor() -> None:
    """Scale workload down safely: replicas=3, healthy=3, min=1, delta=-1 -> safe (2 >= 1)."""
    plan = _make_plan(action=ActionType.SCALE_WORKLOAD, replica_delta=-1)
    facts = _make_facts(replicas=3, healthy_replicas=3, min_replicas=1)
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k1_positive_restart_healthy() -> None:
    """Restart workload with ample healthy pods: replicas=2, healthy=2, min=1.

    Transient reduction takes healthy 2 -> 1, which still satisfies min_replicas 1.
    """
    plan = _make_plan(action=ActionType.RESTART_WORKLOAD, replica_delta=None)
    facts = _make_facts(replicas=2, healthy_replicas=2, min_replicas=1)
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k1_positive_restart_via_target_resources_only() -> None:
    """Restart workload identified via target_resources with empty params.workload."""
    plan = _make_plan(
        action=ActionType.RESTART_WORKLOAD,
        workload="",
        replica_delta=None,
        target_resources=[
            ResourceRef(kind="Deployment", name="data-service", namespace="ust-prod")
        ],
    )
    facts = _make_facts(replicas=2, healthy_replicas=2, min_replicas=1)
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k1_positive_no_action() -> None:
    """NO_ACTION leaves replicas unchanged (delta=0): replicas=2, healthy=2, min=1."""
    plan = _make_plan(action=ActionType.NO_ACTION, replica_delta=None)
    facts = _make_facts(replicas=2, healthy_replicas=2, min_replicas=1)
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k1_positive_rollback_deploy() -> None:
    """ROLLBACK_DEPLOY leaves replica counts untouched (delta=0): replicas=2, min=1."""
    plan = _make_plan(action=ActionType.ROLLBACK_DEPLOY, replica_delta=None)
    facts = _make_facts(replicas=2, healthy_replicas=2, min_replicas=1)
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k1_positive_disable_flag_and_revert_config() -> None:
    """DISABLE_FLAG and REVERT_CONFIG leave replica counts untouched."""
    facts = _make_facts(replicas=2, healthy_replicas=2, min_replicas=1)

    for action in (ActionType.DISABLE_FLAG, ActionType.REVERT_CONFIG):
        plan = _make_plan(action=action, replica_delta=None)
        ctx = KernelContext(plan, facts)
        k1 = K1ReplicaFloor()
        inv_formula = k1.build(ctx)

        solver = z3.Solver()
        for assertion in ctx.fact_assertions:
            solver.add(assertion)
        solver.add(z3.Not(inv_formula))
        assert solver.check() == z3.unsat


def test_k1_positive_no_deployment_targets() -> None:
    """Plan mutating only a ConfigMap with no workload has no replica floor constraint."""
    plan = RemediationPlan(
        plan_id="plan_config_only",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="", config_key="app-config", config_value="{}"),
        target_resources=[ResourceRef(kind="ConfigMap", name="app-config", namespace="ust-prod")],
        declared_blast_set=[],
        rationale="Revert config only",
        origin="planner",
    )
    ctx = KernelContext(plan, [])
    k1 = K1ReplicaFloor()
    formula = k1.build(ctx)

    # Trivially True
    assert z3.is_true(formula)


def test_k1_positive_multiple_targets_all_safe() -> None:
    """Multi-deployment plan where all targets satisfy replica floors."""
    targets = [
        ResourceRef(kind="Deployment", name="data-service", namespace="ust-prod"),
        ResourceRef(kind="Deployment", name="auth-service", namespace="ust-prod"),
    ]
    plan = _make_plan(
        action=ActionType.SCALE_WORKLOAD,
        workload="data-service",
        replica_delta=1,
        target_resources=targets,
    )
    facts = _make_facts(service="data-service", replicas=2, healthy_replicas=2, min_replicas=1)
    facts.extend(
        _make_facts(service="auth-service", replicas=2, healthy_replicas=2, min_replicas=1)
    )
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k1_scale_workload_none_delta_defaults_to_zero() -> None:
    """SCALE_WORKLOAD with None replica_delta behaves safely as delta=0."""
    plan = _make_plan(action=ActionType.SCALE_WORKLOAD, replica_delta=None)
    facts = _make_facts(replicas=2, healthy_replicas=2, min_replicas=1)
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


# =============================================================================
# Negative Test Cases (Veto -> sat on negation, counterexample asserted)
# =============================================================================


def test_k1_negative_scale_to_zero() -> None:
    """Negative: SCALE_WORKLOAD scaling to 0 (delta=-2, replicas=2, min=1).

    Per §6.4 and §6.5: asserts sat, asserts invariant is K1, asserts counterexample.
    """
    plan = _make_plan(action=ActionType.SCALE_WORKLOAD, replica_delta=-2)
    facts = _make_facts(replicas=2, healthy_replicas=2, min_replicas=1)
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    assert k1.id == "K1"
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    # sat means a counterexample exists -> VETO!
    assert solver.check() == z3.sat
    model = solver.model()

    # Assert specific counterexample values in the model
    rep_const = ctx.constants["replicas[data-service]"]
    min_const = ctx.constants["min_replicas[data-service]"]
    assert model.eval(rep_const).as_long() == 2
    assert model.eval(min_const).as_long() == 1

    # Desired post replicas: 2 + (-2) = 0, which is strictly less than min_replicas (1)
    post_reps = model.eval(rep_const + z3.IntVal(-2)).as_long()
    assert post_reps == 0
    assert post_reps < model.eval(min_const).as_long()


def test_k1_negative_scale_below_floor() -> None:
    """Negative: SCALE_WORKLOAD scaling below floor (delta=-1, replicas=2, min=2)."""
    plan = _make_plan(action=ActionType.SCALE_WORKLOAD, replica_delta=-1)
    facts = _make_facts(replicas=2, healthy_replicas=2, min_replicas=2)
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    rep_const = ctx.constants["replicas[data-service]"]
    min_const = ctx.constants["min_replicas[data-service]"]
    post_reps = model.eval(rep_const + z3.IntVal(-1)).as_long()
    assert post_reps == 1
    assert post_reps < model.eval(min_const).as_long()


def test_k1_negative_scale_down_during_partial_outage() -> None:
    """Negative: SCALE_WORKLOAD delta=-1 when replicas=2, healthy=1, min=1.

    Although desired replicas becomes 2 - 1 = 1 >= 1, healthy replicas drops to
    1 - 1 = 0 < 1. K1 must veto to protect healthy replica availability.
    """
    plan = _make_plan(action=ActionType.SCALE_WORKLOAD, replica_delta=-1)
    facts = _make_facts(replicas=2, healthy_replicas=1, min_replicas=1)
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    healthy_const = ctx.constants["healthy_replicas[data-service]"]
    min_const = ctx.constants["min_replicas[data-service]"]
    assert model.eval(healthy_const).as_long() == 1
    post_healthy = model.eval(healthy_const + z3.IntVal(-1)).as_long()
    assert post_healthy == 0
    assert post_healthy < model.eval(min_const).as_long()


def test_k1_negative_restart_during_partial_outage() -> None:
    """Negative: RESTART_WORKLOAD during partial outage (replicas=2, healthy=1, min=1).

    Rolling restart takes down maxUnavailable (1) pod, reducing healthy from 1 -> 0 < 1.
    Per docs/03-invariants.md §3.4: this transient reduction makes restart unsafe during
    a partial outage, and modelling it is the point.
    """
    plan = _make_plan(action=ActionType.RESTART_WORKLOAD, replica_delta=None)
    facts = _make_facts(replicas=2, healthy_replicas=1, min_replicas=1)
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    assert k1.id == "K1"
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    healthy_const = ctx.constants["healthy_replicas[data-service]"]
    min_const = ctx.constants["min_replicas[data-service]"]
    assert model.eval(healthy_const).as_long() == 1
    # Transient reduction of 1 leaves 0 healthy pods
    post_healthy = model.eval(healthy_const - z3.IntVal(1)).as_long()
    assert post_healthy == 0
    assert post_healthy < model.eval(min_const).as_long()


def test_k1_negative_restart_single_replica_workload() -> None:
    """Negative: RESTART_WORKLOAD on single-replica service (replicas=1, healthy=1, min=1).

    Transient reduction takes healthy 1 -> 0 < 1.
    """
    plan = _make_plan(
        action=ActionType.RESTART_WORKLOAD,
        workload="worker",
        replica_delta=None,
        target_resources=[ResourceRef(kind="Deployment", name="worker", namespace="ust-prod")],
    )
    facts = _make_facts(service="worker", replicas=1, healthy_replicas=1, min_replicas=1)
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    healthy_const = ctx.constants["healthy_replicas[worker]"]
    min_const = ctx.constants["min_replicas[worker]"]
    post_healthy = model.eval(healthy_const - z3.IntVal(1)).as_long()
    assert post_healthy == 0
    assert post_healthy < model.eval(min_const).as_long()


def test_k1_negative_multiple_targets_one_violates() -> None:
    """Negative: Multi-deployment plan where one service violates replica floor."""
    targets = [
        ResourceRef(kind="Deployment", name="data-service", namespace="ust-prod"),
        ResourceRef(kind="Deployment", name="auth-service", namespace="ust-prod"),
    ]
    # Scales data-service by -2 (scale to zero)
    plan = _make_plan(
        action=ActionType.SCALE_WORKLOAD,
        workload="data-service",
        replica_delta=-2,
        target_resources=targets,
    )
    facts = _make_facts(service="data-service", replicas=2, healthy_replicas=2, min_replicas=1)
    facts.extend(
        _make_facts(service="auth-service", replicas=2, healthy_replicas=2, min_replicas=1)
    )
    ctx = KernelContext(plan, facts)

    k1 = K1ReplicaFloor()
    inv_formula = k1.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    data_rep_const = ctx.constants["replicas[data-service]"]
    data_min_const = ctx.constants["min_replicas[data-service]"]
    post_reps = model.eval(data_rep_const + z3.IntVal(-2)).as_long()
    assert post_reps == 0
    assert post_reps < model.eval(data_min_const).as_long()


# =============================================================================
# Missing Facts Test Cases (Zero Defaults per Rule 5.6)
# =============================================================================


def test_k1_missing_replicas_raises_missing_fact() -> None:
    """Missing replicas[svc] fact raises MissingFact without defaulting."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="healthy_replicas[data-service]", value=2, source="k8s", observed_at=now),
        Fact(name="min_replicas[data-service]", value=1, source="config", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)
    k1 = K1ReplicaFloor()

    with pytest.raises(MissingFact) as exc_info:
        k1.build(ctx)
    assert exc_info.value.fact_name == "replicas[data-service]"
    assert "replicas[data-service]" in ctx.missing_facts


def test_k1_missing_min_replicas_raises_missing_fact() -> None:
    """Missing min_replicas[svc] fact raises MissingFact without defaulting."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="replicas[data-service]", value=2, source="k8s", observed_at=now),
        Fact(name="healthy_replicas[data-service]", value=2, source="k8s", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)
    k1 = K1ReplicaFloor()

    with pytest.raises(MissingFact) as exc_info:
        k1.build(ctx)
    assert exc_info.value.fact_name == "min_replicas[data-service]"
    assert "min_replicas[data-service]" in ctx.missing_facts


def test_k1_missing_healthy_replicas_raises_missing_fact() -> None:
    """Missing healthy_replicas[svc] fact raises MissingFact without defaulting."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="replicas[data-service]", value=2, source="k8s", observed_at=now),
        Fact(name="min_replicas[data-service]", value=1, source="config", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)
    k1 = K1ReplicaFloor()

    with pytest.raises(MissingFact) as exc_info:
        k1.build(ctx)
    assert exc_info.value.fact_name == "healthy_replicas[data-service]"
    assert "healthy_replicas[data-service]" in ctx.missing_facts


def test_k1_facts_for_different_service_raises_missing_fact() -> None:
    """Facts present only for auth-service when plan targets data-service raises MissingFact."""
    plan = _make_plan(workload="data-service")
    facts = _make_facts(service="auth-service", replicas=2, healthy_replicas=2, min_replicas=1)
    ctx = KernelContext(plan, facts)
    k1 = K1ReplicaFloor()

    with pytest.raises(MissingFact):
        k1.build(ctx)
    assert any("data-service" in f for f in ctx.missing_facts)


# =============================================================================
# Real Fixture Integration
# =============================================================================


def test_k1_with_real_fixture_facts_ok() -> None:
    """Verify K1 against real fixtures/facts_ok.json fixture."""
    fixture_path = Path("fixtures/facts_ok.json")
    assert fixture_path.exists()
    facts = load_facts_json(fixture_path)

    # Positive: Scale data-service up by 1
    plan_safe = _make_plan(action=ActionType.SCALE_WORKLOAD, replica_delta=1)
    ctx_safe = KernelContext(plan_safe, facts)
    k1 = K1ReplicaFloor()
    inv_safe = k1.build(ctx_safe)

    solver = z3.Solver()
    for a in ctx_safe.fact_assertions:
        solver.add(a)
    solver.add(z3.Not(inv_safe))
    assert solver.check() == z3.unsat

    # Negative: Scale data-service to zero (delta = -2)
    plan_unsafe = _make_plan(action=ActionType.SCALE_WORKLOAD, replica_delta=-2)
    ctx_unsafe = KernelContext(plan_unsafe, facts)
    inv_unsafe = k1.build(ctx_unsafe)

    solver_unsafe = z3.Solver()
    for a in ctx_unsafe.fact_assertions:
        solver_unsafe.add(a)
    solver_unsafe.add(z3.Not(inv_unsafe))
    assert solver_unsafe.check() == z3.sat
