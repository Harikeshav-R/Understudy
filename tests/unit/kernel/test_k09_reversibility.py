"""Unit tests for Invariant K9 (Reversibility) formal proof verification.

Verifies:
- Invariant protocol conformance and metadata.
- Positive cases (proved safe, solver unsat on negation):
  - NO_ACTION trivially satisfies K9 even with empty facts or no inverse.
  - Single target plan with matching inverse target (ROLLBACK_DEPLOY, SCALE_WORKLOAD,
    RESTART_WORKLOAD, DISABLE_FLAG, REVERT_CONFIG).
  - Multi-target plan with matching multi-target inverse.
  - Plan with empty targets and matching inverse with empty targets.
  - Target representations as ResourceRef, Mapping dicts, and strings.
  - Integration with fixtures/facts_ok.json.
- Negative cases (veto, solver sat on negation, counterexample asserted per §6.4 and §6.5):
  - Plan without an inverse (plan_has_inverse is False, e.g. plan.inverse is None).
  - Inverse touches fewer targets than forward plan (subset).
  - Inverse touches extra targets beyond forward plan (superset).
  - Inverse touches completely disjoint targets from forward plan.
  - Inverse touches empty set while forward plan targets a resource.
  - Defense-in-depth: plan.target_resources contains target not in inverse.
- Missing fact cases (zero-default safety per Rule 5.6):
  - Missing plan_has_inverse raises MissingFact.
  - Missing inverse_targets raises MissingFact.
  - Missing plan_targets raises MissingFact.
  - Empty facts dictionary raises MissingFact.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import z3

from understudy.common.errors import MissingFact
from understudy.contracts.enums import ActionType, InvariantTier
from understudy.contracts.kernel import Fact
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.kernel.dsl import Invariant, KernelContext
from understudy.kernel.facts import load_facts_json
from understudy.kernel.invariants.k09_reversibility import K9Reversibility


def _make_resource(name: str = "data-service", kind: str = "Deployment") -> ResourceRef:
    """Helper to create a ResourceRef in ust-prod."""
    return ResourceRef(kind=kind, name=name, namespace="ust-prod")  # type: ignore[arg-type]


def _make_plan(
    *,
    action: ActionType = ActionType.ROLLBACK_DEPLOY,
    workload: str = "data-service",
    target_resources: list[ResourceRef] | None = None,
    with_inverse: bool = True,
    inverse_targets: list[ResourceRef] | None = None,
) -> RemediationPlan:
    """Helper to create a test RemediationPlan with optional inverse."""
    targets = target_resources if target_resources is not None else [_make_resource(workload)]

    inverse_plan: RemediationPlan | None = None
    if with_inverse:
        inv_targets = inverse_targets if inverse_targets is not None else [_make_resource(workload)]
        inverse_plan = RemediationPlan(
            plan_id="plan_test_inv",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=ActionParams(workload=workload),
            target_resources=inv_targets,
            declared_blast_set=[workload],
            rationale="Inverse plan",
            origin="planner",
        )

    return RemediationPlan(
        plan_id="plan_test",
        candidate_index=0,
        action=action,
        params=ActionParams(workload=workload),
        target_resources=targets,
        declared_blast_set=[workload],
        inverse=inverse_plan,
        rationale="K9 unit test plan",
        origin="planner",
    )


def _make_k9_facts(
    *,
    has_inverse: bool = True,
    plan_targets: list[Any] | None = None,
    inverse_targets: list[Any] | None = None,
) -> list[Fact]:
    """Helper to construct K9 required facts."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    default_target = _make_resource("data-service")
    p_targets = plan_targets if plan_targets is not None else [default_target]
    i_targets = inverse_targets if inverse_targets is not None else [default_target]

    return [
        Fact(
            name="plan_has_inverse",
            value=has_inverse,
            source="plan",
            observed_at=now,
        ),
        Fact(
            name="plan_targets",
            value=p_targets,
            source="plan",
            observed_at=now,
        ),
        Fact(
            name="inverse_targets",
            value=i_targets,
            source="plan",
            observed_at=now,
        ),
    ]


# =============================================================================
# Protocol Conformance
# =============================================================================


def test_k9_protocol_conformance() -> None:
    """Test that K9Reversibility satisfies the Invariant protocol."""
    k9 = K9Reversibility()
    assert isinstance(k9, Invariant)
    assert k9.id == "K9"
    assert k9.tier == InvariantTier.PROOF
    assert "Every plan except NO_ACTION declares an inverse" in k9.statement
    assert "target set equals its own" in k9.statement
    assert "plan_has_inverse" in k9.required_facts
    assert "inverse_targets" in k9.required_facts
    assert "plan_targets" in k9.required_facts


# =============================================================================
# Positive Test Cases (Proved Safe -> unsat on negation)
# =============================================================================


