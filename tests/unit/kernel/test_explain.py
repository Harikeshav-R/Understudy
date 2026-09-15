"""Unit tests for safety kernel veto explanation and actionable prose rendering.

Implements and verifies build-plan step B4.6:
- Structured VetoExplanation models for all 8 formal PROOF invariants (K1-K9) and custom fallbacks.
- Concrete counterexample extraction from Z3 solver models and context facts.
- Actionable operator guidance for on-call responders.
- CLI commands: 'ust kernel explain' (prose, JSON, PASS, VETO, UNCERTAIN, error branches)
  and 'ust kernel verify --explain'.
- 100% line and branch coverage of src/understudy/kernel/explain.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import z3
from typer.testing import CliRunner

from understudy.cli import app
from understudy.contracts.enums import ActionType, InvariantTier, KernelVerdictType
from understudy.contracts.kernel import Fact
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.kernel.api import (
    VetoExplanation,
    explain_veto,
    format_actionable_prose,
    load_facts_json,
    render_veto_reason,
)
from understudy.kernel.dsl import Invariant, KernelContext
from understudy.kernel.explain import _safe_eval_model

if TYPE_CHECKING:
    import pytest
from understudy.kernel.invariants import (
    K1ReplicaFloor,
    K2NamespaceScope,
    K3MigrationBoundary,
    K4BlastContainment,
    K5SingleWriter,
    K7MutationBudget,
    K8EvidenceSufficiency,
    K9Reversibility,
)

runner = CliRunner()


def _make_plan(
    *,
    plan_id: str = "plan_test_explain",
    action: ActionType = ActionType.ROLLBACK_DEPLOY,
    workload: str = "data-service",
    target_commit: str | None = "c0ffee_safe",
    replica_delta: int | None = None,
    target_resources: list[ResourceRef] | None = None,
    declared_blast_set: list[str] | None = None,
    with_inverse: bool = True,
    namespace: str = "ust-prod",
) -> RemediationPlan:
    """Helper to create test RemediationPlan candidate."""
    targets = target_resources or [
        ResourceRef(kind="Deployment", name=workload, namespace=namespace)
    ]
    blast = declared_blast_set or ["auth-service", "data-service", "edge-gateway"]
    inv: RemediationPlan | None = None
    if with_inverse and action != ActionType.NO_ACTION:
        inv = RemediationPlan(
            plan_id=f"inv_{plan_id}",
            candidate_index=0,
            action=action,
            params=ActionParams(workload=workload, target_commit="c0ffee_pre"),
            target_resources=targets,
            declared_blast_set=blast,
            inverse=None,
            rationale="Inverse plan",
            origin="planner",
        )

    return RemediationPlan(
        plan_id=plan_id,
        candidate_index=0,
        action=action,
        params=ActionParams(
            workload=workload,
            target_commit=target_commit,
            replica_delta=replica_delta,
        ),
        target_resources=targets,
        declared_blast_set=blast,
        inverse=inv,
        rationale="Unit test plan",
        origin="planner",
    )


def test_safe_eval_model_branches() -> None:
    """Verify _safe_eval_model helper branches."""
    # 1. model is None or constant is None
    assert _safe_eval_model(None, None, 42) == 42

    const = z3.Int("test_val")
    assert _safe_eval_model(None, const, 10) == 10

    # 2. Model with integer
    solver = z3.Solver()
    solver.add(const == 7)
    assert solver.check() == z3.sat
    model = solver.model()
    assert _safe_eval_model(model, const, 0) == 7

    # 3. Model with real/decimal
    real_const = z3.Real("real_val")
    solver2 = z3.Solver()
    solver2.add(real_const == z3.RealVal("3.14"))
    assert solver2.check() == z3.sat
    model2 = solver2.model()
    assert abs(_safe_eval_model(model2, real_const, 0.0) - 3.14) < 1e-4

    # 4. Exception or unsupported evaluation
    mock_model = MagicMock()
    mock_model.eval.side_effect = RuntimeError("z3 evaluation failed")
    assert _safe_eval_model(mock_model, const, 99) == 99

    # 5. Model evaluates to AST without as_decimal or as_long (e.g. Bool)
    bool_const = z3.Bool("bool_val")
    solver3 = z3.Solver()
    solver3.add(bool_const)
    assert solver3.check() == z3.sat
    model3 = solver3.model()
    assert _safe_eval_model(model3, bool_const, "fallback") == "fallback"


def test_explain_veto_k1_scale_to_zero_with_model() -> None:
    """Verify K1 explanation extracts model counterexample and provides guidance."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    plan = _make_plan(
        action=ActionType.SCALE_WORKLOAD,
        replica_delta=-2,
    )
    ctx = KernelContext(plan, facts)
    k1 = K1ReplicaFloor()
    formula = k1.build(ctx)

    solver = z3.Solver()
    for a in ctx.fact_assertions:
        solver.add(a)
    solver.add(z3.Not(formula))
    assert solver.check() == z3.sat
    model = solver.model()

    explanation = explain_veto(k1, plan, ctx, model)
    assert isinstance(explanation, VetoExplanation)
    assert explanation.invariant_id == "K1"
    assert explanation.invariant_name == "Replica floor"
    assert "scaling/restarting 'data-service'" in explanation.human_reason
    assert "below minimum floor (1)" in explanation.human_reason
    assert "data-service" in explanation.counterexample
    assert explanation.counterexample["data-service"]["post_healthy"] == 0
    assert "min_replicas[data-service]" in explanation.failing_facts
    assert "Do not down-scale or rolling-restart" in explanation.guidance
    assert "Actionable Operator Guidance:" in explanation.actionable_prose


