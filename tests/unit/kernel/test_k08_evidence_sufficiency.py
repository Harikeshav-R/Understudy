"""Unit tests for Invariant K8 (Evidence sufficiency and freshness) formal proof verification.

Verifies:
- Invariant protocol conformance and metadata.
- Positive cases (proved safe, solver unsat on negation):
  - Standard fresh evidence within all thresholds (age=45.0, count=80, drop=0.01).
  - Exact boundary conditions:
    - evidence_age_seconds == 300.0 (MAX_EVIDENCE_AGE).
    - probe_sample_count == 60 (MIN_PROBE_SAMPLES).
    - max_drop_ratio == 0.05 (MIRROR_DROP_CEILING).
    - All three at exact boundary simultaneously (300.0, 60, 0.05).
  - Extreme positive boundaries:
    - Zero drop ratio (0.0).
    - Zero evidence age (0.0).
    - High sample count (1000).
  - Custom configured thresholds (e.g. max_age=120.0, min_samples=30, drop_ceiling=0.02).
  - All closed ActionType variants evaluated with valid evidence:
    - NO_ACTION
    - ROLLBACK_DEPLOY
    - RESTART_WORKLOAD
    - SCALE_WORKLOAD
    - DISABLE_FLAG
    - REVERT_CONFIG
  - Integration with fixtures/facts_ok.json.
- Negative cases (veto, solver sat on negation, counterexample asserted per §6.4 and §6.5):
  - Stale evidence: evidence_age_seconds = 300.1 and 600.0 (age > 300.0).
  - Insufficient probe samples: probe_sample_count = 59 and 0 (samples < 60).
  - High mirror drop ratio: max_drop_ratio = 0.051 and 0.50 (drop > 0.05).
  - Multiple concurrent violations.
  - Custom configured thresholds negative cases.
  - Each action type vetoed when evidence is stale or insufficient.
- Missing fact cases (zero-default safety per Rule 5.6):
  - Missing evidence_age_seconds raises MissingFact.
  - Missing probe_sample_count raises MissingFact.
  - Missing max_drop_ratio raises MissingFact.
  - Empty facts dictionary raises MissingFact.
  - Missing facts correctly recorded in ctx.missing_facts.
- Type validation:
  - Non-numeric evidence_age_seconds raises TypeError.
  - Non-integer probe_sample_count raises TypeError.
  - Non-numeric max_drop_ratio raises TypeError.
  - Boolean facts raise TypeError.
- Property-based tests using Hypothesis:
  - Property test across valid randomized parameter space (always unsat).
  - Property test across invalid randomized parameter space (always sat).
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import z3
from hypothesis import given
from hypothesis import strategies as st

from understudy.common.errors import MissingFact
from understudy.contracts.enums import ActionType, InvariantTier
from understudy.contracts.kernel import Fact
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.kernel.dsl import Invariant, KernelContext
from understudy.kernel.facts import load_facts_json
from understudy.kernel.invariants.k08_evidence_sufficiency import (
    DEFAULT_MAX_EVIDENCE_AGE,
    DEFAULT_MIN_PROBE_SAMPLES,
    DEFAULT_MIRROR_DROP_CEILING,
    K8EvidenceSufficiency,
)


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
        plan_id="plan_k8_test",
        candidate_index=0,
        action=action,
        params=ActionParams(
            workload=workload,
            replica_delta=1 if action == ActionType.SCALE_WORKLOAD else None,
        ),
        target_resources=targets,
        declared_blast_set=[workload] if workload else [],
        rationale="K8 unit test candidate plan",
        origin="planner",
    )


def _make_facts(
    *,
    evidence_age_seconds: float | int = 45.0,
    probe_sample_count: int = 80,
    max_drop_ratio: float | int = 0.01,
) -> list[Fact]:
    """Helper to create standard K8 facts."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    return [
        Fact(
            name="evidence_age_seconds",
            value=evidence_age_seconds,
            source="tournament",
            observed_at=now,
        ),
        Fact(
            name="probe_sample_count",
            value=probe_sample_count,
            source="tournament",
            observed_at=now,
        ),
        Fact(
            name="max_drop_ratio",
            value=max_drop_ratio,
            source="tournament",
            observed_at=now,
        ),
    ]


# =============================================================================
# Protocol Conformance
# =============================================================================


