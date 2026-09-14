"""Unit tests for Invariant K3 (Migration boundary) formal proof verification.

Verifies:
- Invariant protocol conformance and metadata.
- Positive cases (proved safe, solver unsat on negation):
  - Rollback target is newer than latest migration (target_time > migration_time)
    and does not contain migration.
  - Rollback target timestamp equals migration timestamp with target_contains_migration=False.
  - Non-rollback action types (SCALE_WORKLOAD, RESTART_WORKLOAD, NO_ACTION,
    DISABLE_FLAG, REVERT_CONFIG) trivially satisfy K3 without requiring rollback facts.
  - Integration with fixtures/facts_ok.json.
- Negative cases (veto, solver sat on negation, counterexample asserted per §6.4 and §6.5):
  - Rollback target predates the latest migration (target_time < migration_time).
  - Rollback target is the migration commit itself (target_contains_migration=True).
  - Rollback target is newer than migration but introduces a migration
    (target_contains_migration=True).
  - Rollback target predates migration AND contains a migration.
- Missing fact cases (zero-default safety per Rule 5.6):
  - Missing last_migration_commit_time raises MissingFact.
  - Missing rollback_target_commit_time raises MissingFact.
  - Missing target_contains_migration raises MissingFact.
  - Integration with fixtures/facts_missing_migration.json.
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
from understudy.kernel.invariants.k03_migration_boundary import K3MigrationBoundary


def _make_rollback_plan(
    *,
    workload: str = "data-service",
    target_commit: str = "c0ffee123456",
) -> RemediationPlan:
    """Helper to create a ROLLBACK_DEPLOY RemediationPlan."""
    return RemediationPlan(
        plan_id="plan_rollback_k3_test",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload=workload,
            target_commit=target_commit,
        ),
        target_resources=[
            ResourceRef(
                kind="Deployment",
                name=workload,
                namespace="ust-prod",
            )
        ],
        declared_blast_set=[workload],
        rationale="K3 unit test rollback plan",
        origin="planner",
    )


def _make_non_rollback_plan(
    action: ActionType = ActionType.SCALE_WORKLOAD,
    workload: str = "data-service",
) -> RemediationPlan:
    """Helper to create a non-rollback RemediationPlan."""
    return RemediationPlan(
        plan_id="plan_non_rollback_k3_test",
        candidate_index=1,
        action=action,
        params=ActionParams(
            workload=workload,
            replica_delta=1 if action == ActionType.SCALE_WORKLOAD else None,
        ),
        target_resources=[
            ResourceRef(
                kind="Deployment",
                name=workload,
                namespace="ust-prod",
            )
        ],
        declared_blast_set=[workload],
        rationale="K3 non-rollback test plan",
        origin="planner",
    )


def _make_k3_facts(
    *,
    last_migration_time: datetime,
    rollback_target_time: datetime,
    target_contains_migration: bool = False,
) -> list[Fact]:
    """Helper to construct K3 required facts."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    return [
        Fact(
            name="last_migration_commit_time",
            value=last_migration_time,
            source="github",
            observed_at=now,
        ),
        Fact(
            name="rollback_target_commit_time",
            value=rollback_target_time,
            source="github",
            observed_at=now,
        ),
        Fact(
            name="target_contains_migration",
            value=target_contains_migration,
            source="github",
            observed_at=now,
        ),
    ]


# =============================================================================
# Protocol Conformance
# =============================================================================


def test_k3_protocol_conformance() -> None:
    """Test that K3MigrationBoundary satisfies the Invariant protocol."""
    k3 = K3MigrationBoundary()
    assert isinstance(k3, Invariant)
    assert k3.id == "K3"
    assert k3.tier == InvariantTier.PROOF
    assert "A ROLLBACK_DEPLOY may not target a commit" in k3.statement
    assert "predates the most recent schema migration" in k3.statement
    assert "last_migration_commit_time" in k3.required_facts
    assert "rollback_target_commit_time" in k3.required_facts
    assert "target_contains_migration" in k3.required_facts


# =============================================================================
# Positive Test Cases (Proved Safe -> unsat on negation)
# =============================================================================


def test_k3_positive_rollback_newer_than_migration() -> None:
    """Target commit is newer than the migration and contains no migration -> SAFE."""
    t_migration = datetime(2026, 9, 13, 10, 0, 0, tzinfo=UTC)
    t_target = datetime(2026, 9, 13, 11, 0, 0, tzinfo=UTC)

    plan = _make_rollback_plan()
    facts = _make_k3_facts(
        last_migration_time=t_migration,
        rollback_target_time=t_target,
        target_contains_migration=False,
    )
    ctx = KernelContext(plan, facts)

    k3 = K3MigrationBoundary()
    formula = k3.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(formula))

    assert solver.check() == z3.unsat