def test_explain_veto_k1_restart_without_model() -> None:
    """Verify K1 explanation operates cleanly without solver model (model=None)."""
    facts = [
        Fact(
            name="min_replicas[data-service]",
            value=2,
            source="config",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="replicas[data-service]",
            value=2,
            source="k8s",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="healthy_replicas[data-service]",
            value=1,
            source="k8s",
            observed_at=datetime.now(UTC),
        ),
    ]
    plan = _make_plan(action=ActionType.RESTART_WORKLOAD)
    ctx = KernelContext(plan, facts)
    k1 = K1ReplicaFloor()

    explanation = explain_veto(k1, plan, ctx, model=None)
    assert explanation.invariant_id == "K1"
    assert explanation.counterexample["data-service"]["delta"] == -1
    assert explanation.counterexample["data-service"]["post_healthy"] == 0


def test_explain_veto_k2_unauthorized_namespace() -> None:
    """Verify K2 explanation detects rogue target namespaces and specifies authorized."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    plan = _make_plan(
        namespace="rogue-namespace",
        target_resources=[
            ResourceRef(kind="Deployment", name="data-service", namespace="rogue-namespace")
        ],
    )
    ctx = KernelContext(plan, facts)
    k2 = K2NamespaceScope()

    explanation = explain_veto(k2, plan, ctx)
    assert explanation.invariant_id == "K2"
    assert "rogue-namespace" in explanation.human_reason
    assert explanation.counterexample["unauthorized_namespaces"] == ["rogue-namespace"]
    assert explanation.counterexample["authorized_namespace"] == "ust-prod"
    assert "strictly confined to 'ust-prod'" in explanation.guidance


def test_explain_veto_k3_rollback_predates_migration() -> None:
    """Verify K3 explanation names migration commit and rollback target commit time."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    plan_text = Path("fixtures/plan_rollback_across_migration.json").read_text(encoding="utf-8")
    plan = RemediationPlan.model_validate_json(plan_text)
    ctx = KernelContext(plan, facts)
    k3 = K3MigrationBoundary()

    explanation = explain_veto(k3, plan, ctx)
    assert explanation.invariant_id == "K3"
    assert "c0ffee_across_migration" in explanation.human_reason
    assert "af9de754" in explanation.human_reason
    assert "2026-09-13T09:00:00" in explanation.human_reason
    assert explanation.counterexample["rollback_target_commit"] == "c0ffee_across_migration"
    assert "af9de754" in explanation.counterexample["last_migration_commit"]
    assert "incompatible with the current database schema" in explanation.guidance