def test_k8_protocol_conformance() -> None:
    """Test that K8EvidenceSufficiency satisfies the Invariant protocol."""
    k8 = K8EvidenceSufficiency()
    assert isinstance(k8, Invariant)
    assert k8.id == "K8"
    assert k8.tier == InvariantTier.PROOF
    assert "A plan may only be cleared on evidence that is fresh" in k8.statement
    assert "evidence_age_seconds" in k8.required_facts
    assert "probe_sample_count" in k8.required_facts
    assert "max_drop_ratio" in k8.required_facts
    assert k8.max_evidence_age == DEFAULT_MAX_EVIDENCE_AGE
    assert k8.min_probe_samples == DEFAULT_MIN_PROBE_SAMPLES
    assert k8.mirror_drop_ceiling == DEFAULT_MIRROR_DROP_CEILING


# =============================================================================
# Positive Test Cases (Proved Safe -> unsat on negation)
# =============================================================================


def test_k8_positive_standard_evidence() -> None:
    """Plan with fresh, dense, high-fidelity evidence is proved safe."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=45.0, probe_sample_count=80, max_drop_ratio=0.01)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k8_positive_exact_boundary_conditions() -> None:
    """Plan with evidence exactly meeting all thresholds is proved safe."""
    plan = _make_plan()
    facts = _make_facts(
        evidence_age_seconds=DEFAULT_MAX_EVIDENCE_AGE,
        probe_sample_count=DEFAULT_MIN_PROBE_SAMPLES,
        max_drop_ratio=DEFAULT_MIRROR_DROP_CEILING,
    )
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k8_positive_zero_drop_ratio() -> None:
    """Plan with zero mirror drop (perfect fidelity) is proved safe."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=10.0, probe_sample_count=100, max_drop_ratio=0.0)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k8_positive_zero_evidence_age() -> None:
    """Plan with zero evidence age (instantaneous rehearsal) is proved safe."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=0.0, probe_sample_count=60, max_drop_ratio=0.02)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k8_positive_high_sample_count() -> None:
    """Plan with very high sample count is proved safe."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=100.0, probe_sample_count=1000, max_drop_ratio=0.01)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k8_positive_integer_fact_values() -> None:
    """Integer values for float facts are cleanly converted and proved safe."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=50, probe_sample_count=60, max_drop_ratio=0)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k8_positive_custom_configured_thresholds() -> None:
    """Custom thresholds configured on invariant instance are respected."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=100.0, probe_sample_count=35, max_drop_ratio=0.015)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency(
        max_evidence_age=120.0,
        min_probe_samples=30,
        mirror_drop_ceiling=0.02,
    )
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


@pytest.mark.parametrize(
    "action",
    [
        ActionType.NO_ACTION,
        ActionType.ROLLBACK_DEPLOY,
        ActionType.RESTART_WORKLOAD,
        ActionType.SCALE_WORKLOAD,
        ActionType.DISABLE_FLAG,
        ActionType.REVERT_CONFIG,
    ],
)
def test_k8_positive_all_action_types_with_valid_evidence(action: ActionType) -> None:
    """All action types satisfy K8 when backed by valid tournament evidence."""
    plan = _make_plan(
        action=action, workload="data-service" if action != ActionType.NO_ACTION else ""
    )
    facts = _make_facts(evidence_age_seconds=30.0, probe_sample_count=75, max_drop_ratio=0.02)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


def test_k8_positive_facts_ok_fixture() -> None:
    """Verify K8 passes against production facts_ok.json fixture."""
    fixture_path = Path(__file__).parents[3] / "fixtures" / "facts_ok.json"
    facts = load_facts_json(fixture_path)
    plan = _make_plan(action=ActionType.SCALE_WORKLOAD)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


# =============================================================================
# Negative Test Cases (Veto -> sat on negation, counterexample asserted)
# =============================================================================


def test_k8_negative_stale_evidence_boundary() -> None:
    """Evidence age marginally exceeding 300.0s is vetoed with counterexample."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=300.1, probe_sample_count=80, max_drop_ratio=0.01)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    age_val = float(model.eval(ctx.constants["evidence_age_seconds"]).as_decimal(5))
    assert age_val > DEFAULT_MAX_EVIDENCE_AGE


def test_k8_negative_very_stale_evidence() -> None:
    """Very stale evidence (600s) is vetoed with counterexample."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=600.0, probe_sample_count=80, max_drop_ratio=0.01)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    age_val = float(model.eval(ctx.constants["evidence_age_seconds"]).as_decimal(5))
    assert age_val == 600.0
    assert age_val > DEFAULT_MAX_EVIDENCE_AGE


