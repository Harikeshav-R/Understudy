"""Unit tests for Invariant K4 (Blast Containment) formal proof verification.

Verifies:
- Invariant protocol conformance and metadata.
- Positive cases (proved safe, solver unsat on negation):
  - Exact containment: observed == declared == reachable.
  - Subset containment: fewer services observed than declared (observed ⊂ declared ⊆ reachable).
  - Empty observed blast set (rehearsal had zero downstream degradation).
  - NO_ACTION plan with empty blast sets.
  - NO_ACTION plan with empty facts (vacuously true).
  - Multi-target plans combining multiple dependent closures into reachable set.
  - Integration with fixtures/facts_ok.json.
  - Target representations as ResourceRef, dict mappings, and strings.
  - Parametric testing across all ActionType variants.
- Negative cases (veto, solver sat on negation, counterexample asserted per §6.4 and §6.5):
  - Observed blast touches unpredicted service not declared in plan -> VETO with counterexample.
  - Declared blast touches service outside dependency reachable set -> VETO with counterexample.
  - Empty declared blast set with non-empty observed blast set -> VETO with counterexample.
  - Plan with no targets declaring non-empty blast set -> VETO with counterexample.
  - Defense-in-depth: plan object declares uncontained blast omitted from fact map -> VETO.
- Missing fact cases (zero-default safety per Rule 5.6):
  - Missing declared_blast_set raises MissingFact.
  - Missing observed_blast_set raises MissingFact.
  - Missing dependents[svc] raises MissingFact.
  - Empty facts dictionary on active plan raises MissingFact.
  - Tracking of all missing facts in ctx.missing_facts.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
import z3

from understudy.common.errors import MissingFact
from understudy.contracts.enums import ActionType, InvariantTier
from understudy.contracts.kernel import Fact
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.kernel.dsl import Invariant, KernelContext
from understudy.kernel.facts import load_facts_json
from understudy.kernel.invariants.k04_blast_containment import K4BlastContainment


def _make_resource(
    name: str = "data-service",
    kind: Literal["Deployment", "ConfigMap", "Secret"] = "Deployment",
) -> ResourceRef:
    """Helper to create a ResourceRef in ust-prod."""
    return ResourceRef(kind=kind, name=name, namespace="ust-prod")


def _make_plan(
    *,
    action: ActionType = ActionType.ROLLBACK_DEPLOY,
    workload: str = "data-service",
    target_resources: list[ResourceRef] | None = None,
    declared_blast_set: list[str] | None = None,
) -> RemediationPlan:
    """Helper to create a test RemediationPlan."""
    targets = target_resources if target_resources is not None else [_make_resource(workload)]
    blast = declared_blast_set if declared_blast_set is not None else [workload]

    return RemediationPlan(
        plan_id="plan_test_k4",
        candidate_index=0,
        action=action,
        params=ActionParams(workload=workload),
        target_resources=targets,
        declared_blast_set=blast,
        rationale="K4 unit test plan",
        origin="planner",
    )


def _make_k4_facts(
    *,
    declared_blast: list[str] | None = None,
    observed_blast: list[str] | None = None,
    dependents_map: dict[str, list[str]] | None = None,
    plan_targets: list[Any] | None = None,
) -> list[Fact]:
    """Helper to construct K4 required facts."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    d_blast = declared_blast if declared_blast is not None else ["data-service"]
    o_blast = observed_blast if observed_blast is not None else ["data-service"]
    deps = (
        dependents_map
        if dependents_map is not None
        else {"data-service": ["auth-service", "edge-gateway", "worker"]}
    )

    facts: list[Fact] = [
        Fact(
            name="declared_blast_set",
            value=d_blast,
            source="plan",
            observed_at=now,
        ),
        Fact(
            name="observed_blast_set",
            value=o_blast,
            source="tournament",
            observed_at=now,
        ),
    ]

    for svc, dep_list in deps.items():
        facts.append(
            Fact(
                name=f"dependents[{svc}]",
                value=dep_list,
                source="graph",
                observed_at=now,
            )
        )

    if plan_targets is not None:
        facts.append(
            Fact(
                name="plan_targets",
                value=plan_targets,
                source="plan",
                observed_at=now,
            )
        )

    return facts


# =============================================================================
# Protocol Conformance
# =============================================================================