def test_k3_positive_rollback_equal_timestamp_no_migration() -> None:
    """Target commit timestamp equals migration timestamp and contains no migration -> SAFE."""
    t_migration = datetime(2026, 9, 13, 10, 0, 0, tzinfo=UTC)
    t_target = datetime(2026, 9, 13, 10, 0, 0, tzinfo=UTC)

    plan = _make_rollback_plan()
    facts = _make_k3_facts(
        last_migration_time=t_migration,
        rollback_target_time=t_target,
        target_contains_migration=False,
    )
    ctx = KernelContext(plan, facts)

    k3 = K3MigrationBoundary()
    formula = k3.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(formula))

    assert solver.check() == z3.unsat


@pytest.mark.parametrize(
    "action",
    [
        ActionType.SCALE_WORKLOAD,
        ActionType.RESTART_WORKLOAD,
        ActionType.NO_ACTION,
        ActionType.DISABLE_FLAG,
        ActionType.REVERT_CONFIG,
    ],
)
def test_k3_positive_non_rollback_actions_trivially_satisfy(action: ActionType) -> None:
    """Non-rollback plans trivially satisfy K3 even when facts are empty."""
    plan = _make_non_rollback_plan(action=action)
    ctx = KernelContext(plan, facts=[])

    k3 = K3MigrationBoundary()
    formula = k3.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(formula))

    assert solver.check() == z3.unsat


def test_k3_positive_fixtures_facts_ok() -> None:
    """Integration test with fixtures/facts_ok.json for a safe rollback plan."""
    facts_path = Path("fixtures/facts_ok.json")
    facts = load_facts_json(facts_path)

    plan = _make_rollback_plan()
    ctx = KernelContext(plan, facts)

    k3 = K3MigrationBoundary()
    formula = k3.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(formula))

    assert solver.check() == z3.unsat


# =============================================================================
# Negative Test Cases (Veto -> sat on negation, counterexample asserted)
# =============================================================================


def test_k3_negative_target_predates_migration() -> None:
    """Negative: Target commit predates the most recent schema migration.

    Asserts solver sat on negation, invariant is K3, and counterexample proves
    rollback_target_commit_time < last_migration_commit_time.
    """
    t_migration = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    t_target = datetime(2026, 9, 13, 10, 0, 0, tzinfo=UTC)

    plan = _make_rollback_plan()
    facts = _make_k3_facts(
        last_migration_time=t_migration,
        rollback_target_time=t_target,
        target_contains_migration=False,
    )
    ctx = KernelContext(plan, facts)

    k3 = K3MigrationBoundary()
    assert k3.id == "K3"
    formula = k3.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(formula))

    assert solver.check() == z3.sat
    model = solver.model()

    # Verify counterexample values in model
    target_const = ctx.constants["rollback_target_commit_time"]
    mig_const = ctx.constants["last_migration_commit_time"]
    target_val = float(model.eval(target_const).as_decimal(6).rstrip("?"))
    mig_val = float(model.eval(mig_const).as_decimal(6).rstrip("?"))

    assert target_val == pytest.approx(t_target.timestamp())
    assert mig_val == pytest.approx(t_migration.timestamp())
    assert target_val < mig_val


def test_k3_negative_target_is_migration_itself() -> None:
    """Negative: Target commit is the migration commit itself (target_contains_migration=True).

    Asserts solver sat on negation and counterexample proves target_contains_migration is True.
    """
    t_migration = datetime(2026, 9, 13, 10, 0, 0, tzinfo=UTC)
    t_target = datetime(2026, 9, 13, 10, 0, 0, tzinfo=UTC)

    plan = _make_rollback_plan()
    facts = _make_k3_facts(
        last_migration_time=t_migration,
        rollback_target_time=t_target,
        target_contains_migration=True,
    )
    ctx = KernelContext(plan, facts)

    k3 = K3MigrationBoundary()
    formula = k3.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(formula))

    assert solver.check() == z3.sat
    model = solver.model()

    mig_flag_const = ctx.constants["target_contains_migration"]
    assert bool(model.eval(mig_flag_const)) is True


