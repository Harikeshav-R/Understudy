"""Unit tests for Safety Kernel formal verification (verify.py).

Implements and verifies build-plan step B4.4:
- Protocol conformance: Z3SafetyKernel conforms to SafetyKernel.
- Verification algorithm:
  - 8 formal PROOF invariants checked in scoped solver frames.
  - unsat -> PASS.
  - sat -> VETO with model rendered into human_reason and unsat_core.
  - unknown / timeout -> UNCERTAIN.
  - missing facts -> UNCERTAIN (Rule 5.6 zero-default discipline).
  - 5 s timeout enforcement, solver_ms tracking (< 500 ms).
- Full coverage of render_veto_reason for all invariant types (K1-K9 and generic).
- CLI commands: 'ust kernel verify' for PASS, VETO, UNCERTAIN, and error branches.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import z3
from typer.testing import CliRunner

from understudy.cli import app
from understudy.common.clock import FrozenClock
from understudy.contracts.enums import ActionType, InvariantTier, KernelVerdictType
from understudy.contracts.kernel import Fact
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.kernel.api import (
    PROOF_INVARIANTS,
    SafetyKernel,
    Z3SafetyKernel,
    load_facts_json,
    render_veto_reason,
    verify,
)
from understudy.kernel.dsl import Invariant, KernelContext

runner = CliRunner()


def _make_plan(
    *,
    plan_id: str = "plan_test",
    action: ActionType = ActionType.ROLLBACK_DEPLOY,
    workload: str = "data-service",
    target_commit: str | None = "c0ffee_safe",
    replica_delta: int | None = None,
    target_resources: list[ResourceRef] | None = None,
    declared_blast_set: list[str] | None = None,
    with_inverse: bool = True,
    namespace: str = "ust-prod",
) -> RemediationPlan:
    """Create a test RemediationPlan candidate."""
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


def test_protocol_conformance() -> None:
    """Verify that Z3SafetyKernel conforms to the SafetyKernel Protocol."""
    kernel = Z3SafetyKernel()
    assert isinstance(kernel, SafetyKernel)
    assert len(PROOF_INVARIANTS) == 8


@pytest.mark.asyncio
async def test_z3_safety_kernel_async_verify() -> None:
    """Verify that Z3SafetyKernel.verify async method operates correctly."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    plan = _make_plan()
    clock = FrozenClock(datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC))
    kernel = Z3SafetyKernel(clock=clock)

    verdict = await kernel.verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.PASS
    assert len(verdict.results) == 8
    assert verdict.solver_ms < 500.0


def test_verify_pass_fixtures_plan_rollback_safe() -> None:
    """Safe rollback plan against facts_ok.json discharges unsat on all 8 PROOF invariants."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    plan_text = Path("fixtures/plan_rollback_safe.json").read_text(encoding="utf-8")
    plan = RemediationPlan.model_validate_json(plan_text)

    verdict = verify(plan, facts, incident_id="inc_custom")
    assert verdict.incident_id == "inc_custom"
    assert verdict.plan_id == plan.plan_id
    assert verdict.verdict == KernelVerdictType.PASS
    assert len(verdict.results) == 8
    assert all(r.satisfied is True for r in verdict.results)
    assert verdict.missing_facts == []
    assert verdict.solver_ms < 500.0
    assert "All 8 PROOF invariants verified safe" in verdict.human_reason


def test_verify_veto_k1_scale_to_zero() -> None:
    """Scale-to-zero plan against facts_ok.json triggers VETO on K1."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    plan_text = Path("fixtures/plan_scale_to_zero.json").read_text(encoding="utf-8")
    plan = RemediationPlan.model_validate_json(plan_text)

    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    assert len(verdict.results) == 1
    assert verdict.results[0].invariant_id == "K1"
    assert verdict.results[0].satisfied is False
    assert verdict.results[0].unsat_core is not None
    assert "K1 violated" in verdict.human_reason
    assert "leaves post-intervention replicas (0)" in verdict.human_reason
    assert verdict.solver_ms < 500.0