def test_explain_veto_k3_target_contains_migration() -> None:
    """Verify K3 explanation when target commit itself contains schema migration."""
    facts = [
        Fact(
            name="last_migration_commit_time",
            value=datetime(2026, 9, 13, 10, 0, 0, tzinfo=UTC),
            source="github",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="target_contains_migration",
            value=True,
            source="github",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="rollback_target_commit_time",
            value=datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC),
            source="github",
            observed_at=datetime.now(UTC),
        ),
    ]
    plan = _make_plan(target_commit="c0ffee_mig")
    ctx = KernelContext(plan, facts)
    k3 = K3MigrationBoundary()

    explanation = explain_veto(k3, plan, ctx)
    assert "contains a schema migration" in explanation.human_reason
    assert explanation.counterexample["target_contains_migration"] is True
    assert "Rolling back directly to a migration commit" in explanation.guidance


def test_explain_veto_k4_unpredicted_and_exceeds_dependents() -> None:
    """Verify K4 explanation for unpredicted observed services and graph exceedance."""
    k4 = K4BlastContainment()

    # 1. Unpredicted observed services
    facts_unpred = [
        Fact(
            name="declared_blast_set",
            value=["data-service"],
            source="plan",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="observed_blast_set",
            value=["data-service", "surprise-service"],
            source="tournament",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="dependents[data-service]",
            value=["surprise-service"],
            source="graph",
            observed_at=datetime.now(UTC),
        ),
    ]
    plan = _make_plan()
    ctx1 = KernelContext(plan, facts_unpred)
    exp1 = explain_veto(k4, plan, ctx1)
    assert "surprise-service" in exp1.human_reason
    assert exp1.counterexample["unpredicted_observed_services"] == ["surprise-service"]
    assert "unanticipated dependencies" in exp1.guidance

    # 2. Declared blast exceeds reachable dependents
    facts_exceed = [
        Fact(
            name="declared_blast_set",
            value=["data-service", "unreachable-service"],
            source="plan",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="observed_blast_set",
            value=["data-service"],
            source="tournament",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="dependents[data-service]",
            value=[],
            source="graph",
            observed_at=datetime.now(UTC),
        ),
    ]
    ctx2 = KernelContext(plan, facts_exceed)
    exp2 = explain_veto(k4, plan, ctx2)
    assert "exceeds reachable dependency graph dependents" in exp2.human_reason
    assert "dependencies.yaml" in exp2.guidance