def test_k3_negative_target_newer_but_contains_migration() -> None:
    """Negative: Target commit is newer than prior migration but itself contains a migration."""
    t_migration = datetime(2026, 9, 13, 10, 0, 0, tzinfo=UTC)
    t_target = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

    plan = _make_rollback_plan()
    facts = _make_k3_facts(
        last_migration_time=t_migration,
        rollback_target_time=t_target,
        target_contains_migration=True,
    )
    ctx = KernelContext(plan, facts)

    k3 = K3MigrationBoundary()
    formula = k3.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(formula))

    assert solver.check() == z3.sat
    model = solver.model()

    mig_flag_const = ctx.constants["target_contains_migration"]
    assert bool(model.eval(mig_flag_const)) is True


def test_k3_negative_predates_and_contains_migration() -> None:
    """Negative: Target commit predates migration AND contains a migration."""
    t_migration = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    t_target = datetime(2026, 9, 13, 9, 0, 0, tzinfo=UTC)

    plan = _make_rollback_plan()
    facts = _make_k3_facts(
        last_migration_time=t_migration,
        rollback_target_time=t_target,
        target_contains_migration=True,
    )
    ctx = KernelContext(plan, facts)

    k3 = K3MigrationBoundary()
    formula = k3.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(formula))

    assert solver.check() == z3.sat
    model = solver.model()

    target_const = ctx.constants["rollback_target_commit_time"]
    mig_const = ctx.constants["last_migration_commit_time"]
    mig_flag_const = ctx.constants["target_contains_migration"]

    target_val = float(model.eval(target_const).as_decimal(6).rstrip("?"))
    mig_val = float(model.eval(mig_const).as_decimal(6).rstrip("?"))

    assert target_val < mig_val
    assert bool(model.eval(mig_flag_const)) is True


# =============================================================================
# Missing Fact Cases (Rule 5.6 zero defaults)
# =============================================================================


def test_k3_missing_last_migration_commit_time() -> None:
    """MissingFact raised when last_migration_commit_time is absent."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(
            name="rollback_target_commit_time",
            value=now,
            source="github",
            observed_at=now,
        ),
        Fact(
            name="target_contains_migration",
            value=False,
            source="github",
            observed_at=now,
        ),
    ]
    plan = _make_rollback_plan()
    ctx = KernelContext(plan, facts)

    k3 = K3MigrationBoundary()
    with pytest.raises(MissingFact) as exc_info:
        k3.build(ctx)

    assert exc_info.value.fact_name == "last_migration_commit_time"
    assert "last_migration_commit_time" in exc_info.value.missing_facts
    assert "last_migration_commit_time" in ctx.missing_facts


def test_k3_missing_rollback_target_commit_time() -> None:
    """MissingFact raised when rollback_target_commit_time is absent."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(
            name="last_migration_commit_time",
            value=now,
            source="github",
            observed_at=now,
        ),
        Fact(
            name="target_contains_migration",
            value=False,
            source="github",
            observed_at=now,
        ),
    ]
    plan = _make_rollback_plan()
    ctx = KernelContext(plan, facts)

    k3 = K3MigrationBoundary()
    with pytest.raises(MissingFact) as exc_info:
        k3.build(ctx)

    assert exc_info.value.fact_name == "rollback_target_commit_time"
    assert "rollback_target_commit_time" in exc_info.value.missing_facts
    assert "rollback_target_commit_time" in ctx.missing_facts


def test_k3_missing_target_contains_migration() -> None:
    """MissingFact raised when target_contains_migration is absent."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(
            name="last_migration_commit_time",
            value=now,
            source="github",
            observed_at=now,
        ),
        Fact(
            name="rollback_target_commit_time",
            value=now,
            source="github",
            observed_at=now,
        ),
    ]
    plan = _make_rollback_plan()
    ctx = KernelContext(plan, facts)

    k3 = K3MigrationBoundary()
    with pytest.raises(MissingFact) as exc_info:
        k3.build(ctx)

    assert exc_info.value.fact_name == "target_contains_migration"
    assert "target_contains_migration" in exc_info.value.missing_facts
    assert "target_contains_migration" in ctx.missing_facts


def test_k3_fixtures_facts_missing_migration() -> None:
    """Integration test with fixtures/facts_missing_migration.json.

    fixtures/facts_missing_migration.json omits last_migration_commit_time,
    which must raise MissingFact('last_migration_commit_time').
    """
    facts_path = Path("fixtures/facts_missing_migration.json")
    facts = load_facts_json(facts_path)

    plan = _make_rollback_plan()
    ctx = KernelContext(plan, facts)

    k3 = K3MigrationBoundary()
    with pytest.raises(MissingFact) as exc_info:
        k3.build(ctx)

    assert exc_info.value.fact_name == "last_migration_commit_time"
    assert "last_migration_commit_time" in exc_info.value.missing_facts
    assert "last_migration_commit_time" in ctx.missing_facts