def test_verify_veto_k2_unauthorized_namespace() -> None:
    """Plan targeting an unauthorized namespace triggers VETO on K2."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    plan = _make_plan(
        namespace="rogue-namespace",
        target_resources=[
            ResourceRef(kind="Deployment", name="data-service", namespace="rogue-namespace")
        ],
    )

    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    k2_res = next(r for r in verdict.results if r.invariant_id == "K2")
    assert k2_res.satisfied is False
    assert "K2 violated" in verdict.human_reason
    assert "rogue-namespace" in verdict.human_reason


def test_verify_veto_k3_rollback_across_migration() -> None:
    """Rollback across migration plan triggers VETO on K3 naming migration commit."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    plan_text = Path("fixtures/plan_rollback_across_migration.json").read_text(encoding="utf-8")
    plan = RemediationPlan.model_validate_json(plan_text)

    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    k3_res = next(r for r in verdict.results if r.invariant_id == "K3")
    assert k3_res.satisfied is False
    assert "K3 violated" in verdict.human_reason
    assert "c0ffee_across_migration" in verdict.human_reason
    assert "af9de754" in verdict.human_reason
    assert "2026-09-13T09:00:00" in verdict.human_reason


def test_verify_veto_k3_target_contains_migration() -> None:
    """Rollback targeting a commit containing a migration triggers VETO on K3."""
    facts = [
        f
        for f in load_facts_json(Path("fixtures/facts_ok.json"))
        if f.name != "target_contains_migration"
    ]
    facts.append(
        Fact(
            name="target_contains_migration",
            value=True,
            source="github",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        )
    )
    plan = _make_plan(target_commit="c0ffee_migration_commit")

    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    k3_res = next(r for r in verdict.results if r.invariant_id == "K3")
    assert k3_res.satisfied is False
    assert "contains a schema migration" in verdict.human_reason


def test_verify_veto_k4_unpredicted_blast() -> None:
    """Plan where observed blast touches unpredicted services triggers VETO on K4."""
    facts = [
        f for f in load_facts_json(Path("fixtures/facts_ok.json")) if f.name != "observed_blast_set"
    ]
    facts.append(
        Fact(
            name="observed_blast_set",
            value={"data-service", "unpredicted-service"},
            source="tournament",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        )
    )
    plan = _make_plan()

    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    k4_res = next(r for r in verdict.results if r.invariant_id == "K4")
    assert k4_res.satisfied is False
    assert "unpredicted-service" in verdict.human_reason


def test_verify_veto_k4_declared_blast_exceeds_dependents() -> None:
    """Plan where declared blast set exceeds reachable dependents triggers VETO on K4."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    plan = _make_plan(
        declared_blast_set=["data-service", "auth-service", "unreachable-service"],
    )

    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    k4_res = next(r for r in verdict.results if r.invariant_id == "K4")
    assert k4_res.satisfied is False
    assert "exceeds reachable dependency graph dependents" in verdict.human_reason


def test_verify_veto_k5_single_writer_collision() -> None:
    """Plan with target colliding with in-flight plan triggers VETO on K5."""
    facts = [
        f
        for f in load_facts_json(Path("fixtures/facts_ok.json"))
        if f.name != "in_flight_plan_targets"
    ]
    facts.append(
        Fact(
            name="in_flight_plan_targets",
            value=[ResourceRef(kind="Deployment", name="data-service", namespace="ust-prod")],
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        )
    )
    plan = _make_plan()

    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    k5_res = next(r for r in verdict.results if r.invariant_id == "K5")
    assert k5_res.satisfied is False
    assert "K5 violated" in verdict.human_reason
    assert "Deployment/ust-prod/data-service" in verdict.human_reason


def test_verify_veto_k7_mutation_budget_exceeded() -> None:
    """Plan when mutations in window reaches budget triggers VETO on K7."""
    facts = [
        f
        for f in load_facts_json(Path("fixtures/facts_ok.json"))
        if f.name != "prod_mutations_in_window"
    ]
    facts.append(
        Fact(
            name="prod_mutations_in_window",
            value=3,
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        )
    )
    plan = _make_plan()

    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    k7_res = next(r for r in verdict.results if r.invariant_id == "K7")
    assert k7_res.satisfied is False
    assert "K7 violated" in verdict.human_reason
    assert "exceeds mutation budget" in verdict.human_reason


def test_verify_veto_k8_evidence_sufficiency_failure() -> None:
    """Plan with stale rehearsal evidence triggers VETO on K8."""
    facts = [
        f
        for f in load_facts_json(Path("fixtures/facts_ok.json"))
        if f.name not in ("evidence_age_seconds", "probe_sample_count", "max_drop_ratio")
    ]
    facts.extend(
        [
            Fact(
                name="evidence_age_seconds",
                value=450.0,
                source="tournament",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
            Fact(
                name="probe_sample_count",
                value=20,
                source="tournament",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
            Fact(
                name="max_drop_ratio",
                value=0.12,
                source="tournament",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
        ]
    )
    plan = _make_plan()

    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    k8_res = next(r for r in verdict.results if r.invariant_id == "K8")
    assert k8_res.satisfied is False
    assert "K8 violated" in verdict.human_reason
    assert "evidence age 450.0s exceeds 300s limit" in verdict.human_reason
    assert "probe samples (20) below 60 minimum" in verdict.human_reason
    assert "max drop ratio (0.120) exceeds 0.05 ceiling" in verdict.human_reason


def test_verify_veto_k9_reversibility_missing_inverse() -> None:
    """Plan lacking an inverse plan triggers VETO on K9."""
    facts = [
        f for f in load_facts_json(Path("fixtures/facts_ok.json")) if f.name != "plan_has_inverse"
    ]
    facts.append(
        Fact(
            name="plan_has_inverse",
            value=False,
            source="plan",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        )
    )
    plan = _make_plan(with_inverse=False)

    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    k9_res = next(r for r in verdict.results if r.invariant_id == "K9")
    assert k9_res.satisfied is False
    assert "K9 violated" in verdict.human_reason


def test_verify_uncertain_missing_migration_fact() -> None:
    """Verification against facts_missing_migration.json yields UNCERTAIN."""
    facts = load_facts_json(Path("fixtures/facts_missing_migration.json"))
    plan = _make_plan()

    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.UNCERTAIN
    assert "last_migration_commit_time" in verdict.missing_facts
    assert "UNCERTAIN: Missing required fact(s)" in verdict.human_reason
    assert "last_migration_commit_time" in verdict.human_reason


def test_verify_uncertain_solver_timeout_before_loop() -> None:
    """Verify timeout when remaining_ms <= 0 before/during loop."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    plan = _make_plan()

    # With negative timeout, remaining_ms is <= 0 immediately
    verdict = verify(plan, facts, timeout_seconds=-1.0)
    assert verdict.verdict == KernelVerdictType.UNCERTAIN
    assert "solver timed out" in verdict.human_reason


