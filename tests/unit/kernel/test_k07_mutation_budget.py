"""Unit tests for Invariant K7 (Production mutation budget) formal proof verification.

Verifies:
- Invariant protocol conformance and metadata.
- Positive cases (proved safe, solver unsat on negation):
  - Zero mutations in window within budget (0 + 1 <= 3).
  - Mid-window mutations within budget (1 + 1 <= 3).
  - Exact budget boundary (2 + 1 <= 3).
  - Custom configured budget (4 + 1 <= 5).
  - All mutating action types evaluated within budget:
    - ROLLBACK_DEPLOY
    - RESTART_WORKLOAD
    - SCALE_WORKLOAD
    - DISABLE_FLAG
    - REVERT_CONFIG
  - Non-mutating NO_ACTION trivially satisfies K7:
    - When budget is available.
    - When budget is already exhausted (3 mutations in window).
    - When mutations exceed budget (5 mutations in window).
    - When facts are completely omitted from context.
  - Integration with fixtures/facts_ok.json.
- Negative cases (veto, solver sat on negation, counterexample asserted per §6.4 and §6.5):
  - Budget exhausted: 3 mutations in window with budget 3 (3 + 1 > 3).
  - Budget exceeded: 4 mutations in window with budget 3 (4 + 1 > 3).
  - Zero budget: 0 mutations in window with budget 0 (0 + 1 > 0).
  - Each mutating action type vetoed when budget is exhausted.
- Missing fact cases (zero-default safety per Rule 5.6):
  - Missing prod_mutations_in_window raises MissingFact.
  - Missing mutation_budget raises MissingFact.
  - Empty facts dictionary raises MissingFact.
- Type validation:
  - Non-integer prod_mutations_in_window raises TypeError.
  - Non-integer mutation_budget raises TypeError.
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
from understudy.kernel.invariants.k07_mutation_budget import K7MutationBudget


def _make_plan(
    *,
    action: ActionType = ActionType.SCALE_WORKLOAD,
    workload: str = "data-service",
) -> RemediationPlan:
    """Helper to create a test RemediationPlan."""
    targets = (
        [
            ResourceRef(
                kind="Deployment",
                name=workload,
                namespace="ust-prod",
            )
        ]
        if workload
        else []
    )
    return RemediationPlan(
        plan_id="plan_k7_test",
        candidate_index=0,
        action=action,
        params=ActionParams(
            workload=workload,
            replica_delta=1 if action == ActionType.SCALE_WORKLOAD else None,
        ),
        target_resources=targets,
        declared_blast_set=[workload] if workload else [],
        rationale="K7 unit test candidate plan",
        origin="planner",
    )


def _make_facts(
    *,
    prod_mutations_in_window: int = 0,
    mutation_budget: int = 3,
) -> list[Fact]:
    """Helper to create standard K7 facts."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    return [
        Fact(
            name="prod_mutations_in_window",
            value=prod_mutations_in_window,
            source="store",
            observed_at=now,
        ),
        Fact(
            name="mutation_budget",
            value=mutation_budget,
            source="config",
            observed_at=now,
        ),
    ]


def test_k7_protocol_conformance() -> None:
    """Test that K7MutationBudget satisfies the Invariant protocol."""
    k7 = K7MutationBudget()
    assert isinstance(k7, Invariant)
    assert k7.id == "K7"
    assert k7.tier == InvariantTier.PROOF
    assert "At most `mutation_budget` (default 3) production mutations" in k7.statement
    assert "prod_mutations_in_window" in k7.required_facts
    assert "mutation_budget" in k7.required_facts


# =============================================================================
# Positive Test Cases (Proved Safe -> unsat on negation)
# =============================================================================


def test_k7_positive_zero_mutations() -> None:
    """Plan with 0 past mutations within budget 3 is proved safe."""
    plan = _make_plan()
    facts = _make_facts(prod_mutations_in_window=0, mutation_budget=3)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k7_positive_mid_budget() -> None:
    """Plan with 1 past mutation within budget 3 is proved safe."""
    plan = _make_plan()
    facts = _make_facts(prod_mutations_in_window=1, mutation_budget=3)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k7_positive_boundary_condition() -> None:
    """Plan with 2 past mutations within budget 3 is proved safe (2 + 1 == 3)."""
    plan = _make_plan()
    facts = _make_facts(prod_mutations_in_window=2, mutation_budget=3)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k7_positive_custom_budget() -> None:
    """Plan with 4 past mutations within custom budget 5 is proved safe."""
    plan = _make_plan()
    facts = _make_facts(prod_mutations_in_window=4, mutation_budget=5)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


@pytest.mark.parametrize(
    "action",
    [
        ActionType.ROLLBACK_DEPLOY,
        ActionType.RESTART_WORKLOAD,
        ActionType.SCALE_WORKLOAD,
        ActionType.DISABLE_FLAG,
        ActionType.REVERT_CONFIG,
    ],
)
def test_k7_positive_all_mutating_actions_within_budget(action: ActionType) -> None:
    """All mutating action types satisfy K7 when within budget."""
    plan = _make_plan(action=action)
    facts = _make_facts(prod_mutations_in_window=1, mutation_budget=3)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k7_positive_no_action_within_budget() -> None:
    """NO_ACTION plan is trivially safe and does not consume mutation budget."""
    plan = _make_plan(action=ActionType.NO_ACTION, workload="")
    facts = _make_facts(prod_mutations_in_window=0, mutation_budget=3)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k7_positive_no_action_when_budget_exhausted() -> None:
    """NO_ACTION plan satisfies K7 even when production mutation budget is exhausted."""
    plan = _make_plan(action=ActionType.NO_ACTION, workload="")
    facts = _make_facts(prod_mutations_in_window=3, mutation_budget=3)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k7_positive_no_action_when_budget_exceeded() -> None:
    """NO_ACTION plan satisfies K7 even when production mutation count exceeds budget."""
    plan = _make_plan(action=ActionType.NO_ACTION, workload="")
    facts = _make_facts(prod_mutations_in_window=10, mutation_budget=3)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k7_positive_no_action_without_facts() -> None:
    """NO_ACTION plan trivially satisfies K7 without requiring any mutation facts."""
    plan = _make_plan(action=ActionType.NO_ACTION, workload="")
    ctx = KernelContext(plan, [])

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k7_positive_facts_ok_fixture() -> None:
    """Verify K7 against production facts_ok.json fixture."""
    fixture_path = Path(__file__).parents[3] / "fixtures" / "facts_ok.json"
    facts = load_facts_json(fixture_path)
    plan = _make_plan(action=ActionType.SCALE_WORKLOAD)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