def test_explain_veto_k5_in_flight_collision() -> None:
    """Verify K5 explanation identifies colliding in-flight targets."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    facts = [f for f in facts if f.name != "in_flight_plan_targets"]
    facts.append(
        Fact(
            name="in_flight_plan_targets",
            value=[{"kind": "Deployment", "name": "data-service", "namespace": "ust-prod"}],
            source="store",
            observed_at=datetime.now(UTC),
        )
    )
    plan = _make_plan()
    ctx = KernelContext(plan, facts)
    k5 = K5SingleWriter()

    explanation = explain_veto(k5, plan, ctx)
    assert explanation.invariant_id == "K5"
    assert "Deployment/ust-prod/data-service" in explanation.human_reason
    assert "Deployment/ust-prod/data-service" in explanation.counterexample["colliding_resources"]
    assert "currently locked by an active remediation" in explanation.guidance


def test_explain_veto_k7_mutation_budget_with_model() -> None:
    """Verify K7 explanation calculates mutation budget exceedance."""
    facts = [
        Fact(
            name="prod_mutations_in_window",
            value=3,
            source="store",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="mutation_budget",
            value=3,
            source="config",
            observed_at=datetime.now(UTC),
        ),
    ]
    plan = _make_plan()
    ctx = KernelContext(plan, facts)
    k7 = K7MutationBudget()
    formula = k7.build(ctx)

    solver = z3.Solver()
    for a in ctx.fact_assertions:
        solver.add(a)
    solver.add(z3.Not(formula))
    assert solver.check() == z3.sat
    model = solver.model()

    explanation = explain_veto(k7, plan, ctx, model)
    assert explanation.invariant_id == "K7"
    assert "exceeds mutation budget (3)" in explanation.human_reason
    assert explanation.counterexample["projected_mutations"] == 4
    assert "halted to prevent remediation thrashing" in explanation.guidance


def test_explain_veto_k8_evidence_sufficiency_multiple_issues() -> None:
    """Verify K8 explanation disaggregates age, sample count, and drop ratio."""
    facts = [
        Fact(
            name="evidence_age_seconds",
            value=450.0,
            source="tournament",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="probe_sample_count",
            value=20,
            source="tournament",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="max_drop_ratio",
            value=0.120,
            source="tournament",
            observed_at=datetime.now(UTC),
        ),
    ]
    plan = _make_plan()
    ctx = KernelContext(plan, facts)
    k8 = K8EvidenceSufficiency()

    explanation = explain_veto(k8, plan, ctx)
    assert explanation.invariant_id == "K8"
    assert "evidence age 450.0s exceeds 300s limit" in explanation.human_reason
    assert "probe samples (20) below 60 minimum" in explanation.human_reason
    assert "max drop ratio (0.120) exceeds 0.05 ceiling" in explanation.human_reason
    assert explanation.counterexample["evidence_age_seconds"] == 450.0
    assert explanation.counterexample["probe_sample_count"] == 20
    assert explanation.counterexample["max_drop_ratio"] == 0.120


def test_explain_veto_k9_reversibility() -> None:
    """Verify K9 explanation detects missing inverse or asymmetrical target set."""
    facts = [
        Fact(
            name="plan_has_inverse",
            value=False,
            source="plan",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="inverse_targets",
            value=[],
            source="plan",
            observed_at=datetime.now(UTC),
        ),
        Fact(
            name="plan_targets",
            value=[{"kind": "Deployment", "name": "data-service", "namespace": "ust-prod"}],
            source="plan",
            observed_at=datetime.now(UTC),
        ),
    ]
    plan = _make_plan(with_inverse=False)
    ctx = KernelContext(plan, facts)
    k9 = K9Reversibility()

    explanation = explain_veto(k9, plan, ctx)
    assert explanation.invariant_id == "K9"
    assert "lacks a deterministic inverse" in explanation.human_reason
    assert explanation.counterexample["plan_has_inverse"] is False
    assert "requires every non-trivial plan to be reversible" in explanation.guidance


def test_explain_veto_custom_invariant_fallback() -> None:
    """Verify fallback explanation for custom/generic invariant outside K1-K9."""

    class CustomInvariant(Invariant):
        id: str = "K99"
        tier: InvariantTier = InvariantTier.PROOF
        statement: str = "Custom invariant property must hold."
        required_facts: tuple[str, ...] = ()

        def build(self, ctx: KernelContext) -> z3.BoolRef:
            _ = ctx
            return z3.BoolVal(False)

    plan = _make_plan()
    ctx = KernelContext(plan, [])
    inv = CustomInvariant()

    explanation = explain_veto(inv, plan, ctx)
    assert explanation.invariant_id == "K99"
    assert (
        "VETO: Invariant K99 violated: Custom invariant property must hold."
        in explanation.human_reason
    )
    assert "Review invariant K99" in explanation.guidance
    assert "(none recorded)" in explanation.actionable_prose


def test_render_veto_reason_delegation() -> None:
    """Verify render_veto_reason returns (human_reason, unsat_core) matching explain_veto."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    plan = _make_plan(action=ActionType.SCALE_WORKLOAD, replica_delta=-2)
    ctx = KernelContext(plan, facts)
    k1 = K1ReplicaFloor()

    reason, core = render_veto_reason(k1, plan, ctx)
    assert "VETO: Invariant K1 violated" in reason
    assert len(core) >= 1
    assert "healthy_replicas[data-service]" in core[0]


def test_format_actionable_prose_formatting() -> None:
    """Verify format_actionable_prose produces all structured sections."""
    prose = format_actionable_prose(
        invariant_id="K3",
        invariant_name="Migration boundary",
        statement="Rollback cannot cross migration.",
        tier=InvariantTier.PROOF,
        human_reason="VETO: Invariant K3 violated: target predates migration",
        failing_facts={"last_migration_commit": "af9de754"},
        counterexample={"diff": "target < migration"},
        guidance="Do not roll back.",
    )
    assert "VETO: Invariant K3 (Migration boundary) Violated" in prose
    assert "Summary:" in prose
    assert "Invariant Definition:" in prose
    assert "Failing Facts:" in prose
    assert "last_migration_commit: af9de754" in prose
    assert "Counterexample:" in prose
    assert "diff: target < migration" in prose
    assert "Actionable Operator Guidance:" in prose
    assert "Do not roll back." in prose

    # Test empty failing_facts and empty counterexample
    prose_empty = format_actionable_prose(
        invariant_id="K1",
        invariant_name="Replica floor",
        statement="Statement.",
        tier=InvariantTier.PROOF,
        human_reason="VETO: Invariant K1 violated",
        failing_facts={},
        counterexample={},
        guidance="Check replicas.",
    )
    assert "(none recorded)" in prose_empty


