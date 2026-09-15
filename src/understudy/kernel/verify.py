"""Formal safety verification kernel using Z3 SMT solver.

Implements build-plan step B4.4:
Build the solver, assert facts, then for each PROOF invariant assert its negation
in a scope and check:
- unsat -> satisfied (proved safe)
- sat -> veto with the counterexample model rendered into human_reason
- unknown / timeout -> UNCERTAIN
- 5s timeout, tracked in solver_ms.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import z3

from understudy.common.clock import Clock, resolve_clock
from understudy.common.errors import MissingFact
from understudy.contracts.enums import ActionType, KernelVerdictType
from understudy.contracts.kernel import Fact, InvariantResult, KernelVerdict
from understudy.kernel.dsl import Invariant, KernelContext
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
from understudy.kernel.invariants.k05_single_writer import _canonical_target

if TYPE_CHECKING:
    from collections.abc import Sequence

    from understudy.contracts.plan import RemediationPlan

DEFAULT_TIMEOUT_SECONDS: float = 5.0

# The eight formal PROOF-tier invariants evaluated in canonical order
PROOF_INVARIANTS: tuple[Invariant, ...] = (
    K1ReplicaFloor(),
    K2NamespaceScope(),
    K3MigrationBoundary(),
    K4BlastContainment(),
    K5SingleWriter(),
    K7MutationBudget(),
    K8EvidenceSufficiency(),
    K9Reversibility(),
)


def render_veto_reason(
    inv: Invariant,
    plan: RemediationPlan,
    ctx: KernelContext,
    model: z3.ModelRef | None,
) -> tuple[str, list[str]]:
    """Render a Z3 counterexample model or invariant violation into human prose and unsat core."""
    _ = model
    inv_id = inv.id

    if inv_id == "K1":
        # Extract workload and check replicas
        workloads: set[str] = set()
        if plan.params.workload:
            workloads.add(plan.params.workload)
        for target in plan.target_resources:
            if target.kind == "Deployment":
                workloads.add(target.name)

        reasons: list[str] = []
        cores: list[str] = []
        for svc in sorted(workloads):
            min_reps = ctx.get_int(f"min_replicas[{svc}]")
            reps = ctx.get_int(f"replicas[{svc}]")
            healthy_reps = ctx.get_int(f"healthy_replicas[{svc}]")

            delta = 0
            if plan.action == ActionType.SCALE_WORKLOAD and plan.params.workload == svc:
                delta = plan.params.replica_delta or 0
            elif plan.action == ActionType.RESTART_WORKLOAD:
                delta = -1

            post_reps = reps + delta
            post_healthy = healthy_reps + delta

            if post_reps < min_reps or post_healthy < min_reps:
                reasons.append(
                    f"scaling/restarting '{svc}' leaves post-intervention replicas ({post_reps}) "
                    f"or healthy replicas ({post_healthy}) below minimum floor ({min_reps})"
                )
                cores.append(
                    f"healthy_replicas[{svc}] ({post_healthy}) < min_replicas[{svc}] ({min_reps})"
                )

        detail = "; ".join(reasons)
        human_reason = f"VETO: Invariant K1 violated: {detail}"
        unsat_core = cores
        return human_reason, unsat_core

    if inv_id == "K2":
        auth_ns = ctx.get_str("authorized_namespace")
        target_namespaces: set[str] = {str(ns) for ns in ctx.get_set("plan_target_namespaces")}
        for resource in plan.target_resources:
            if resource.namespace:
                target_namespaces.add(resource.namespace)

        unauthorized = sorted(target_namespaces - {auth_ns})
        unauth_str = ", ".join(unauthorized)
        human_reason = (
            f"VETO: Invariant K2 violated: plan mutates unauthorized namespace(s): {unauth_str}, "
            f"authorized is '{auth_ns}'"
        )
        unsat_core = [f"namespace({ns}) != authorized_namespace({auth_ns})" for ns in unauthorized]
        return human_reason, unsat_core

    if inv_id == "K3":
        target_commit = plan.params.target_commit or "unknown"
        last_mig_commit = (
            ctx.get_str("last_migration_commit")
            if ctx.has_fact("last_migration_commit")
            else "migration"
        )
        last_mig_time = ctx.get_datetime("last_migration_commit_time")

        if target_commit != "unknown" and ctx.has_fact(
            f"target_contains_migration[{target_commit}]"
        ):
            has_mig = ctx.get_bool(f"target_contains_migration[{target_commit}]")
        else:
            has_mig = ctx.get_bool("target_contains_migration")

        if has_mig:
            human_reason = (
                f"VETO: Invariant K3 violated: rollback target commit {target_commit} "
                f"contains a schema migration"
            )
            unsat_core = [f"target_contains_migration({target_commit}) == True"]
            return human_reason, unsat_core

        if target_commit != "unknown" and ctx.has_fact(
            f"rollback_target_commit_time[{target_commit}]"
        ):
            target_time = ctx.get_datetime(f"rollback_target_commit_time[{target_commit}]")
        else:
            target_time = ctx.get_datetime("rollback_target_commit_time")

        human_reason = (
            f"VETO: Invariant K3 violated: rollback target commit {target_commit} deployed at "
            f"{target_time.isoformat()} predates schema migration commit {last_mig_commit} at "
            f"{last_mig_time.isoformat()}"
        )
        unsat_core = [
            f"rollback_target_commit_time ({target_time.isoformat()}) < "
            f"last_migration_commit_time ({last_mig_time.isoformat()})"
        ]
        return human_reason, unsat_core

    if inv_id == "K4":
        declared = ctx.get_set("declared_blast_set")
        observed = ctx.get_set("observed_blast_set")
        declared_str_set = {str(s) for s in declared}
        observed_str_set = {str(s) for s in observed}

        if not observed_str_set.issubset(declared_str_set):
            unpredicted = sorted(observed_str_set - declared_str_set)
            human_reason = (
                f"VETO: Invariant K4 violated: observed blast set touched unpredicted service(s): "
                f"{', '.join(unpredicted)}"
            )
            unsat_core = [f"observed service '{s}' not in declared blast set" for s in unpredicted]
            return human_reason, unsat_core

        human_reason = (
            "VETO: Invariant K4 violated: declared blast set exceeds reachable "
            "dependency graph dependents"
        )
        unsat_core = ["declared_blast_set not subset of reachable dependents"]
        return human_reason, unsat_core

    if inv_id == "K5":
        plan_targets: set[str] = {_canonical_target(r) for r in plan.target_resources}
        plan_targets.update(_canonical_target(t) for t in ctx.get_set("plan_targets"))
        in_flight: set[str] = {_canonical_target(t) for t in ctx.get_set("in_flight_plan_targets")}

        colliding = sorted(plan_targets & in_flight)
        col_str = ", ".join(colliding)
        human_reason = (
            f"VETO: Invariant K5 violated: target resource(s) already held by an in-flight "
            f"execution: {col_str}"
        )
        unsat_core = [f"target '{c}' in in_flight_plan_targets" for c in colliding]
        return human_reason, unsat_core

    if inv_id == "K7":
        mutations = ctx.get_int("prod_mutations_in_window")
        budget = ctx.get_int("mutation_budget")
        human_reason = (
            f"VETO: Invariant K7 violated: production mutations in 15-minute window "
            f"({mutations} + 1) exceeds mutation budget ({budget})"
        )
        unsat_core = [f"prod_mutations_in_window ({mutations}) + 1 > mutation_budget ({budget})"]
        return human_reason, unsat_core

    if inv_id == "K8":
        age = ctx.get_float("evidence_age_seconds")
        samples = ctx.get_int("probe_sample_count")
        drop = ctx.get_float("max_drop_ratio")
        issues: list[str] = []
        if age > 300.0:
            issues.append(f"evidence age {age:.1f}s exceeds 300s limit")
        if samples < 60:
            issues.append(f"probe samples ({samples}) below 60 minimum")
        if drop > 0.05:
            issues.append(f"max drop ratio ({drop:.3f}) exceeds 0.05 ceiling")

        human_reason = f"VETO: Invariant K8 violated: {'; '.join(issues)}"
        return human_reason, issues

    if inv_id == "K9":
        human_reason = (
            "VETO: Invariant K9 violated: plan lacks a deterministic inverse or inverse "
            "targets do not strictly match plan targets"
        )
        unsat_core = ["plan_has_inverse == False or inverse_targets != plan_targets"]
        return human_reason, unsat_core

    human_reason = f"VETO: Invariant {inv_id} violated: {inv.statement}"
    unsat_core = [f"{inv_id} negation satisfied in solver model"]
    return human_reason, unsat_core


def verify(
    plan: RemediationPlan,
    facts: Sequence[Fact],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    invariants: Sequence[Invariant] | None = None,
    clock: Clock | None = None,
    incident_id: str | None = None,
) -> KernelVerdict:
    """Evaluate formal PROOF safety invariants for a proposed plan against system facts.

    Uses Z3 solver to assert the negation of each invariant in a scoped context:
    - unsat -> satisfied (proved safe)
    - sat -> veto with counterexample model rendered into human_reason
    - unknown / timeout -> UNCERTAIN
    - missing facts -> UNCERTAIN (Rule 5.6 zero-default discipline)

    Args:
        plan: The candidate remediation plan to verify.
        facts: List of observed immutable system facts.
        timeout_seconds: Maximum time allowed for verification (default 5.0 s).
        invariants: Optional sequence of Invariants to check (defaults to PROOF_INVARIANTS).
        clock: Optional Clock instance for incident timestamps.
        incident_id: Optional incident ID override.

    Returns:
        KernelVerdict containing PASS, VETO, or UNCERTAIN, plus per-invariant results and solver_ms.
    """
    _ = clock
    start_time = time.perf_counter()
    inc_id = incident_id or "inc_kernel_verify"
    timeout_ms = int(timeout_seconds * 1000)

    ctx = KernelContext(plan, facts)
    inv_list = list(invariants) if invariants is not None else list(PROOF_INVARIANTS)

    results: list[InvariantResult] = []
    missing_facts: list[str] = []

    solver = z3.Solver()
    if timeout_ms > 0:
        solver.set("timeout", timeout_ms)

    for inv in inv_list:
        # Check elapsed time before each invariant
        elapsed_sec = time.perf_counter() - start_time
        remaining_ms = int(timeout_ms - (elapsed_sec * 1000))
        if remaining_ms <= 0:
            solver_ms = (time.perf_counter() - start_time) * 1000.0
            return KernelVerdict(
                incident_id=inc_id,
                plan_id=plan.plan_id,
                verdict=KernelVerdictType.UNCERTAIN,
                results=results,
                missing_facts=missing_facts,
                solver_ms=solver_ms,
                human_reason="UNCERTAIN: Safety kernel solver timed out (5s ceiling exceeded).",
            )

        solver.set("timeout", max(1, remaining_ms))

        # Build invariant formula; missing facts raise MissingFact immediately (Rule 5.6)
        try:
            inv_formula = inv.build(ctx)
        except MissingFact as exc:
            for m in exc.missing_facts:
                if m not in missing_facts:
                    missing_facts.append(m)
            results.append(
                InvariantResult(
                    invariant_id=inv.id,
                    tier=inv.tier,
                    satisfied=None,
                    unsat_core=None,
                    reason=f"Missing required fact(s): {', '.join(exc.missing_facts)}",
                )
            )
            continue

        # Check negation in a scoped solver frame
        solver.push()
        for assertion in ctx.fact_assertions:
            solver.add(assertion)
        solver.add(z3.Not(inv_formula))

        check_res = solver.check()

        if check_res == z3.unsat:
            # Negation is unsatisfiable -> invariant holds across all fact models
            results.append(
                InvariantResult(
                    invariant_id=inv.id,
                    tier=inv.tier,
                    satisfied=True,
                    unsat_core=None,
                    reason=f"Proof discharged unsat by solver ({inv.id} safe)",
                )
            )
            solver.pop()
        elif check_res == z3.sat:
            # Negation is satisfiable -> counterexample exists -> VETO!
            model = solver.model()
            veto_reason, unsat_core = render_veto_reason(inv, plan, ctx, model)
            results.append(
                InvariantResult(
                    invariant_id=inv.id,
                    tier=inv.tier,
                    satisfied=False,
                    unsat_core=unsat_core,
                    reason=veto_reason,
                )
            )
            solver.pop()

            solver_ms = (time.perf_counter() - start_time) * 1000.0
            return KernelVerdict(
                incident_id=inc_id,
                plan_id=plan.plan_id,
                verdict=KernelVerdictType.VETO,
                results=results,
                missing_facts=[],
                solver_ms=solver_ms,
                human_reason=veto_reason,
            )
        else:
            # unknown (timeout or undecidable)
            unknown_reason = solver.reason_unknown()
            results.append(
                InvariantResult(
                    invariant_id=inv.id,
                    tier=inv.tier,
                    satisfied=None,
                    unsat_core=None,
                    reason=f"Solver returned unknown: {unknown_reason}",
                )
            )
            solver.pop()

            solver_ms = (time.perf_counter() - start_time) * 1000.0
            return KernelVerdict(
                incident_id=inc_id,
                plan_id=plan.plan_id,
                verdict=KernelVerdictType.UNCERTAIN,
                results=results,
                missing_facts=[],
                solver_ms=solver_ms,
                human_reason=f"UNCERTAIN: Safety kernel solver returned unknown: {unknown_reason}",
            )

    solver_ms = (time.perf_counter() - start_time) * 1000.0

    # Combine all missing facts tracked during evaluation
    all_missing = sorted(dict.fromkeys(missing_facts + ctx.missing_facts))
    if all_missing:
        missing_str = ", ".join(all_missing)
        return KernelVerdict(
            incident_id=inc_id,
            plan_id=plan.plan_id,
            verdict=KernelVerdictType.UNCERTAIN,
            results=results,
            missing_facts=all_missing,
            solver_ms=solver_ms,
            human_reason=f"UNCERTAIN: Missing required fact(s) for verification: {missing_str}",
        )

    return KernelVerdict(
        incident_id=inc_id,
        plan_id=plan.plan_id,
        verdict=KernelVerdictType.PASS,
        results=results,
        missing_facts=[],
        solver_ms=solver_ms,
        human_reason=f"All {len(results)} PROOF invariants verified safe by safety kernel.",
    )


class Z3SafetyKernel:
    """Formal safety verification kernel using Z3 SMT solver."""

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        invariants: Sequence[Invariant] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._invariants = invariants
        self.clock: Clock = resolve_clock(clock)

    async def verify(self, plan: RemediationPlan, facts: list[Fact]) -> KernelVerdict:
        """Evaluate safety invariants for a proposed plan against current system facts."""
        return await asyncio.to_thread(
            verify,
            plan=plan,
            facts=facts,
            timeout_seconds=self.timeout_seconds,
            invariants=self._invariants,
            clock=self.clock,
        )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "PROOF_INVARIANTS",
    "Z3SafetyKernel",
    "render_veto_reason",
    "verify",
]
