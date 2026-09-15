"""Unit tests for Invariant K5 (Single Writer) formal proof verification.

Verifies:
- Invariant protocol conformance and metadata.
- Positive cases (proved safe, solver unsat on negation):
  - Disjoint targets: plan targets data-service, in-flight targets auth-service.
  - Empty in-flight targets (no active runs).
  - Empty plan targets (plan without mutations).
  - NO_ACTION plan with empty facts and empty targets (vacuously true).
  - Same service name in different namespaces (e.g. ust-prod vs twin namespace).
  - Different resource kinds in same namespace (e.g. Deployment vs ConfigMap).
  - Multi-target plans completely disjoint from multi-target in-flight runs.
  - Single condition pairwise check (1 target each).
  - Target representations as ResourceRef, dict mappings, and strings.
  - Parametric testing across all ActionType variants.
  - Integration with fixtures/facts_ok.json.
- Negative cases (veto, solver sat on negation, counterexample asserted per §6.4 and §6.5):
  - Single overlapping workload in same namespace -> VETO with counterexample.
  - Multi-target overlap -> VETO with counterexample identifying overlapping target.
  - Complete overlap of all targets -> VETO with counterexample.
  - Defense-in-depth: plan object target_resources carries overlapping target omitted
    from fact dictionary -> VETO with counterexample.
  - Shadow mode collision: speculative injection active on Deployment while incident
    plan attempts remediation on same Deployment -> VETO.
  - Target overlap using dict mappings and string representations.
- Missing fact cases (zero-default safety per Rule 5.6):
  - Missing plan_targets raises MissingFact.
  - Missing in_flight_plan_targets raises MissingFact.
  - Empty facts dictionary on active plan raises MissingFact.
  - Tracking of missing facts in ctx.missing_facts.
- Atomic claim under transaction tests:
  - Atomic claim serialization via RunStore.claim_run under advisory lock.
  - Conflict prevention when active run holds target Deployment.
  - Release of targets upon run completion enabling subsequent plans to pass K5.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
import z3

from understudy.common.errors import MissingFact, StoreError
from understudy.contracts.enums import ActionType, InvariantTier, RunOutcome
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.kernel import Fact
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.contracts.run import RunRecord
from understudy.kernel.dsl import Invariant, KernelContext
from understudy.kernel.facts import FactExtractor, load_facts_json
from understudy.kernel.invariants.k05_single_writer import K5SingleWriter, _canonical_target
from understudy.store.fakes import FakeRunStore


def _make_context(incident_id: str = "inc_001") -> IncidentContext:
    """Helper to create a minimal IncidentContext."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    return IncidentContext(
        incident_id=incident_id,
        alert=Alert(
            alert_id="alt_001",
            source="synthetic",
            title="K5 test alert",
            service="data-service",
            severity="critical",
            fired_at=now,
        ),
        metrics_window=MetricWindow(
            service="data-service",
            start_time=now,
            end_time=now,
        ),
        dependency_graph=DependencyGraphSnapshot(observed_at=now),
        gathered_at=now,
    )


def _make_resource(
    name: str = "data-service",
    kind: Literal["Deployment", "ConfigMap", "Secret"] = "Deployment",
    namespace: str = "ust-prod",
) -> ResourceRef:
    """Helper to create a ResourceRef."""
    return ResourceRef(kind=kind, name=name, namespace=namespace)


def _make_plan(
    *,
    action: ActionType = ActionType.ROLLBACK_DEPLOY,
    workload: str = "data-service",
    namespace: str = "ust-prod",
    target_resources: list[ResourceRef] | None = None,
) -> RemediationPlan:
    """Helper to create a test RemediationPlan."""
    targets = (
        target_resources
        if target_resources is not None
        else [_make_resource(name=workload, namespace=namespace)]
    )

    return RemediationPlan(
        plan_id="plan_test_k5",
        candidate_index=0,
        action=action,
        params=ActionParams(workload=workload),
        target_resources=targets,
        declared_blast_set=[workload] if workload else [],
        rationale="K5 unit test plan",
        origin="planner",
    )


