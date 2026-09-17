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
from understudy.contracts.enums import KernelVerdictType
from understudy.contracts.kernel import Fact, InvariantResult, KernelVerdict
from understudy.kernel.dsl import Invariant, KernelContext
from understudy.kernel.explain import (
    VetoExplanation,
    explain_veto,
    format_actionable_prose,
    render_veto_reason,
)
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


def verify(
    plan: RemediationPlan,
    facts: Sequence[Fact],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    invariants: Sequence[Invariant] | None = None,
    incident_id: str | None = None,
    clock: Clock | None = None,
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
        incident_id: Optional incident ID override.
        clock: Optional system or frozen clock instance for evaluating timestamps.

    Returns:
        KernelVerdict containing PASS, VETO, or UNCERTAIN, plus per-invariant results and solver_ms.
    """
    start_time = time.perf_counter()
    inc_id = incident_id or "inc_kernel_verify"
    timeout_ms = int(timeout_seconds * 1000)
    evaluated_at = resolve_clock(clock).now()

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
            all_missing = sorted(dict.fromkeys(missing_facts + ctx.missing_facts))
            return KernelVerdict(
                incident_id=inc_id,
                plan_id=plan.plan_id,
                verdict=KernelVerdictType.UNCERTAIN,
                results=results,
                missing_facts=all_missing,
                solver_ms=solver_ms,
                human_reason="UNCERTAIN: Safety kernel solver timed out (5s ceiling exceeded).",
                evaluated_at=evaluated_at,
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
            # Negation is satisfiable -> counterexample exists -> VETO candidate
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

            all_missing = sorted(dict.fromkeys(missing_facts + ctx.missing_facts))
            if not all_missing:
                veto_ms = (time.perf_counter() - start_time) * 1000.0
                return KernelVerdict(
                    incident_id=inc_id,
                    plan_id=plan.plan_id,
                    verdict=KernelVerdictType.VETO,
                    results=results,
                    missing_facts=[],
                    solver_ms=veto_ms,
                    human_reason=veto_reason,
                    evaluated_at=evaluated_at,
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
            solver_ms = (time.perf_counter() - start_time) * 1000.0
            all_missing = sorted(dict.fromkeys(missing_facts + ctx.missing_facts))
            reason = (
                f"UNCERTAIN: Missing required fact(s) for verification: {', '.join(all_missing)}"
                if all_missing
                else f"UNCERTAIN: Safety kernel solver returned unknown: {unknown_reason}"
            )
            return KernelVerdict(
                incident_id=inc_id,
                plan_id=plan.plan_id,
                verdict=KernelVerdictType.UNCERTAIN,
                results=results,
                missing_facts=all_missing,
                solver_ms=solver_ms,
                human_reason=reason,
                evaluated_at=evaluated_at,
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
            evaluated_at=evaluated_at,
        )

    return KernelVerdict(
        incident_id=inc_id,
        plan_id=plan.plan_id,
        verdict=KernelVerdictType.PASS,
        results=results,
        missing_facts=[],
        solver_ms=solver_ms,
        human_reason=f"All {len(results)} PROOF invariants verified safe by safety kernel.",
        evaluated_at=evaluated_at,
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
    "VetoExplanation",
    "Z3SafetyKernel",
    "explain_veto",
    "format_actionable_prose",
    "render_veto_reason",
    "verify",
]