def test_k8_negative_insufficient_samples_boundary() -> None:
    """Sample count marginally below 60 (59) is vetoed with counterexample."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=45.0, probe_sample_count=59, max_drop_ratio=0.01)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    count_val = model.eval(ctx.constants["probe_sample_count"]).as_long()
    assert count_val == 59
    assert count_val < DEFAULT_MIN_PROBE_SAMPLES


def test_k8_negative_zero_samples() -> None:
    """Zero probe samples is vetoed with counterexample."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=45.0, probe_sample_count=0, max_drop_ratio=0.01)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    count_val = model.eval(ctx.constants["probe_sample_count"]).as_long()
    assert count_val == 0
    assert count_val < DEFAULT_MIN_PROBE_SAMPLES


def test_k8_negative_high_drop_ratio_boundary() -> None:
    """Mirror drop ratio marginally exceeding 0.05 (0.051) is vetoed with counterexample."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=45.0, probe_sample_count=80, max_drop_ratio=0.051)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    drop_val = float(model.eval(ctx.constants["max_drop_ratio"]).as_decimal(5))
    assert drop_val > DEFAULT_MIRROR_DROP_CEILING


def test_k8_negative_catastrophic_drop_ratio() -> None:
    """High mirror drop ratio (0.50) is vetoed with counterexample."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=45.0, probe_sample_count=80, max_drop_ratio=0.50)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    drop_val = float(model.eval(ctx.constants["max_drop_ratio"]).as_decimal(5))
    assert drop_val == 0.50
    assert drop_val > DEFAULT_MIRROR_DROP_CEILING


def test_k8_negative_multiple_simultaneous_violations() -> None:
    """Evidence violating all three criteria simultaneously is vetoed."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=500.0, probe_sample_count=10, max_drop_ratio=0.25)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    age_val = float(model.eval(ctx.constants["evidence_age_seconds"]).as_decimal(5))
    count_val = model.eval(ctx.constants["probe_sample_count"]).as_long()
    drop_val = float(model.eval(ctx.constants["max_drop_ratio"]).as_decimal(5))

    assert age_val > DEFAULT_MAX_EVIDENCE_AGE
    assert count_val < DEFAULT_MIN_PROBE_SAMPLES
    assert drop_val > DEFAULT_MIRROR_DROP_CEILING


def test_k8_negative_custom_thresholds_veto() -> None:
    """Custom thresholds cause veto when evidence exceeds custom limits."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=150.0, probe_sample_count=80, max_drop_ratio=0.01)
    ctx = KernelContext(plan, facts)

    # Configured with stricter max_evidence_age=100.0
    k8 = K8EvidenceSufficiency(max_evidence_age=100.0)
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    age_val = float(model.eval(ctx.constants["evidence_age_seconds"]).as_decimal(5))
    assert age_val == 150.0
    assert age_val > 100.0


@pytest.mark.parametrize(
    "action",
    [
        ActionType.NO_ACTION,
        ActionType.ROLLBACK_DEPLOY,
        ActionType.RESTART_WORKLOAD,
        ActionType.SCALE_WORKLOAD,
        ActionType.DISABLE_FLAG,
        ActionType.REVERT_CONFIG,
    ],
)
def test_k8_negative_all_action_types_vetoed_on_stale_evidence(action: ActionType) -> None:
    """All action types are vetoed when evidence is stale."""
    plan = _make_plan(
        action=action, workload="data-service" if action != ActionType.NO_ACTION else ""
    )
    facts = _make_facts(evidence_age_seconds=400.0, probe_sample_count=80, max_drop_ratio=0.01)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
    model = solver.model()
    age_val = float(model.eval(ctx.constants["evidence_age_seconds"]).as_decimal(5))
    assert age_val > DEFAULT_MAX_EVIDENCE_AGE


# =============================================================================
# Missing Fact Test Cases (Zero-default safety per Rule 5.6)
# =============================================================================


