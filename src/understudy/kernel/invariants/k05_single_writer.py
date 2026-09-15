"""Invariant K5: Single writer formal proof verification.

Implements build-plan step B4.3f:
No two plans may hold overlapping target resources concurrently, across
incident handling and shadow mode.
"""

from collections.abc import Sequence
from typing import Any

import z3

from understudy.contracts.enums import ActionType, InvariantTier
from understudy.contracts.plan import ResourceRef
from understudy.kernel.dsl import Invariant, KernelContext


def _canonical_target(target: Any) -> str:
    """Format a target resource into a canonical 'Kind/Namespace/Name' string."""
    if isinstance(target, ResourceRef):
        ns = target.namespace or ""
        return f"{target.kind}/{ns}/{target.name}"
    if isinstance(target, dict):
        kind = target.get("kind", "")
        ns = target.get("namespace", "") or ""
        name = target.get("name", "")
        return f"{kind}/{ns}/{name}"
    return str(target)


class K5SingleWriter(Invariant):
    """Safety invariant proving that candidate plans do not collide with in-flight plans.

    SMT shape (docs/03-invariants.md §3.4):
    plan_targets ∩ in_flight_plan_targets = ∅
    """

    id: str = "K5"
    name: str = "Single writer"
    tier: InvariantTier = InvariantTier.PROOF
    tier_display: str = "PROOF"
    statement: str = (
        "No two plans may hold overlapping target resources concurrently, across\n"
        "incident handling and shadow mode."
    )
    required_facts: Sequence[str] = (
        "plan_targets",
        "in_flight_plan_targets",
    )
    smt_shape: str | None = "`plan_targets ∩ in_flight_plan_targets = ∅`"
    why_it_exists: str | None = (
        "Shadow mode runs continuously (ADR-020). Without this, a speculative\n"
        "injection and a real remediation can collide on the same Deployment. The fact is "
        "read from\n"
        "the store under a transaction that also inserts the claim, so the check and the "
        "claim are\n"
        "atomic."
    )

    def build(self, ctx: KernelContext) -> z3.BoolRef:
        """Build SMT formula asserting plan targets are strictly disjoint from in-flight targets.

        Enforces single-writer isolation across incident handling and continuous shadow mode:
        1. Vacuously safe for NO_ACTION plans with no target resources and no facts.
        2. Zero-default safety (Rule 5.6):
           - Missing 'plan_targets' raises MissingFact.
           - Missing 'in_flight_plan_targets' raises MissingFact.
        3. Canonicalizes target resources from fact set and defense-in-depth from ctx.plan.
        4. If either target set is empty, returns z3.BoolVal(True).
        5. Asserts pairwise inequality across all plan and in-flight targets:
           ∀ p ∈ plan_targets, ∀ f ∈ in_flight_plan_targets: p ≠ f
        """
        if (
            ctx.plan.action == ActionType.NO_ACTION
            and not ctx.has_fact("plan_targets")
            and not ctx.has_fact("in_flight_plan_targets")
            and not ctx.plan.target_resources
        ):
            return z3.BoolVal(True)

        raw_plan_targets = ctx.get_raw("plan_targets")
        raw_in_flight_targets = ctx.get_raw("in_flight_plan_targets")

        plan_list = (
            list(raw_plan_targets)
            if isinstance(raw_plan_targets, (list, tuple, set, frozenset))
            else [raw_plan_targets]
        )
        in_flight_list = (
            list(raw_in_flight_targets)
            if isinstance(raw_in_flight_targets, (list, tuple, set, frozenset))
            else [raw_in_flight_targets]
        )

        plan_targets: set[str] = {_canonical_target(t) for t in plan_list}
        for resource in ctx.plan.target_resources:
            plan_targets.add(_canonical_target(resource))

        in_flight_targets: set[str] = {_canonical_target(t) for t in in_flight_list}

        if not plan_targets or not in_flight_targets:
            return z3.BoolVal(True)

        conditions: list[z3.BoolRef] = [
            z3.StringVal(p) != z3.StringVal(f)
            for p in sorted(plan_targets)
            for f in sorted(in_flight_targets)
        ]

        if len(conditions) == 1:
            return conditions[0]
        return z3.And(*conditions)


__all__ = ["K5SingleWriter"]