def test_verify_uncertain_solver_unknown() -> None:
    """Verify UNCERTAIN verdict when solver returns unknown."""
    facts = load_facts_json(Path("fixtures/facts_ok.json"))
    plan = _make_plan()

    with patch("understudy.kernel.verify.z3.Solver") as mock_solver_cls:
        mock_solver = MagicMock()
        mock_solver.check.return_value = z3.unknown
        mock_solver.reason_unknown.return_value = "resource limit reached"
        mock_solver_cls.return_value = mock_solver

        verdict = verify(plan, facts)
        assert verdict.verdict == KernelVerdictType.UNCERTAIN
        assert "resource limit reached" in verdict.human_reason


def test_render_veto_reason_custom_invariant_fallback() -> None:
    """Verify fallback rendering for an invariant not matching K1-K9."""

    class CustomInv(Invariant):
        id: str = "K99"
        tier: InvariantTier = InvariantTier.PROOF
        statement: str = "Custom property must hold."
        required_facts: tuple[str, ...] = ()

        def build(self, ctx: KernelContext) -> z3.BoolRef:
            _ = ctx
            return z3.BoolVal(False)

    plan = _make_plan()
    ctx = KernelContext(plan, [])
    reason, core = render_veto_reason(CustomInv(), plan, ctx, None)
    assert "VETO: Invariant K99 violated" in reason
    assert "K99 negation satisfied" in core[0]


def test_cli_kernel_verify_pass() -> None:
    """CLI ust kernel verify produces pass format matching Checkpoint B4."""
    result = runner.invoke(
        app,
        [
            "kernel",
            "verify",
            "--plan",
            "fixtures/plan_rollback_safe.json",
            "--facts",
            "fixtures/facts_ok.json",
        ],
    )
    assert result.exit_code == 0
    assert "verdict=pass" in result.stdout
    assert "8 invariants" in result.stdout
    assert "solver_ms=" in result.stdout


def test_cli_kernel_verify_veto_k3() -> None:
    """CLI ust kernel verify produces veto K3 matching Checkpoint B4."""
    result = runner.invoke(
        app,
        [
            "kernel",
            "verify",
            "--plan",
            "fixtures/plan_rollback_across_migration.json",
            "--facts",
            "fixtures/facts_ok.json",
        ],
    )
    assert result.exit_code == 0
    assert "verdict=veto invariant=K3" in result.stdout
    assert "af9de754" in result.stdout
    assert "2026-09-13T09:00:00" in result.stdout