def test_cli_kernel_explain_pass() -> None:
    """CLI ust kernel explain on safe plan outputs pass summary."""
    result = runner.invoke(
        app,
        [
            "kernel",
            "explain",
            "--plan",
            "fixtures/plan_rollback_safe.json",
            "--facts",
            "fixtures/facts_ok.json",
        ],
    )
    assert result.exit_code == 0
    assert "verdict=pass" in result.stdout
    assert "verified safe" in result.stdout


def test_cli_kernel_explain_veto_prose() -> None:
    """CLI ust kernel explain on veto plan outputs full actionable prose."""
    result = runner.invoke(
        app,
        [
            "kernel",
            "explain",
            "--plan",
            "fixtures/plan_rollback_across_migration.json",
            "--facts",
            "fixtures/facts_ok.json",
        ],
    )
    assert result.exit_code == 0
    assert "VETO: Invariant K3 (Migration boundary) Violated" in result.stdout
    assert "Summary:" in result.stdout
    assert "Failing Facts:" in result.stdout
    assert "Counterexample:" in result.stdout
    assert "Actionable Operator Guidance:" in result.stdout
    assert "c0ffee_across_migration" in result.stdout
    assert "af9de754" in result.stdout


def test_cli_kernel_explain_veto_json() -> None:
    """CLI ust kernel explain supports --json flag for structured output."""
    result = runner.invoke(
        app,
        [
            "kernel",
            "explain",
            "--plan",
            "fixtures/plan_rollback_across_migration.json",
            "--facts",
            "fixtures/facts_ok.json",
            "--json",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["invariant_id"] == "K3"
    assert data["invariant_name"] == "Migration boundary"
    assert "c0ffee_across_migration" in data["human_reason"]
    assert "actionable_prose" in data


def test_cli_kernel_explain_uncertain() -> None:
    """CLI ust kernel explain on uncertain plan outputs missing facts summary."""
    result = runner.invoke(
        app,
        [
            "kernel",
            "explain",
            "--plan",
            "fixtures/plan_rollback_safe.json",
            "--facts",
            "fixtures/facts_missing_migration.json",
        ],
    )
    assert result.exit_code == 0
    assert "verdict=uncertain" in result.stdout
    assert "last_migration_commit_time" in result.stdout


def test_cli_kernel_explain_error_handling(tmp_path: Path) -> None:
    """CLI ust kernel explain validates plan and facts file paths."""
    missing = tmp_path / "missing.json"
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("invalid json", encoding="utf-8")

    # 1. Missing plan file
    res1 = runner.invoke(
        app,
        ["kernel", "explain", "--plan", str(missing), "--facts", "fixtures/facts_ok.json"],
    )
    assert res1.exit_code != 0
    assert "plan file not found" in res1.stderr

    # 2. Missing facts file
    res2 = runner.invoke(
        app,
        [
            "kernel",
            "explain",
            "--plan",
            "fixtures/plan_rollback_safe.json",
            "--facts",
            str(missing),
        ],
    )
    assert res2.exit_code != 0
    assert "facts file not found" in res2.stderr

    # 3. Bad plan content
    res3 = runner.invoke(
        app,
        ["kernel", "explain", "--plan", str(bad_json), "--facts", "fixtures/facts_ok.json"],
    )
    assert res3.exit_code != 0
    assert "Error reading plan" in res3.stderr

    # 4. Bad facts content
    res4 = runner.invoke(
        app,
        [
            "kernel",
            "explain",
            "--plan",
            "fixtures/plan_rollback_safe.json",
            "--facts",
            str(bad_json),
        ],
    )
    assert res4.exit_code != 0
    assert "Error reading facts" in res4.stderr


def test_cli_kernel_verify_with_explain_flag() -> None:
    """CLI ust kernel verify --explain outputs actionable prose on veto."""
    result = runner.invoke(
        app,
        [
            "kernel",
            "verify",
            "--plan",
            "fixtures/plan_rollback_across_migration.json",
            "--facts",
            "fixtures/facts_ok.json",
            "--explain",
        ],
    )
    assert result.exit_code == 0
    assert "verdict=veto invariant=K3" in result.stdout
    assert "Actionable Operator Guidance:" in result.stdout
    assert "VETO: Invariant K3 (Migration boundary) Violated" in result.stdout


def test_cli_kernel_explain_unknown_invariant(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI ust kernel explain handles fallback when failed invariant is not in PROOF_INVARIANTS."""
    from understudy.contracts.kernel import InvariantResult, KernelVerdict

    mock_verdict = KernelVerdict(
        incident_id="inc_test",
        plan_id="plan_test",
        verdict=KernelVerdictType.VETO,
        results=[
            InvariantResult(
                invariant_id="K99",
                tier=InvariantTier.PROOF,
                satisfied=False,
                reason="VETO: Invariant K99 violated",
            )
        ],
        missing_facts=[],
        solver_ms=10.0,
        human_reason="VETO: Invariant K99 violated",
    )

    def _mock_verify(*_a: object, **_kw: object) -> KernelVerdict:
        return mock_verdict

    monkeypatch.setattr("understudy.kernel.api.verify", _mock_verify)

    res = runner.invoke(
        app,
        [
            "kernel",
            "explain",
            "--plan",
            "fixtures/plan_rollback_safe.json",
            "--facts",
            "fixtures/facts_ok.json",
        ],
    )
    assert res.exit_code == 0
    assert "VETO: Invariant K99 (K99) Violated" in res.stdout


def test_cli_kernel_verify_explain_unknown_invariant(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI ust kernel verify --explain handles fallback for unknown invariant."""
    from understudy.contracts.kernel import InvariantResult, KernelVerdict

    mock_verdict = KernelVerdict(
        incident_id="inc_test",
        plan_id="plan_test",
        verdict=KernelVerdictType.VETO,
        results=[
            InvariantResult(
                invariant_id="K99",
                tier=InvariantTier.PROOF,
                satisfied=False,
                reason="VETO: Invariant K99 violated",
            )
        ],
        missing_facts=[],
        solver_ms=10.0,
        human_reason="VETO: Invariant K99 violated",
    )

    def _mock_verify_2(*_a: object, **_kw: object) -> KernelVerdict:
        return mock_verdict

    monkeypatch.setattr("understudy.kernel.api.verify", _mock_verify_2)

    res = runner.invoke(
        app,
        [
            "kernel",
            "verify",
            "--plan",
            "fixtures/plan_rollback_safe.json",
            "--facts",
            "fixtures/facts_ok.json",
            "--explain",
        ],
    )
    assert res.exit_code == 0
    assert "verdict=veto invariant=K99" in res.stdout
    assert "VETO: Invariant K99 violated" in res.stdout


def test_explain_veto_with_string_id_known() -> None:
    """explain_veto resolves known invariant when passed an invariant ID string."""
    plan = _make_plan(action=ActionType.ROLLBACK_DEPLOY, target_commit="c0ffee_across_migration")
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    ctx = KernelContext(plan, facts)
    explanation = explain_veto("K3", plan, ctx)
    assert explanation.invariant_id == "K3"
    assert explanation.invariant_name == "Migration boundary"
    assert "c0ffee_across_migration" in explanation.human_reason


def test_explain_veto_with_string_id_unknown() -> None:
    """explain_veto generates fallback explanation when passed an unknown invariant ID string."""
    plan = _make_plan()
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    ctx = KernelContext(plan, facts)
    explanation = explain_veto("K99", plan, ctx)
    assert explanation.invariant_id == "K99"
    assert explanation.invariant_name == "K99"
    assert "Unknown invariant K99 violated" in explanation.statement
    assert "VETO: Invariant K99 violated" in explanation.human_reason
