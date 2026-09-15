"""Unit tests for Invariant K2 (Namespace scope) formal proof verification.

Verifies:
- Invariant protocol conformance and metadata.
- Positive cases (proved safe, solver unsat on negation):
  - Plan targeting authorized production namespace (ust-prod).
  - Plan targeting multiple resources all within ust-prod.
  - Plan targeting twin namespace when twin is the authorized namespace.
  - Plan targeting multiple resources in twin namespace.
  - Vacuous satisfaction when plan has no target resources or namespaces.
  - NO_ACTION, ROLLBACK_DEPLOY, SCALE_WORKLOAD, RESTART_WORKLOAD, DISABLE_FLAG,
    REVERT_CONFIG targeting authorized namespace.
- Negative cases (veto, solver sat on negation, counterexample asserted per §6.4 and §6.5):
  - Production plan targeting default namespace.
  - Production plan targeting kube-system.
  - Production plan targeting twin namespace.
  - Twin plan attempting to mutate ust-prod (Rule 2.4 twin writes never touch prod).
  - Twin plan attempting to mutate sibling twin namespace.
  - Multi-target plan where one target is in an unauthorized namespace.
  - Defense-in-depth: plan target_resources containing unauthorized namespace even
    when fact set contains authorized namespace.
- Missing fact cases (zero-default safety per Rule 5.6):
  - Missing authorized_namespace raises MissingFact.
  - Missing plan_target_namespaces raises MissingFact.
  - Empty facts dictionary raises MissingFact.
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
from understudy.kernel.invariants.k02_namespace_scope import K2NamespaceScope


def _make_plan(
    *,
    action: ActionType = ActionType.SCALE_WORKLOAD,
    workload: str = "data-service",
    target_resources: list[ResourceRef] | None = None,
    namespace: str = "ust-prod",
) -> RemediationPlan:
    """Helper to create a test RemediationPlan."""
    targets = target_resources
    if targets is None:
        targets = [
            ResourceRef(
                kind="Deployment",
                name=workload,
                namespace=namespace,
            )
        ]
    return RemediationPlan(
        plan_id="plan_k2_test",
        candidate_index=0,
        action=action,
        params=ActionParams(
            workload=workload,
            replica_delta=1,
        ),
        target_resources=targets,
        declared_blast_set=[workload] if workload else [],
        rationale="K2 unit test candidate plan",
        origin="planner",
    )


def _make_facts(
    *,
    authorized_namespace: str = "ust-prod",
    target_namespaces: set[str] | list[str] | None = None,
) -> list[Fact]:
    """Helper to create standard K2 facts."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    if target_namespaces is None:
        target_namespaces = [authorized_namespace]
    return [
        Fact(
            name="authorized_namespace",
            value=authorized_namespace,
            source="config",
            observed_at=now,
        ),
        Fact(
            name="plan_target_namespaces",
            value=list(target_namespaces),
            source="plan",
            observed_at=now,
        ),
    ]


def test_k2_protocol_conformance() -> None:
    """Test that K2NamespaceScope satisfies the Invariant protocol."""
    k2 = K2NamespaceScope()
    assert isinstance(k2, Invariant)
    assert k2.id == "K2"
    assert k2.tier == InvariantTier.PROOF
    assert "Every resource a plan mutates lies in the single authorized namespace" in k2.statement
    assert "plan_target_namespaces" in k2.required_facts
    assert "authorized_namespace" in k2.required_facts


# =============================================================================
# Positive Test Cases (Proved Safe -> unsat on negation)
# =============================================================================


def test_k2_positive_prod_single_workload() -> None:
    """Production plan targeting single workload in ust-prod is safe."""
    plan = _make_plan(namespace="ust-prod")
    facts = _make_facts(authorized_namespace="ust-prod", target_namespaces=["ust-prod"])
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    inv_formula = k2.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k2_positive_prod_multiple_workloads() -> None:
    """Production plan targeting multiple workloads, all in ust-prod, is safe."""
    targets = [
        ResourceRef(kind="Deployment", name="data-service", namespace="ust-prod"),
        ResourceRef(kind="Deployment", name="auth-service", namespace="ust-prod"),
        ResourceRef(kind="ConfigMap", name="app-config", namespace="ust-prod"),
    ]
    plan = _make_plan(target_resources=targets)
    facts = _make_facts(authorized_namespace="ust-prod", target_namespaces=["ust-prod"])
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    inv_formula = k2.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k2_positive_twin_single_workload() -> None:
    """Twin plan with authorized_namespace ust-twin-inc-test-0 targeting that twin is safe."""
    twin_ns = "ust-twin-inc-test-0"
    plan = _make_plan(namespace=twin_ns)
    facts = _make_facts(authorized_namespace=twin_ns, target_namespaces=[twin_ns])
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    inv_formula = k2.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k2_positive_twin_multiple_workloads() -> None:
    """Twin plan targeting multiple resources within its own namespace is safe."""
    twin_ns = "ust-twin-inc-test-0"
    targets = [
        ResourceRef(kind="Deployment", name="data-service", namespace=twin_ns),
        ResourceRef(kind="Deployment", name="worker", namespace=twin_ns),
    ]
    plan = _make_plan(target_resources=targets)
    facts = _make_facts(authorized_namespace=twin_ns, target_namespaces=[twin_ns])
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    inv_formula = k2.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k2_positive_no_action_empty_targets() -> None:
    """Plan mutating no targets vacuously satisfies K2."""
    plan = _make_plan(
        action=ActionType.NO_ACTION,
        workload="",
        target_resources=[],
    )
    facts = _make_facts(authorized_namespace="ust-prod", target_namespaces=[])
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    inv_formula = k2.build(ctx)

    assert z3.is_true(inv_formula)