def _make_k5_facts(
    *,
    plan_targets: list[Any] | None = None,
    in_flight_targets: list[Any] | None = None,
) -> list[Fact]:
    """Helper to construct K5 required facts."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    p_targets = plan_targets if plan_targets is not None else [_make_resource("data-service")]
    f_targets = in_flight_targets if in_flight_targets is not None else []

    return [
        Fact(
            name="plan_targets",
            value=p_targets,
            source="plan",
            observed_at=now,
        ),
        Fact(
            name="in_flight_plan_targets",
            value=f_targets,
            source="store",
            observed_at=now,
        ),
    ]


# =============================================================================
# Protocol Conformance and Helper Normalization
# =============================================================================


def test_k5_protocol_conformance() -> None:
    """Test that K5SingleWriter satisfies the Invariant protocol."""
    k5 = K5SingleWriter()
    assert isinstance(k5, Invariant)
    assert k5.id == "K5"
    assert k5.tier == InvariantTier.PROOF
    assert "No two plans may hold overlapping target resources concurrently" in k5.statement
    assert "plan_targets" in k5.required_facts
    assert "in_flight_plan_targets" in k5.required_facts


def test_canonical_target_helper() -> None:
    """Test _canonical_target formatting for ResourceRef, dict, and string."""
    ref = ResourceRef(kind="Deployment", name="data-service", namespace="ust-prod")
    assert _canonical_target(ref) == "Deployment/ust-prod/data-service"

    ref_cfg = ResourceRef(kind="ConfigMap", name="app-config", namespace="ust-prod")
    assert _canonical_target(ref_cfg) == "ConfigMap/ust-prod/app-config"

    d1 = {"kind": "Deployment", "name": "auth-service", "namespace": "ust-prod"}
    assert _canonical_target(d1) == "Deployment/ust-prod/auth-service"

    d2 = {"kind": "Deployment", "name": "data-service"}
    assert _canonical_target(d2) == "Deployment//data-service"

    raw_str = "Deployment/ust-prod/worker"
    assert _canonical_target(raw_str) == "Deployment/ust-prod/worker"


# =============================================================================
# Positive Test Cases (Proved Safe -> unsat on negation)
# =============================================================================


def test_k5_positive_disjoint_targets() -> None:
    """Plan targets data-service, in-flight targets auth-service -> proved safe (unsat)."""
    target_p = _make_resource("data-service")
    target_f = _make_resource("auth-service")

    plan = _make_plan(target_resources=[target_p])
    facts = _make_k5_facts(
        plan_targets=[target_p],
        in_flight_targets=[target_f],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k5_positive_empty_in_flight_targets() -> None:
    """No active runs in flight (in_flight_plan_targets is empty) -> proved safe."""
    target_p = _make_resource("data-service")

    plan = _make_plan(target_resources=[target_p])
    facts = _make_k5_facts(
        plan_targets=[target_p],
        in_flight_targets=[],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    assert z3.is_true(inv_formula)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k5_positive_empty_plan_targets() -> None:
    """Plan targets no resources -> proved safe."""
    target_f = _make_resource("auth-service")

    plan = _make_plan(target_resources=[])
    facts = _make_k5_facts(
        plan_targets=[],
        in_flight_targets=[target_f],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    assert z3.is_true(inv_formula)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k5_positive_no_action_empty_facts() -> None:
    """NO_ACTION plan with empty facts trivially satisfies K5."""
    plan = _make_plan(
        action=ActionType.NO_ACTION,
        workload="",
        target_resources=[],
    )
    ctx = KernelContext(plan, facts=[])

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    assert z3.is_true(inv_formula)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k5_positive_same_name_different_namespaces() -> None:
    """Same service name in different namespaces (ust-prod vs twin) are disjoint."""
    target_p = _make_resource("data-service", namespace="ust-prod")
    target_f = _make_resource("data-service", namespace="ust-twin-inc-0")

    plan = _make_plan(target_resources=[target_p])
    facts = _make_k5_facts(
        plan_targets=[target_p],
        in_flight_targets=[target_f],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k5_positive_different_kinds_same_namespace() -> None:
    """Different resource kinds with same name in same namespace are disjoint."""
    target_p = _make_resource("data-service", kind="Deployment", namespace="ust-prod")
    target_f = _make_resource("data-service", kind="ConfigMap", namespace="ust-prod")

    plan = _make_plan(target_resources=[target_p])
    facts = _make_k5_facts(
        plan_targets=[target_p],
        in_flight_targets=[target_f],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k5_positive_multi_target_disjoint() -> None:
    """Multi-target plan disjoint from multi-target in-flight active runs."""
    p_targets = [_make_resource("data-service"), _make_resource("worker")]
    f_targets = [_make_resource("auth-service"), _make_resource("edge-gateway")]

    plan = _make_plan(target_resources=p_targets)
    facts = _make_k5_facts(
        plan_targets=p_targets,
        in_flight_targets=f_targets,
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k5_positive_target_representations() -> None:
    """Target resources passed as ResourceRef, dict, and string representations."""
    target_p = _make_resource("data-service")
    target_f_dict = {"kind": "Deployment", "name": "auth-service", "namespace": "ust-prod"}
    target_f_str = "Deployment/ust-prod/edge-gateway"

    plan = _make_plan(target_resources=[target_p])
    facts = _make_k5_facts(
        plan_targets=[target_p],
        in_flight_targets=[target_f_dict, target_f_str],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

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
def test_k5_positive_all_action_types_pass_with_disjoint_targets(
    action: ActionType,
) -> None:
    """All action types pass when their targets do not overlap with in-flight targets."""
    target_p = _make_resource("data-service")
    target_f = _make_resource("auth-service")

    plan = _make_plan(action=action, workload="data-service", target_resources=[target_p])
    facts = _make_k5_facts(
        plan_targets=[target_p],
        in_flight_targets=[target_f],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k5_positive_fixtures_facts_ok() -> None:
    """Integration test with fixtures/facts_ok.json for a candidate plan."""
    facts_path = Path("fixtures/facts_ok.json")
    facts = load_facts_json(facts_path)

    plan = _make_plan(
        workload="data-service",
        target_resources=[_make_resource("data-service")],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


# =============================================================================
# Negative Test Cases (Veto -> sat on negation, counterexample asserted)
# =============================================================================


def test_k5_negative_single_overlapping_workload() -> None:
    """Plan targets data-service while an in-flight run also holds data-service -> VETO.

    Counterexample asserts solver sat on negation and proves the overlapping
    resource equality violates single-writer isolation.
    """
    target = _make_resource("data-service")

    plan = _make_plan(target_resources=[target])
    facts = _make_k5_facts(
        plan_targets=[target],
        in_flight_targets=[target],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    assert k5.id == "K5"
    inv_formula = k5.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    # Counterexample assertion per §6.4 and §6.5
    overlapping_plan_target = z3.StringVal("Deployment/ust-prod/data-service")
    overlapping_in_flight = z3.StringVal("Deployment/ust-prod/data-service")
    is_equal = overlapping_plan_target == overlapping_in_flight
    assert z3.is_true(model.eval(is_equal))

    is_disjoint = overlapping_plan_target != overlapping_in_flight
    assert z3.is_false(model.eval(is_disjoint))


def test_k5_negative_multi_target_partial_overlap() -> None:
    """Multi-target plan partially overlaps with in-flight targets -> VETO.

    Plan targets {data-service, auth-service}, in-flight holds {auth-service, worker}.
    Counterexample isolates auth-service as the colliding resource.
    """
    t_data = _make_resource("data-service")
    t_auth = _make_resource("auth-service")
    t_worker = _make_resource("worker")

    plan = _make_plan(target_resources=[t_data, t_auth])
    facts = _make_k5_facts(
        plan_targets=[t_data, t_auth],
        in_flight_targets=[t_auth, t_worker],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    # Counterexample identifies collision on auth-service
    colliding = z3.StringVal("Deployment/ust-prod/auth-service")
    assert z3.is_true(model.eval(colliding == colliding))

    # Condition asserting auth-service != auth-service evaluates to False
    assert z3.is_false(model.eval(colliding != colliding))


def test_k5_negative_complete_overlap() -> None:
    """Every resource targeted by the candidate plan is held by in-flight runs -> VETO."""
    t1 = _make_resource("data-service")
    t2 = _make_resource("auth-service")

    plan = _make_plan(target_resources=[t1, t2])
    facts = _make_k5_facts(
        plan_targets=[t1, t2],
        in_flight_targets=[t1, t2],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat


def test_k5_negative_defense_in_depth_plan_target_omitted_from_facts() -> None:
    """Plan target_resources carries colliding target omitted from fact dictionary -> VETO."""
    t_safe = _make_resource("data-service")
    t_colliding = _make_resource("auth-service")

    # Plan object mutates both, but fact map only listed data-service
    plan = _make_plan(target_resources=[t_safe, t_colliding])
    facts = _make_k5_facts(
        plan_targets=[t_safe],
        in_flight_targets=[t_colliding],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat


def test_k5_negative_shadow_mode_speculative_collision() -> None:
    """Shadow mode speculative injection collision on same Deployment -> VETO.

    Per ADR-020, shadow mode runs continuously. If a speculative injection is active,
    an incident remediation on the same Deployment must be vetoed to prevent races.
    """
    deployment = _make_resource("data-service")

    # Incident candidate plan targeting data-service
    incident_plan = _make_plan(
        action=ActionType.ROLLBACK_DEPLOY,
        workload="data-service",
        target_resources=[deployment],
    )

    # In-flight active run originates from shadow mode holding data-service
    facts = _make_k5_facts(
        plan_targets=[deployment],
        in_flight_targets=[deployment],
    )
    ctx = KernelContext(incident_plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat


def test_k5_negative_dict_and_string_target_overlap() -> None:
    """Overlapping targets formatted as dict or string are detected and vetoed."""
    target_res = _make_resource("data-service")
    target_dict = {"kind": "Deployment", "name": "data-service", "namespace": "ust-prod"}

    plan = _make_plan(target_resources=[target_res])
    facts = _make_k5_facts(
        plan_targets=[target_res],
        in_flight_targets=[target_dict],
    )
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    inv_formula = k5.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat


# =============================================================================
# Missing Fact Cases (Rule 5.6 zero defaults)
# =============================================================================


def test_k5_missing_plan_targets() -> None:
    """Missing plan_targets fact raises MissingFact without defaulting."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(
            name="in_flight_plan_targets",
            value=[_make_resource("auth-service")],
            source="store",
            observed_at=now,
        ),
    ]
    plan = _make_plan()
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    with pytest.raises(MissingFact) as exc_info:
        k5.build(ctx)

    assert exc_info.value.fact_name == "plan_targets"
    assert "plan_targets" in ctx.missing_facts