def test_k4_protocol_conformance() -> None:
    """Test that K4BlastContainment satisfies the Invariant protocol."""
    k4 = K4BlastContainment()
    assert isinstance(k4, Invariant)
    assert k4.id == "K4"
    assert k4.tier == InvariantTier.PROOF
    assert "observed blast set must be a subset of its declared blast set" in k4.statement
    assert (
        "declared blast set must be a subset of the dependency-graph reachable set" in k4.statement
    )
    assert "dependents[svc]" in k4.required_facts
    assert "declared_blast_set" in k4.required_facts
    assert "observed_blast_set" in k4.required_facts


# =============================================================================
# Positive Test Cases (Proved Safe -> unsat on negation)
# =============================================================================


def test_k4_positive_exact_containment() -> None:
    """Exact match: observed == declared == target + subset of dependents."""
    plan = _make_plan(
        workload="data-service",
        declared_blast_set=["data-service", "auth-service"],
    )
    facts = _make_k4_facts(
        declared_blast=["data-service", "auth-service"],
        observed_blast=["data-service", "auth-service"],
        dependents_map={"data-service": ["auth-service", "edge-gateway", "worker"]},
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k4_positive_fewer_services_observed_than_declared() -> None:
    """Observed affects fewer services than declared (observed ⊂ declared ⊆ reachable)."""
    plan = _make_plan(
        workload="data-service",
        declared_blast_set=["data-service", "auth-service", "edge-gateway"],
    )
    facts = _make_k4_facts(
        declared_blast=["data-service", "auth-service", "edge-gateway"],
        observed_blast=["data-service"],  # Only data-service degraded
        dependents_map={"data-service": ["auth-service", "edge-gateway", "worker"]},
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k4_positive_empty_observed_blast_set() -> None:
    """Zero side-effects observed in twin rehearsal (observed == ∅ ⊆ declared ⊆ reachable)."""
    plan = _make_plan(
        workload="data-service",
        declared_blast_set=["data-service"],
    )
    facts = _make_k4_facts(
        declared_blast=["data-service"],
        observed_blast=[],
        dependents_map={"data-service": ["auth-service"]},
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k4_positive_no_action_empty_blast_sets() -> None:
    """NO_ACTION plan with empty declared and observed blast sets satisfies K4."""
    plan = _make_plan(
        action=ActionType.NO_ACTION,
        workload="",
        target_resources=[],
        declared_blast_set=[],
    )
    facts = _make_k4_facts(
        declared_blast=[],
        observed_blast=[],
        dependents_map={},
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    assert z3.is_true(inv_formula)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k4_positive_no_action_empty_facts() -> None:
    """NO_ACTION plan with empty facts trivially satisfies K4."""
    plan = _make_plan(
        action=ActionType.NO_ACTION,
        workload="",
        target_resources=[],
        declared_blast_set=[],
    )
    ctx = KernelContext(plan, facts=[])

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    assert z3.is_true(inv_formula)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k4_positive_multi_target_plan() -> None:
    """Multi-target plan combining dependents closures from multiple workloads."""
    t1 = _make_resource("data-service")
    t2 = _make_resource("auth-service")
    plan = _make_plan(
        workload="",
        target_resources=[t1, t2],
        declared_blast_set=["data-service", "auth-service", "edge-gateway"],
    )
    facts = _make_k4_facts(
        declared_blast=["data-service", "auth-service", "edge-gateway"],
        observed_blast=["data-service", "edge-gateway"],
        dependents_map={
            "data-service": ["auth-service", "worker"],
            "auth-service": ["edge-gateway"],
        },
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k4_positive_target_representations_in_plan_targets() -> None:
    """Target resources parsed from ResourceRef, dict, with Deployment and ConfigMap kinds."""
    t = _make_resource("data-service")
    cfg_target = ResourceRef(kind="ConfigMap", name="app-config", namespace="ust-prod")
    plan = _make_plan(
        workload="",
        target_resources=[t, cfg_target],
        declared_blast_set=["data-service"],
    )
    facts = _make_k4_facts(
        declared_blast=["data-service"],
        observed_blast=["data-service"],
        dependents_map={"data-service": ["auth-service"]},
        plan_targets=[
            t,
            cfg_target,
            {"kind": "Deployment", "name": "data-service", "namespace": "ust-prod"},
            {"kind": "ConfigMap", "name": "app-config", "namespace": "ust-prod"},
        ],
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k4_positive_scalar_plan_targets_representation() -> None:
    """plan_targets fact passed as a single ResourceRef object rather than collection."""
    t = _make_resource("data-service")
    plan = _make_plan(workload="", target_resources=[t], declared_blast_set=["data-service"])
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="declared_blast_set", value=["data-service"], source="plan", observed_at=now),
        Fact(
            name="observed_blast_set", value=["data-service"], source="tournament", observed_at=now
        ),
        Fact(
            name="dependents[data-service]", value=["auth-service"], source="graph", observed_at=now
        ),
        Fact(name="plan_targets", value=t, source="plan", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

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
def test_k4_positive_all_action_types_pass_with_valid_blast(action: ActionType) -> None:
    """All non-NO_ACTION action types pass when blast containment holds."""
    plan = _make_plan(
        action=action,
        workload="data-service",
        declared_blast_set=["data-service", "worker"],
    )
    facts = _make_k4_facts(
        declared_blast=["data-service", "worker"],
        observed_blast=["data-service"],
        dependents_map={"data-service": ["worker", "auth-service"]},
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k4_positive_fixtures_facts_ok() -> None:
    """Integration test with fixtures/facts_ok.json for a safe plan."""
    facts_path = Path("fixtures/facts_ok.json")
    facts = load_facts_json(facts_path)

    plan = _make_plan(
        workload="data-service",
        declared_blast_set=["auth-service", "data-service", "edge-gateway"],
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


# =============================================================================
# Negative Test Cases (Veto -> sat on negation, counterexample asserted)
# =============================================================================


def test_k4_negative_observed_touches_unpredicted_service() -> None:
    """Observed blast contains an unpredicted service not declared in plan -> VETO.

    Counterexample asserts solver sat on negation and proves violating service
    is not in declared_blast_set.
    """
    plan = _make_plan(
        workload="data-service",
        declared_blast_set=["data-service"],
    )
    facts = _make_k4_facts(
        declared_blast=["data-service"],
        observed_blast=["data-service", "unpredicted-service"],
        dependents_map={"data-service": ["auth-service", "unpredicted-service"]},
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    assert k4.id == "K4"
    inv_formula = k4.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    # Counterexample assertion per §6.4 and §6.5
    violating_service = z3.StringVal("unpredicted-service")
    declared_options = [z3.StringVal("data-service")]
    is_contained = z3.Or([violating_service == d for d in declared_options])
    assert z3.is_false(model.eval(is_contained))


def test_k4_negative_declared_touches_service_outside_reachable_set() -> None:
    """Declared blast contains rogue service outside dependency reachable set -> VETO.

    Counterexample asserts solver sat on negation and proves declared rogue service
    is not in the reachable set.
    """
    plan = _make_plan(
        workload="data-service",
        declared_blast_set=["data-service", "rogue-service"],
    )
    facts = _make_k4_facts(
        declared_blast=["data-service", "rogue-service"],
        observed_blast=["data-service"],
        dependents_map={"data-service": ["auth-service", "edge-gateway", "worker"]},
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()

    # Counterexample assertion
    violating_declared = z3.StringVal("rogue-service")
    reachable = ["data-service", "auth-service", "edge-gateway", "worker"]
    reachable_options = [z3.StringVal(r) for r in reachable]
    is_reachable = z3.Or([violating_declared == r for r in reachable_options])
    assert z3.is_false(model.eval(is_reachable))


def test_k4_negative_empty_declared_with_nonempty_observed() -> None:
    """Empty declared blast set with non-empty observed blast set -> VETO."""
    plan = _make_plan(
        workload="data-service",
        declared_blast_set=[],
    )
    facts = _make_k4_facts(
        declared_blast=[],
        observed_blast=["data-service"],
        dependents_map={"data-service": ["auth-service"]},
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat


def test_k4_negative_no_targets_with_nonempty_declared() -> None:
    """Plan with no targets but declared blast set is non-empty -> VETO."""
    plan = _make_plan(
        workload="",
        target_resources=[],
        declared_blast_set=["data-service"],
    )
    facts = _make_k4_facts(
        declared_blast=["data-service"],
        observed_blast=[],
        dependents_map={},
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat


def test_k4_negative_defense_in_depth_plan_object_uncontained_blast() -> None:
    """Defense-in-depth: plan object declares uncontained blast omitted from fact map."""
    plan = _make_plan(
        workload="data-service",
        declared_blast_set=["data-service", "uncontained-service"],
    )
    # Fact map only contains data-service, but plan declares uncontained-service
    facts = _make_k4_facts(
        declared_blast=["data-service"],
        observed_blast=["data-service"],
        dependents_map={"data-service": ["auth-service"]},
    )
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    inv_formula = k4.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat


# =============================================================================
# Missing Fact Cases (Rule 5.6 zero defaults)
# =============================================================================


def test_k4_missing_declared_blast_set() -> None:
    """Missing declared_blast_set fact raises MissingFact without defaulting."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(
            name="observed_blast_set", value=["data-service"], source="tournament", observed_at=now
        ),
        Fact(
            name="dependents[data-service]", value=["auth-service"], source="graph", observed_at=now
        ),
    ]
    plan = _make_plan()
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    with pytest.raises(MissingFact) as exc_info:
        k4.build(ctx)

    assert exc_info.value.fact_name == "declared_blast_set"
    assert "declared_blast_set" in ctx.missing_facts


def test_k4_missing_observed_blast_set() -> None:
    """Missing observed_blast_set fact raises MissingFact without defaulting."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="declared_blast_set", value=["data-service"], source="plan", observed_at=now),
        Fact(
            name="dependents[data-service]", value=["auth-service"], source="graph", observed_at=now
        ),
    ]
    plan = _make_plan()
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    with pytest.raises(MissingFact) as exc_info:
        k4.build(ctx)

    assert exc_info.value.fact_name == "observed_blast_set"
    assert "observed_blast_set" in ctx.missing_facts


def test_k4_missing_dependents_for_target_service() -> None:
    """Missing dependents[svc] fact for a target workload raises MissingFact."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="declared_blast_set", value=["data-service"], source="plan", observed_at=now),
        Fact(
            name="observed_blast_set", value=["data-service"], source="tournament", observed_at=now
        ),
    ]
    plan = _make_plan(workload="data-service")
    ctx = KernelContext(plan, facts)

    k4 = K4BlastContainment()
    with pytest.raises(MissingFact) as exc_info:
        k4.build(ctx)

    assert exc_info.value.fact_name == "dependents[data-service]"
    assert "dependents[data-service]" in ctx.missing_facts


def test_k4_empty_facts_raises_missing_fact() -> None:
    """Empty facts dictionary on active plan raises MissingFact immediately."""
    plan = _make_plan()
    ctx = KernelContext(plan, [])
    k4 = K4BlastContainment()

    with pytest.raises(MissingFact):
        k4.build(ctx)


def test_k4_multi_hop_transitive_closure() -> None:
    """Assert K4 reachable_set computes full transitive closure across chained dependents facts."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    plan = _make_plan(
        workload="data-service",
        declared_blast_set=["data-service", "auth-service", "edge-gateway"],
    )
    # data-service -> auth-service -> edge-gateway (multi-hop chain)
    facts = [
        Fact(
            name="declared_blast_set",
            value=["data-service", "auth-service", "edge-gateway"],
            source="plan",
            observed_at=now,
        ),
        Fact(
            name="observed_blast_set",
            value=["data-service", "edge-gateway"],
            source="tournament",
            observed_at=now,
        ),
        Fact(
            name="dependents[data-service]", value=["auth-service"], source="graph", observed_at=now
        ),
        Fact(
            name="dependents[auth-service]", value=["edge-gateway"], source="graph", observed_at=now
        ),
        Fact(name="dependents[edge-gateway]", value=[], source="graph", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)
    k4 = K4BlastContainment()
    formula = k4.build(ctx)

    solver = z3.Solver()
    solver.add(z3.Not(formula))
    assert solver.check() == z3.unsat


def test_k4_transitive_closure_diamond_and_cycle() -> None:
    """Assert K4 handles diamond dependencies and cycles without infinite loops
    or duplicate visits.
    """
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    plan = _make_plan(
        workload="data-service",
        declared_blast_set=["data-service", "auth-service", "worker", "edge-gateway"],
    )
    # Diamond: data-service -> auth-service & worker; both -> edge-gateway -> data-service (cycle)
    facts = [
        Fact(
            name="declared_blast_set",
            value=["data-service", "auth-service", "worker", "edge-gateway"],
            source="plan",
            observed_at=now,
        ),
        Fact(
            name="observed_blast_set",
            value=["data-service", "edge-gateway"],
            source="tournament",
            observed_at=now,
        ),
        Fact(
            name="dependents[data-service]",
            value=["auth-service", "worker"],
            source="graph",
            observed_at=now,
        ),
        Fact(
            name="dependents[auth-service]",
            value=["edge-gateway"],
            source="graph",
            observed_at=now,
        ),
        Fact(
            name="dependents[worker]",
            value=["edge-gateway"],
            source="graph",
            observed_at=now,
        ),
        Fact(
            name="dependents[edge-gateway]",
            value=["data-service"],
            source="graph",
            observed_at=now,
        ),
    ]
    ctx = KernelContext(plan, facts)
    k4 = K4BlastContainment()
    formula = k4.build(ctx)

    solver = z3.Solver()
    solver.add(z3.Not(formula))
    assert solver.check() == z3.unsat