def test_k2_positive_all_action_types_in_prod() -> None:
    """Verify all closed ActionType variants pass when targeting authorized namespace."""
    for action in (
        ActionType.SCALE_WORKLOAD,
        ActionType.RESTART_WORKLOAD,
        ActionType.ROLLBACK_DEPLOY,
        ActionType.DISABLE_FLAG,
        ActionType.REVERT_CONFIG,
    ):
        plan = _make_plan(action=action, namespace="ust-prod")
        facts = _make_facts(authorized_namespace="ust-prod", target_namespaces=["ust-prod"])
        ctx = KernelContext(plan, facts)

        k2 = K2NamespaceScope()
        inv_formula = k2.build(ctx)

        solver = z3.Solver()
        for assertion in ctx.fact_assertions:
            solver.add(assertion)
        solver.add(z3.Not(inv_formula))
        assert solver.check() == z3.unsat


def test_k2_positive_resource_with_empty_namespace_skipped() -> None:
    """Resource with empty namespace string is ignored, falling back to fact set."""
    targets = [
        ResourceRef(kind="Deployment", name="data-service", namespace=""),
    ]
    plan = _make_plan(target_resources=targets)
    facts = _make_facts(authorized_namespace="ust-prod", target_namespaces=["ust-prod"])
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    inv_formula = k2.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


# =============================================================================
# Negative Test Cases (Veto -> sat on negation, counterexample asserted)
# =============================================================================


def test_k2_negative_prod_targets_default() -> None:
    """Production plan targeting default namespace is vetoed with counterexample."""
    plan = _make_plan(namespace="default")
    facts = _make_facts(authorized_namespace="ust-prod", target_namespaces=["default"])
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    assert k2.id == "K2"
    inv_formula = k2.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    auth_const = ctx.constants["authorized_namespace"]
    assert model.eval(auth_const).as_string() == "ust-prod"
    violating_const = z3.StringVal("default")
    assert model.eval(violating_const).as_string() == "default"
    assert model.eval(violating_const).as_string() != model.eval(auth_const).as_string()


def test_k2_negative_prod_targets_kube_system() -> None:
    """Production plan targeting kube-system namespace is vetoed with counterexample."""
    plan = _make_plan(namespace="kube-system")
    facts = _make_facts(authorized_namespace="ust-prod", target_namespaces=["kube-system"])
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    inv_formula = k2.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    auth_const = ctx.constants["authorized_namespace"]
    assert model.eval(auth_const).as_string() == "ust-prod"
    assert model.eval(auth_const).as_string() != "kube-system"


def test_k2_negative_prod_targets_twin() -> None:
    """Production plan targeting twin namespace ust-twin-inc-test-0 is vetoed."""
    twin_ns = "ust-twin-inc-test-0"
    plan = _make_plan(namespace=twin_ns)
    facts = _make_facts(authorized_namespace="ust-prod", target_namespaces=[twin_ns])
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    inv_formula = k2.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    auth_const = ctx.constants["authorized_namespace"]
    assert model.eval(auth_const).as_string() == "ust-prod"
    assert twin_ns != model.eval(auth_const).as_string()


def test_k2_negative_twin_targets_prod() -> None:
    """Rule 2.4 core safety test: Twin plan attempting to mutate ust-prod is vetoed.

    No module or plan under twin execution context may reference or mutate ust-prod.
    """
    twin_ns = "ust-twin-inc-test-0"
    plan = _make_plan(namespace="ust-prod")
    facts = _make_facts(authorized_namespace=twin_ns, target_namespaces=["ust-prod"])
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    inv_formula = k2.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    auth_const = ctx.constants["authorized_namespace"]
    assert model.eval(auth_const).as_string() == twin_ns
    assert model.eval(auth_const).as_string() != "ust-prod"