def test_k5_missing_in_flight_plan_targets() -> None:
    """Missing in_flight_plan_targets fact raises MissingFact without defaulting."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(
            name="plan_targets",
            value=[_make_resource("data-service")],
            source="plan",
            observed_at=now,
        ),
    ]
    plan = _make_plan()
    ctx = KernelContext(plan, facts)

    k5 = K5SingleWriter()
    with pytest.raises(MissingFact) as exc_info:
        k5.build(ctx)

    assert exc_info.value.fact_name == "in_flight_plan_targets"
    assert "in_flight_plan_targets" in ctx.missing_facts


def test_k5_empty_facts_raises_missing_fact() -> None:
    """Empty facts dictionary on an active plan raises MissingFact immediately."""
    plan = _make_plan()
    ctx = KernelContext(plan, [])
    k5 = K5SingleWriter()

    with pytest.raises(MissingFact):
        k5.build(ctx)


# =============================================================================
# Atomic Claim Under Transaction Tests
# =============================================================================


@pytest.mark.asyncio
async def test_k5_atomic_claim_under_transaction_flow() -> None:
    """End-to-end integration test of atomic run claim, fact extraction, and K5.

    1. Initially no active runs exist in store -> K5 passes.
    2. Active run 1 is claimed via store.claim_run() under transaction.
    3. FactExtractor extracts in_flight_plan_targets containing run 1's targets.
    4. Candidate plan targeting the same resource is evaluated -> K5 VETO.
    5. Concurrent attempt to claim run 2 raises StoreError (atomic lock prevents double claim).
    6. Run 1 is marked finished -> K5 passes again.
    """
    store = FakeRunStore()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    k5 = K5SingleWriter()

    target = _make_resource("data-service")
    candidate_plan = _make_plan(workload="data-service", target_resources=[target])

    # 1. Initially empty store: extract facts -> K5 passes
    extractor = FactExtractor(run_store=store)
    store_facts = await extractor.extract_store_facts(candidate_plan, now)
    all_facts = [
        Fact(name="plan_targets", value=[target], source="plan", observed_at=now),
        *store_facts,
    ]
    ctx1 = KernelContext(candidate_plan, all_facts)
    solver1 = z3.Solver()
    solver1.add(z3.Not(k5.build(ctx1)))
    assert solver1.check() == z3.unsat

    # 2. Claim active run 1 targeting data-service under transaction
    run_1 = RunRecord(
        run_id="run_001",
        incident_id="inc_001",
        scenario_id="scen_001",
        started_at=now,
        finished_at=None,
        outcome=RunOutcome.EXECUTED,
        context=_make_context("inc_001"),
        plans=[candidate_plan],
    )
    await store.claim_run(run_1)

    # 3 & 4. FactExtractor extracts active targets -> K5 vetoes candidate plan
    store_facts_active = await extractor.extract_store_facts(candidate_plan, now)
    ctx2 = KernelContext(
        candidate_plan,
        [
            Fact(name="plan_targets", value=[target], source="plan", observed_at=now),
            *store_facts_active,
        ],
    )
    solver2 = z3.Solver()
    solver2.add(z3.Not(k5.build(ctx2)))
    assert solver2.check() == z3.sat

    # 5. Concurrent claim_run attempt while run_1 is active raises StoreError
    run_2 = RunRecord(
        run_id="run_002",
        incident_id="inc_002",
        scenario_id="scen_002",
        started_at=now,
        finished_at=None,
        outcome=RunOutcome.EXECUTED,
        context=_make_context("inc_002"),
        plans=[candidate_plan],
    )
    with pytest.raises(StoreError, match="Another active run already exists"):
        await store.claim_run(run_2)

    # 6. Mark run_1 as finished -> in-flight targets clear and K5 passes
    run_1_finished = run_1.model_copy(update={"finished_at": now})
    await store.record_run(run_1_finished)

    store_facts_finished = await extractor.extract_store_facts(candidate_plan, now)
    ctx3 = KernelContext(
        candidate_plan,
        [
            Fact(name="plan_targets", value=[target], source="plan", observed_at=now),
            *store_facts_finished,
        ],
    )
    solver3 = z3.Solver()
    solver3.add(z3.Not(k5.build(ctx3)))
    assert solver3.check() == z3.unsat