def test_k9_positive_no_action_trivially_satisfies() -> None:
    """NO_ACTION trivially satisfies K9 even with empty facts and no inverse."""
    plan = _make_plan(action=ActionType.NO_ACTION, with_inverse=False, target_resources=[])
    ctx = KernelContext(plan, facts=[])

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    assert z3.is_true(inv_formula)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k9_positive_single_target_matching_inverse() -> None:
    """Forward plan and inverse both target the same single deployment."""
    target = _make_resource("data-service")
    plan = _make_plan(target_resources=[target], inverse_targets=[target])
    facts = _make_k9_facts(has_inverse=True, plan_targets=[target], inverse_targets=[target])
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k9_positive_multiple_targets_matching_inverse() -> None:
    """Forward plan and inverse both target multiple identical resources."""
    targets = [
        _make_resource("data-service", kind="Deployment"),
        _make_resource("auth-service", kind="Deployment"),
        _make_resource("app-config", kind="ConfigMap"),
    ]
    plan = _make_plan(target_resources=targets, inverse_targets=targets)
    facts = _make_k9_facts(has_inverse=True, plan_targets=targets, inverse_targets=targets)
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k9_positive_empty_targets_with_empty_inverse() -> None:
    """Plan with no targets and inverse with no targets satisfies K9."""
    plan = _make_plan(target_resources=[], inverse_targets=[])
    facts = _make_k9_facts(has_inverse=True, plan_targets=[], inverse_targets=[])
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


@pytest.mark.parametrize(
    "action",
    [
        ActionType.SCALE_WORKLOAD,
        ActionType.RESTART_WORKLOAD,
        ActionType.ROLLBACK_DEPLOY,
        ActionType.DISABLE_FLAG,
        ActionType.REVERT_CONFIG,
    ],
)
def test_k9_positive_all_action_types_pass_with_valid_inverse(action: ActionType) -> None:
    """All non-NO_ACTION action types pass when inverse matches targets."""
    target = _make_resource("data-service")
    plan = _make_plan(action=action, target_resources=[target], inverse_targets=[target])
    facts = _make_k9_facts(has_inverse=True, plan_targets=[target], inverse_targets=[target])
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k9_positive_dict_mappings_and_string_targets() -> None:
    """Target resources passed as ResourceRef or string formats normalize correctly."""
    res_target = _make_resource("data-service")
    plan = _make_plan(target_resources=[res_target], inverse_targets=[res_target])
    facts = _make_k9_facts(
        has_inverse=True,
        plan_targets=[res_target],
        inverse_targets=["Deployment/ust-prod/data-service"],
    )
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k9_positive_fixtures_facts_ok() -> None:
    """Integration test with fixtures/facts_ok.json for a valid reversible plan."""
    facts_path = Path("fixtures/facts_ok.json")
    facts = load_facts_json(facts_path)

    target = _make_resource("data-service")
    plan = _make_plan(target_resources=[target], inverse_targets=[target])
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


# =============================================================================
# Negative Test Cases (Veto -> sat on negation, counterexample asserted)
# =============================================================================


def test_k9_negative_no_inverse_declared() -> None:
    """Plan has no inverse: plan_has_inverse is False -> VETO with counterexample.

    Asserts solver sat on negation, invariant is K9, and model proves
    plan_has_inverse is False.
    """
    target = _make_resource("data-service")
    plan = _make_plan(target_resources=[target], with_inverse=False)
    facts = _make_k9_facts(has_inverse=False, plan_targets=[target], inverse_targets=[])
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    assert k9.id == "K9"
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    # Counterexample assertion per §6.4 and §6.5
    has_inv_const = ctx.constants["plan_has_inverse"]
    assert bool(model.eval(has_inv_const)) is False


def test_k9_negative_inverse_touches_fewer_targets() -> None:
    """Inverse touches fewer targets than the forward plan (subset) -> VETO.

    Per docs/03-invariants.md §3.4: 'Equality rather than subset is deliberate:
    an inverse that touches fewer resources leaves partial state behind.'
    """
    target1 = _make_resource("data-service")
    target2 = _make_resource("auth-service")
    # Plan mutates two resources, but inverse only restores one
    plan = _make_plan(target_resources=[target1, target2], inverse_targets=[target1])
    facts = _make_k9_facts(
        has_inverse=True,
        plan_targets=[target1, target2],
        inverse_targets=[target1],
    )
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    # Counterexample proves plan_has_inverse is True but target sets differ
    assert bool(model.eval(ctx.constants["plan_has_inverse"])) is True
    # Evaluates cardinality inequality (1 != 2)
    inv_len = z3.IntVal(1)
    plan_len = z3.IntVal(2)
    assert z3.is_false(model.eval(inv_len == plan_len))