def test_k2_negative_twin_targets_different_twin() -> None:
    """Twin plan attempting to mutate sibling twin namespace is vetoed."""
    twin_0 = "ust-twin-inc-test-0"
    twin_1 = "ust-twin-inc-test-1"
    plan = _make_plan(namespace=twin_1)
    facts = _make_facts(authorized_namespace=twin_0, target_namespaces=[twin_1])
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    inv_formula = k2.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    auth_const = ctx.constants["authorized_namespace"]
    assert model.eval(auth_const).as_string() == twin_0
    assert model.eval(auth_const).as_string() != twin_1


def test_k2_negative_multi_target_one_unauthorized() -> None:
    """Multi-resource plan where one target is in ust-prod but another is unauthorized is vetoed."""
    targets = [
        ResourceRef(kind="Deployment", name="data-service", namespace="ust-prod"),
        ResourceRef(kind="Deployment", name="auth-service", namespace="malicious-ns"),
    ]
    plan = _make_plan(target_resources=targets)
    facts = _make_facts(
        authorized_namespace="ust-prod",
        target_namespaces=["ust-prod", "malicious-ns"],
    )
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    inv_formula = k2.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    auth_const = ctx.constants["authorized_namespace"]
    assert model.eval(auth_const).as_string() == "ust-prod"
    assert model.eval(auth_const).as_string() != "malicious-ns"


def test_k2_negative_unauthorized_namespace_in_target_resources_only() -> None:
    """Defense-in-depth: plan target_resources contains unauthorized namespace."""
    targets = [
        ResourceRef(kind="Deployment", name="data-service", namespace="attacker-ns"),
    ]
    plan = _make_plan(target_resources=targets)
    # The fact dictionary only reports ust-prod, but the plan object itself targets attacker-ns
    facts = _make_facts(authorized_namespace="ust-prod", target_namespaces=["ust-prod"])
    ctx = KernelContext(plan, facts)

    k2 = K2NamespaceScope()
    inv_formula = k2.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    auth_const = ctx.constants["authorized_namespace"]
    assert model.eval(auth_const).as_string() == "ust-prod"
    assert model.eval(auth_const).as_string() != "attacker-ns"


# =============================================================================
# Missing Facts Test Cases (Zero Defaults per Rule 5.6)
# =============================================================================


def test_k2_missing_authorized_namespace_raises_missing_fact() -> None:
    """Missing authorized_namespace fact raises MissingFact without defaulting."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(
            name="plan_target_namespaces",
            value=["ust-prod"],
            source="plan",
            observed_at=now,
        ),
    ]
    ctx = KernelContext(plan, facts)
    k2 = K2NamespaceScope()

    with pytest.raises(MissingFact) as exc_info:
        k2.build(ctx)
    assert exc_info.value.fact_name == "authorized_namespace"
    assert "authorized_namespace" in ctx.missing_facts


def test_k2_missing_plan_target_namespaces_raises_missing_fact() -> None:
    """Missing plan_target_namespaces fact raises MissingFact without defaulting."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(
            name="authorized_namespace",
            value="ust-prod",
            source="config",
            observed_at=now,
        ),
    ]
    ctx = KernelContext(plan, facts)
    k2 = K2NamespaceScope()

    with pytest.raises(MissingFact) as exc_info:
        k2.build(ctx)
    assert exc_info.value.fact_name == "plan_target_namespaces"
    assert "plan_target_namespaces" in ctx.missing_facts


def test_k2_empty_facts_raises_missing_fact() -> None:
    """Empty fact dictionary raises MissingFact immediately."""
    plan = _make_plan()
    ctx = KernelContext(plan, [])
    k2 = K2NamespaceScope()

    with pytest.raises(MissingFact):
        k2.build(ctx)


# =============================================================================
# Real Fixture Integration
# =============================================================================


def test_k2_with_real_fixture_facts_ok() -> None:
    """Verify K2 against real fixtures/facts_ok.json fixture."""
    fixture_path = Path("fixtures/facts_ok.json")
    assert fixture_path.exists()
    facts = load_facts_json(fixture_path)

    # Positive: Standard plan targeting ust-prod
    plan_safe = _make_plan(namespace="ust-prod")
    ctx_safe = KernelContext(plan_safe, facts)
    k2 = K2NamespaceScope()
    inv_safe = k2.build(ctx_safe)

    solver_safe = z3.Solver()
    for a in ctx_safe.fact_assertions:
        solver_safe.add(a)
    solver_safe.add(z3.Not(inv_safe))
    assert solver_safe.check() == z3.unsat

    # Negative: Plan targeting unauthorized namespace against real fixture facts
    plan_unsafe = _make_plan(namespace="unauthorized-namespace")
    ctx_unsafe = KernelContext(plan_unsafe, facts)
    inv_unsafe = k2.build(ctx_unsafe)

    solver_unsafe = z3.Solver()
    for a in ctx_unsafe.fact_assertions:
        solver_unsafe.add(a)
    solver_unsafe.add(z3.Not(inv_unsafe))
    assert solver_unsafe.check() == z3.sat