# =============================================================================
# Negative Test Cases (Veto -> sat on negation, counterexample asserted)
# =============================================================================


def test_k7_negative_budget_exhausted() -> None:
    """Plan when 3 mutations already occurred in window is vetoed (3 + 1 > 3)."""
    plan = _make_plan()
    facts = _make_facts(prod_mutations_in_window=3, mutation_budget=3)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    mut_const = ctx.constants["prod_mutations_in_window"]
    bud_const = ctx.constants["mutation_budget"]
    mut_val = model.eval(mut_const).as_long()
    bud_val = model.eval(bud_const).as_long()

    assert mut_val == 3
    assert bud_val == 3
    assert mut_val + 1 > bud_val  # Exact counterexample


def test_k7_negative_budget_exceeded() -> None:
    """Plan when 4 mutations already occurred in window is vetoed (4 + 1 > 3)."""
    plan = _make_plan()
    facts = _make_facts(prod_mutations_in_window=4, mutation_budget=3)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    mut_const = ctx.constants["prod_mutations_in_window"]
    bud_const = ctx.constants["mutation_budget"]
    mut_val = model.eval(mut_const).as_long()
    bud_val = model.eval(bud_const).as_long()

    assert mut_val == 4
    assert bud_val == 3
    assert mut_val + 1 > bud_val


def test_k7_negative_zero_budget() -> None:
    """Plan when mutation budget is set to 0 is vetoed (0 + 1 > 0)."""
    plan = _make_plan()
    facts = _make_facts(prod_mutations_in_window=0, mutation_budget=0)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    mut_const = ctx.constants["prod_mutations_in_window"]
    bud_const = ctx.constants["mutation_budget"]
    mut_val = model.eval(mut_const).as_long()
    bud_val = model.eval(bud_const).as_long()

    assert mut_val == 0
    assert bud_val == 0
    assert mut_val + 1 > bud_val


@pytest.mark.parametrize(
    "action",
    [
        ActionType.ROLLBACK_DEPLOY,
        ActionType.RESTART_WORKLOAD,
        ActionType.SCALE_WORKLOAD,
        ActionType.DISABLE_FLAG,
        ActionType.REVERT_CONFIG,
    ],
)
def test_k7_negative_all_mutating_actions_vetoed_on_budget_exhaustion(action: ActionType) -> None:
    """All mutating action types are vetoed when mutation budget is exhausted."""
    plan = _make_plan(action=action)
    facts = _make_facts(prod_mutations_in_window=3, mutation_budget=3)
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    inv_formula = k7.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    mut_val = model.eval(ctx.constants["prod_mutations_in_window"]).as_long()
    bud_val = model.eval(ctx.constants["mutation_budget"]).as_long()
    assert mut_val + 1 > bud_val


# =============================================================================
# Missing Fact Test Cases (Zero-default safety per Rule 5.6)
# =============================================================================


def test_k7_missing_prod_mutations_raises() -> None:
    """Missing prod_mutations_in_window fact raises MissingFact."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="mutation_budget", value=3, source="config", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    with pytest.raises(MissingFact) as exc_info:
        k7.build(ctx)

    assert "prod_mutations_in_window" in str(exc_info.value)
    assert "prod_mutations_in_window" in ctx.missing_facts


def test_k7_missing_mutation_budget_raises() -> None:
    """Missing mutation_budget fact raises MissingFact."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="prod_mutations_in_window", value=0, source="store", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    with pytest.raises(MissingFact) as exc_info:
        k7.build(ctx)

    assert "mutation_budget" in str(exc_info.value)
    assert "mutation_budget" in ctx.missing_facts


def test_k7_missing_all_facts_raises() -> None:
    """Empty facts list for a mutating plan raises MissingFact."""
    plan = _make_plan()
    ctx = KernelContext(plan, [])

    k7 = K7MutationBudget()
    with pytest.raises(MissingFact) as exc_info:
        k7.build(ctx)

    assert "prod_mutations_in_window" in str(exc_info.value)
    assert "prod_mutations_in_window" in ctx.missing_facts


# =============================================================================
# Type Validation Tests
# =============================================================================


def test_k7_type_error_prod_mutations_not_int() -> None:
    """Non-integer prod_mutations_in_window fact value raises TypeError."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="prod_mutations_in_window", value="one", source="store", observed_at=now),
        Fact(name="mutation_budget", value=3, source="config", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    with pytest.raises(TypeError, match="Fact 'prod_mutations_in_window' expected int"):
        k7.build(ctx)


def test_k7_type_error_mutation_budget_not_int() -> None:
    """Non-integer mutation_budget fact value raises TypeError."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="prod_mutations_in_window", value=0, source="store", observed_at=now),
        Fact(name="mutation_budget", value="three", source="config", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    k7 = K7MutationBudget()
    with pytest.raises(TypeError, match="Fact 'mutation_budget' expected int"):
        k7.build(ctx)