def test_k8_missing_evidence_age_raises() -> None:
    """Missing evidence_age_seconds fact raises MissingFact."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="probe_sample_count", value=80, source="tournament", observed_at=now),
        Fact(name="max_drop_ratio", value=0.01, source="tournament", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    with pytest.raises(MissingFact) as exc_info:
        k8.build(ctx)

    assert "evidence_age_seconds" in str(exc_info.value)
    assert "evidence_age_seconds" in ctx.missing_facts


def test_k8_missing_probe_sample_count_raises() -> None:
    """Missing probe_sample_count fact raises MissingFact."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="evidence_age_seconds", value=45.0, source="tournament", observed_at=now),
        Fact(name="max_drop_ratio", value=0.01, source="tournament", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    with pytest.raises(MissingFact) as exc_info:
        k8.build(ctx)

    assert "probe_sample_count" in str(exc_info.value)
    assert "probe_sample_count" in ctx.missing_facts


def test_k8_missing_max_drop_ratio_raises() -> None:
    """Missing max_drop_ratio fact raises MissingFact."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="evidence_age_seconds", value=45.0, source="tournament", observed_at=now),
        Fact(name="probe_sample_count", value=80, source="tournament", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    with pytest.raises(MissingFact) as exc_info:
        k8.build(ctx)

    assert "max_drop_ratio" in str(exc_info.value)
    assert "max_drop_ratio" in ctx.missing_facts


def test_k8_missing_all_facts_raises() -> None:
    """Empty facts list raises MissingFact immediately."""
    plan = _make_plan()
    ctx = KernelContext(plan, [])

    k8 = K8EvidenceSufficiency()
    with pytest.raises(MissingFact) as exc_info:
        k8.build(ctx)

    assert "evidence_age_seconds" in str(exc_info.value)
    assert "evidence_age_seconds" in ctx.missing_facts


# =============================================================================
# Type Validation Tests
# =============================================================================


def test_k8_type_error_evidence_age_not_float() -> None:
    """Non-numeric string for evidence_age_seconds raises TypeError."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="evidence_age_seconds", value="recent", source="tournament", observed_at=now),
        Fact(name="probe_sample_count", value=80, source="tournament", observed_at=now),
        Fact(name="max_drop_ratio", value=0.01, source="tournament", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    with pytest.raises(TypeError, match="Fact 'evidence_age_seconds' expected float/int"):
        k8.build(ctx)


def test_k8_type_error_probe_sample_count_not_int() -> None:
    """Float value for probe_sample_count raises TypeError."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="evidence_age_seconds", value=45.0, source="tournament", observed_at=now),
        Fact(name="probe_sample_count", value=80.5, source="tournament", observed_at=now),
        Fact(name="max_drop_ratio", value=0.01, source="tournament", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    with pytest.raises(TypeError, match="Fact 'probe_sample_count' expected int"):
        k8.build(ctx)


def test_k8_type_error_max_drop_ratio_not_float() -> None:
    """Non-numeric string for max_drop_ratio raises TypeError."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="evidence_age_seconds", value=45.0, source="tournament", observed_at=now),
        Fact(name="probe_sample_count", value=80, source="tournament", observed_at=now),
        Fact(name="max_drop_ratio", value="low", source="tournament", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    with pytest.raises(TypeError, match="Fact 'max_drop_ratio' expected float/int"):
        k8.build(ctx)


def test_k8_type_error_boolean_values_rejected() -> None:
    """Booleans passed for numeric facts are rejected with TypeError."""
    plan = _make_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    facts = [
        Fact(name="evidence_age_seconds", value=True, source="tournament", observed_at=now),
        Fact(name="probe_sample_count", value=80, source="tournament", observed_at=now),
        Fact(name="max_drop_ratio", value=0.01, source="tournament", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    with pytest.raises(TypeError, match="Fact 'evidence_age_seconds' expected float/int"):
        k8.build(ctx)


# =============================================================================
# Property-Based Tests (Hypothesis)
# =============================================================================


@given(
    age=st.floats(min_value=0.0, max_value=300.0, allow_nan=False, allow_infinity=False),
    count=st.integers(min_value=60, max_value=1000),
    drop=st.floats(min_value=0.0, max_value=0.05, allow_nan=False, allow_infinity=False),
)
def test_k8_property_valid_range_always_unsat(age: float, count: int, drop: float) -> None:
    """Any parameter combination within valid thresholds is proved safe (unsat on negation)."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=age, probe_sample_count=count, max_drop_ratio=drop)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.unsat


@given(
    age=st.floats(min_value=300.001, max_value=10000.0, allow_nan=False, allow_infinity=False),
)
def test_k8_property_stale_age_always_sat(age: float) -> None:
    """Any evidence age exceeding 300.0s is always vetoed (sat on negation)."""
    plan = _make_plan()
    facts = _make_facts(evidence_age_seconds=age, probe_sample_count=80, max_drop_ratio=0.01)
    ctx = KernelContext(plan, facts)

    k8 = K8EvidenceSufficiency()
    inv_formula = k8.build(ctx)

    solver = z3.Solver()
    for assertion in ctx.fact_assertions:
        solver.add(assertion)
    solver.add(z3.Not(inv_formula))

    assert solver.check() == z3.sat