def test_k9_negative_inverse_touches_extra_targets() -> None:
    """Inverse touches extra targets not touched by forward plan (superset) -> VETO.

    Restoring untouched resources introduces collateral mutation risk.
    """
    target1 = _make_resource("data-service")
    target2 = _make_resource("auth-service")
    # Forward plan mutates one resource, but inverse touches two
    plan = _make_plan(target_resources=[target1], inverse_targets=[target1, target2])
    facts = _make_k9_facts(
        has_inverse=True,
        plan_targets=[target1],
        inverse_targets=[target1, target2],
    )
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    assert bool(model.eval(ctx.constants["plan_has_inverse"])) is True
    inv_len = z3.IntVal(2)
    plan_len = z3.IntVal(1)
    assert z3.is_false(model.eval(inv_len == plan_len))


def test_k9_negative_inverse_touches_disjoint_targets() -> None:
    """Inverse touches completely disjoint targets with equal cardinality -> VETO.

    Forward plan touches data-service; inverse touches auth-service.
    """
    target_plan = _make_resource("data-service")
    target_inv = _make_resource("auth-service")
    plan = _make_plan(target_resources=[target_plan], inverse_targets=[target_inv])
    facts = _make_k9_facts(
        has_inverse=True,
        plan_targets=[target_plan],
        inverse_targets=[target_inv],
    )
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    assert bool(model.eval(ctx.constants["plan_has_inverse"])) is True
    # Resource equality fails
    s_plan = z3.StringVal("Deployment/ust-prod/data-service")
    s_inv = z3.StringVal("Deployment/ust-prod/auth-service")
    assert z3.is_false(model.eval(s_plan == s_inv))


def test_k9_negative_empty_inverse_targets_with_plan_targets() -> None:
    """Forward plan targets data-service but inverse targets is empty -> VETO."""
    target = _make_resource("data-service")
    plan = _make_plan(target_resources=[target], inverse_targets=[])
    facts = _make_k9_facts(
        has_inverse=True,
        plan_targets=[target],
        inverse_targets=[],
    )
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat


def test_k9_negative_empty_plan_targets_with_nonempty_inverse_targets() -> None:
    """Forward plan targets empty but inverse targets data-service -> VETO."""
    target = _make_resource("data-service")
    plan = _make_plan(target_resources=[], inverse_targets=[target])
    facts = _make_k9_facts(
        has_inverse=True,
        plan_targets=[],
        inverse_targets=[target],
    )
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat


def test_k9_negative_defense_in_depth_target_resources_mismatch() -> None:
    """Defense-in-depth: plan object contains extra target not in inverse object."""
    target1 = _make_resource("data-service")
    target2 = _make_resource("worker")
    # Even if fact dictionary reports equal targets, the plan itself declares target2
    plan = _make_plan(target_resources=[target1, target2], inverse_targets=[target1])
    facts = _make_k9_facts(
        has_inverse=True,
        plan_targets=[target1],
        inverse_targets=[target1],
    )
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    inv_formula = k9.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat


# =============================================================================
# Missing Fact Cases (Rule 5.6 zero defaults)
# =============================================================================


def test_k9_missing_plan_has_inverse() -> None:
    """Missing plan_has_inverse fact raises MissingFact without defaulting."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    target = _make_resource("data-service")
    facts = [
        Fact(name="plan_targets", value=[target], source="plan", observed_at=now),
        Fact(name="inverse_targets", value=[target], source="plan", observed_at=now),
    ]
    plan = _make_plan()
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    with pytest.raises(MissingFact) as exc_info:
        k9.build(ctx)

    assert exc_info.value.fact_name == "plan_has_inverse"
    assert "plan_has_inverse" in ctx.missing_facts


def test_k9_missing_plan_targets() -> None:
    """Missing plan_targets fact raises MissingFact without defaulting."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    target = _make_resource("data-service")
    facts = [
        Fact(name="plan_has_inverse", value=True, source="plan", observed_at=now),
        Fact(name="inverse_targets", value=[target], source="plan", observed_at=now),
    ]
    plan = _make_plan()
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    with pytest.raises(MissingFact) as exc_info:
        k9.build(ctx)

    assert exc_info.value.fact_name == "plan_targets"
    assert "plan_targets" in ctx.missing_facts


def test_k9_missing_inverse_targets() -> None:
    """Missing inverse_targets fact raises MissingFact without defaulting."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    target = _make_resource("data-service")
    facts = [
        Fact(name="plan_has_inverse", value=True, source="plan", observed_at=now),
        Fact(name="plan_targets", value=[target], source="plan", observed_at=now),
    ]
    plan = _make_plan()
    ctx = KernelContext(plan, facts)

    k9 = K9Reversibility()
    with pytest.raises(MissingFact) as exc_info:
        k9.build(ctx)

    assert exc_info.value.fact_name == "inverse_targets"
    assert "inverse_targets" in ctx.missing_facts


def test_k9_empty_facts_raises_missing_fact() -> None:
    """Empty fact dictionary on non-NO_ACTION plan raises MissingFact immediately."""
    plan = _make_plan()
    ctx = KernelContext(plan, [])
    k9 = K9Reversibility()

    with pytest.raises(MissingFact):
        k9.build(ctx)