def test_cli_kernel_verify_veto_k1() -> None:
    """CLI ust kernel verify produces veto K1 matching Checkpoint B4."""
    result = runner.invoke(
        app,
        [
            "kernel",
            "verify",
            "--plan",
            "fixtures/plan_scale_to_zero.json",
            "--facts",
            "fixtures/facts_ok.json",
        ],
    )
    assert result.exit_code == 0
    assert "verdict=veto invariant=K1" in result.stdout
    assert "below minimum floor" in result.stdout


def test_cli_kernel_verify_uncertain() -> None:
    """CLI ust kernel verify produces uncertain matching Checkpoint B4."""
    result = runner.invoke(
        app,
        [
            "kernel",
            "verify",
            "--plan",
            "fixtures/plan_rollback_safe.json",
            "--facts",
            "fixtures/facts_missing_migration.json",
        ],
    )
    assert result.exit_code == 0
    assert 'verdict=uncertain missing_facts=["last_migration_commit_time"]' in result.stdout


def test_cli_kernel_verify_json_output() -> None:
    """CLI ust kernel verify supports --json flag."""
    result = runner.invoke(
        app,
        [
            "kernel",
            "verify",
            "--plan",
            "fixtures/plan_rollback_safe.json",
            "--facts",
            "fixtures/facts_ok.json",
            "--json",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["verdict"] == "pass"
    assert len(data["results"]) == 8


def test_cli_kernel_verify_error_handling(tmp_path: Path) -> None:
    """CLI ust kernel verify handles missing files and corrupted JSON cleanly."""
    missing = tmp_path / "nonexistent.json"
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("invalid json content", encoding="utf-8")

    # 1. Missing plan file
    res1 = runner.invoke(
        app, ["kernel", "verify", "--plan", str(missing), "--facts", "fixtures/facts_ok.json"]
    )
    assert res1.exit_code != 0
    assert "plan file not found" in res1.stderr

    # 2. Missing facts file
    res2 = runner.invoke(
        app,
        ["kernel", "verify", "--plan", "fixtures/plan_rollback_safe.json", "--facts", str(missing)],
    )
    assert res2.exit_code != 0
    assert "facts file not found" in res2.stderr

    # 3. Bad plan JSON
    res3 = runner.invoke(
        app, ["kernel", "verify", "--plan", str(bad_json), "--facts", "fixtures/facts_ok.json"]
    )
    assert res3.exit_code != 0
    assert "Error reading plan" in res3.stderr

    # 4. Bad facts JSON
    res4 = runner.invoke(
        app,
        [
            "kernel",
            "verify",
            "--plan",
            "fixtures/plan_rollback_safe.json",
            "--facts",
            str(bad_json),
        ],
    )
    assert res4.exit_code != 0
    assert "Error reading facts" in res4.stderr


def test_verify_veto_k1_restart_workload() -> None:
    """RESTART_WORKLOAD drops healthy replica by 1, triggering VETO if already at floor."""
    facts = [
        f
        for f in load_facts_json(Path("fixtures/facts_ok.json"))
        if f.name
        not in {
            "replicas[auth-service]",
            "healthy_replicas[auth-service]",
            "min_replicas[auth-service]",
        }
    ]
    facts.extend(
        [
            Fact(
                name="replicas[auth-service]",
                value=1,
                source="k8s",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
            Fact(
                name="healthy_replicas[auth-service]",
                value=1,
                source="k8s",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
            Fact(
                name="min_replicas[auth-service]",
                value=1,
                source="k8s",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
        ]
    )
    plan = _make_plan(
        action=ActionType.RESTART_WORKLOAD,
        workload="auth-service",
        target_resources=[
            ResourceRef(kind="Deployment", name="auth-service", namespace="ust-prod")
        ],
    )
    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    assert "Invariant K1 violated" in verdict.human_reason
    assert "scaling/restarting 'auth-service'" in verdict.human_reason


def test_verify_veto_k1_multi_workload_partial_violation() -> None:
    """Plan where one workload violates K1 and another does not."""
    facts = [
        f
        for f in load_facts_json(Path("fixtures/facts_ok.json"))
        if f.name
        not in {
            "replicas[auth-service]",
            "healthy_replicas[auth-service]",
            "min_replicas[auth-service]",
            "replicas[data-service]",
            "healthy_replicas[data-service]",
            "min_replicas[data-service]",
        }
    ]
    facts.extend(
        [
            # auth-service violates
            Fact(
                name="replicas[auth-service]",
                value=1,
                source="k8s",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
            Fact(
                name="healthy_replicas[auth-service]",
                value=1,
                source="k8s",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
            Fact(
                name="min_replicas[auth-service]",
                value=1,
                source="k8s",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
            # data-service does not violate (reps=3, healthy=3, min=1)
            Fact(
                name="replicas[data-service]",
                value=3,
                source="k8s",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
            Fact(
                name="healthy_replicas[data-service]",
                value=3,
                source="k8s",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
            Fact(
                name="min_replicas[data-service]",
                value=1,
                source="k8s",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
        ]
    )
    plan = _make_plan(
        action=ActionType.SCALE_WORKLOAD,
        workload="auth-service",
        replica_delta=-1,
        target_resources=[
            ResourceRef(kind="Deployment", name="data-service", namespace="ust-prod"),
            ResourceRef(kind="ConfigMap", name="data-service-cm", namespace="ust-prod"),
        ],
    )
    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    assert "scaling/restarting 'auth-service'" in verdict.human_reason
    assert "data-service" not in verdict.human_reason


def test_verify_veto_k1_target_without_workload_param() -> None:
    """Plan with params.workload='' but target_resources specifying Deployment."""
    facts = [
        f
        for f in load_facts_json(Path("fixtures/facts_ok.json"))
        if f.name
        not in {
            "replicas[auth-service]",
            "healthy_replicas[auth-service]",
            "min_replicas[auth-service]",
        }
    ]
    facts.extend(
        [
            Fact(
                name="replicas[auth-service]",
                value=1,
                source="k8s",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
            Fact(
                name="healthy_replicas[auth-service]",
                value=1,
                source="k8s",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
            Fact(
                name="min_replicas[auth-service]",
                value=1,
                source="k8s",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
        ]
    )
    plan = _make_plan(
        action=ActionType.RESTART_WORKLOAD,
        workload="",
        target_resources=[
            ResourceRef(kind="Deployment", name="auth-service", namespace="ust-prod")
        ],
    )
    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    assert "scaling/restarting 'auth-service'" in verdict.human_reason


def test_verify_veto_k2_target_without_namespace() -> None:
    """Plan target resource without namespace still triggers VETO if unauthorized ns present."""
    facts = [
        f
        for f in load_facts_json(Path("fixtures/facts_ok.json"))
        if f.name != "plan_target_namespaces"
    ]
    facts.append(
        Fact(
            name="plan_target_namespaces",
            value=["ust-prod", "rogue-namespace"],
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        )
    )
    plan = _make_plan(
        target_resources=[
            ResourceRef(kind="Deployment", name="data-service", namespace=""),
            ResourceRef(kind="Deployment", name="auth-service", namespace="ust-prod"),
        ]
    )
    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    assert "rogue-namespace" in verdict.human_reason


def test_verify_veto_k3_scalar_fallback_predating_migration() -> None:
    """K3 vetoes using scalar fallback facts when commit is not in subscripted facts."""
    facts = [
        f
        for f in load_facts_json(Path("fixtures/facts_ok.json"))
        if not f.name.startswith("rollback_target_commit_time[")
        and not f.name.startswith("target_contains_migration[")
        and f.name not in {"rollback_target_commit_time", "target_contains_migration"}
    ]
    facts.extend(
        [
            # Scalar facts
            Fact(
                name="rollback_target_commit_time",
                value=datetime(2026, 9, 13, 9, 0, 0, tzinfo=UTC),
                source="github",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
            Fact(
                name="target_contains_migration",
                value=False,
                source="github",
                observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
            ),
        ]
    )
    plan = _make_plan(
        action=ActionType.ROLLBACK_DEPLOY,
        target_commit="unknown_subscript_commit",
    )
    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    assert "predates schema migration" in verdict.human_reason


def test_verify_veto_k3_scalar_fallback_contains_migration() -> None:
    """K3 vetoes using scalar fallback fact when target_contains_migration is True."""
    facts = [
        f
        for f in load_facts_json(Path("fixtures/facts_ok.json"))
        if not f.name.startswith("target_contains_migration[")
        and f.name != "target_contains_migration"
    ]
    facts.append(
        Fact(
            name="target_contains_migration",
            value=True,
            source="github",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        )
    )
    plan = _make_plan(
        action=ActionType.ROLLBACK_DEPLOY,
        target_commit="unknown_subscript_commit",
    )
    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.VETO
    assert "contains a schema migration" in verdict.human_reason


def test_verify_veto_k8_individual_threshold_violations() -> None:
    """K8 vetoes correctly for each individual threshold condition."""
    base_facts = [
        f
        for f in load_facts_json(Path("fixtures/facts_ok.json"))
        if f.name not in {"evidence_age_seconds", "probe_sample_count", "max_drop_ratio"}
    ]

    # 1. Only age violation: age=400, samples=100, drop=0.01
    facts_age = [
        *base_facts,
        Fact(
            name="evidence_age_seconds",
            value=400.0,
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        ),
        Fact(
            name="probe_sample_count",
            value=100,
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        ),
        Fact(
            name="max_drop_ratio",
            value=0.01,
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        ),
    ]
    v_age = verify(_make_plan(), facts_age)
    assert v_age.verdict == KernelVerdictType.VETO
    assert "evidence age 400.0s exceeds 300s limit" in v_age.human_reason
    assert "probe samples" not in v_age.human_reason
    assert "max drop ratio" not in v_age.human_reason

    # 2. Only sample violation: age=100, samples=30, drop=0.01
    facts_samples = [
        *base_facts,
        Fact(
            name="evidence_age_seconds",
            value=100.0,
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        ),
        Fact(
            name="probe_sample_count",
            value=30,
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        ),
        Fact(
            name="max_drop_ratio",
            value=0.01,
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        ),
    ]
    v_samples = verify(_make_plan(), facts_samples)
    assert v_samples.verdict == KernelVerdictType.VETO
    assert "probe samples (30) below 60 minimum" in v_samples.human_reason
    assert "evidence age" not in v_samples.human_reason
    assert "max drop ratio" not in v_samples.human_reason

    # 3. Only drop ratio violation: age=100, samples=100, drop=0.10
    facts_drop = [
        *base_facts,
        Fact(
            name="evidence_age_seconds",
            value=100.0,
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        ),
        Fact(
            name="probe_sample_count",
            value=100,
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        ),
        Fact(
            name="max_drop_ratio",
            value=0.10,
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        ),
    ]
    v_drop = verify(_make_plan(), facts_drop)
    assert v_drop.verdict == KernelVerdictType.VETO
    assert "max drop ratio (0.100) exceeds 0.05 ceiling" in v_drop.human_reason
    assert "evidence age" not in v_drop.human_reason
    assert "probe samples" not in v_drop.human_reason


def test_verify_uncertain_duplicate_missing_fact_across_invariants() -> None:
    """Duplicate missing fact across multiple invariants (K5 and K9 both require plan_targets)."""
    facts = [f for f in load_facts_json(Path("fixtures/facts_ok.json")) if f.name != "plan_targets"]
    verdict = verify(_make_plan(), facts)
    assert verdict.verdict == KernelVerdictType.UNCERTAIN
    assert "plan_targets" in verdict.missing_facts


def test_verify_uncertain_precedence_over_veto_with_missing_facts() -> None:
    """Missing fact ensures UNCERTAIN verdict even if a subsequent invariant finds sat."""
    # Plan targeting unauthorized namespace (violates K2),
    # but omit K1 required fact 'healthy_replicas'
    plan = _make_plan(
        workload="data-service",
        target_resources=[
            ResourceRef(kind="Deployment", name="data-service", namespace="rogue-namespace")
        ],
    )
    facts = [
        f
        for f in load_facts_json(Path("fixtures/facts_ok.json"))
        if not f.name.startswith("healthy_replicas") and f.name != "plan_target_namespaces"
    ]
    facts.append(
        Fact(
            name="plan_target_namespaces",
            value=["rogue-namespace"],
            source="store",
            observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
        )
    )
    verdict = verify(plan, facts)
    assert verdict.verdict == KernelVerdictType.UNCERTAIN
    assert any("healthy_replicas" in m for m in verdict.missing_facts)
    assert "UNCERTAIN: Missing required fact(s)" in verdict.human_reason
    k2_res = next((r for r in verdict.results if r.invariant_id == "K2"), None)
    assert k2_res is not None
    assert k2_res.satisfied is False
