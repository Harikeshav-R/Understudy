"""Invariant K9: Reversibility formal proof verification.

Implements build-plan step B4.3d:
Every plan except NO_ACTION declares an inverse whose target set equals its own.
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
        return f"{target.kind}/{target.namespace}/{target.name}"
    return str(target)


class K9Reversibility(Invariant):
    """Safety invariant proving that candidate plans are strictly reversible."""

    id: str = "K9"
    name: str = "Reversibility"
    tier: InvariantTier = InvariantTier.PROOF
    tier_display: str = "PROOF"
    statement: str = (
        "Every plan except NO_ACTION declares an inverse whose target set equals its own."
    )
    doc_statement: str | None = (
        "Every plan except `NO_ACTION` declares an inverse whose target set equals\nits own."
    )
    required_facts: Sequence[str] = (
        "plan_has_inverse",
        "inverse_targets",
        "plan_targets",
    )
    smt_shape: str | None = (
        "`plan.action ≠ NO_ACTION ⟹ plan_has_inverse ∧ inverse_targets = plan_targets`"
    )
    why_it_exists: str | None = (
        "If the fix makes things worse, the system must be able to undo exactly\n"
        "what it did, and no more. Equality rather than subset is deliberate: an "
        "inverse that touches\n"
        "fewer resources leaves partial state behind."
    )

    def build(self, ctx: KernelContext) -> z3.BoolRef:
        """Build SMT formula asserting plan reversibility and target equality.

        SMT shape (docs/03-invariants.md §3.4):
        plan.action ≠ NO_ACTION ⟹ plan_has_inverse ∧ inverse_targets = plan_targets

        1. If plan.action is NO_ACTION, the implication is vacuously true,
           returning z3.BoolVal(True) without requiring inverse facts.
        2. If plan.action is not NO_ACTION:
           - Retrieves 'plan_has_inverse' (Bool constant asserted to fact value)
           - Retrieves 'plan_targets' (set of ResourceRef)
           - Retrieves 'inverse_targets' (set of ResourceRef)
           Missing facts raise MissingFact immediately (Rule 5.6 zero defaults).
        3. Normalizes target sets, including defense-in-depth targets from ctx.plan.
        4. Asserts:
           - plan_has_inverse is True
           - |inverse_targets| == |plan_targets|
           - ∀ p ∈ plan_targets: p ∈ inverse_targets
           - ∀ i ∈ inverse_targets: i ∈ plan_targets
        """
        if ctx.plan.action == ActionType.NO_ACTION:
            return z3.BoolVal(True)

        has_inverse = ctx.bool("plan_has_inverse")
        raw_plan_targets = ctx.get_set("plan_targets")
        raw_inv_targets = ctx.get_set("inverse_targets")

        plan_targets: set[str] = {_canonical_target(t) for t in raw_plan_targets}
        for resource in ctx.plan.target_resources:
            plan_targets.add(_canonical_target(resource))

        inv_targets: set[str] = {_canonical_target(t) for t in raw_inv_targets}
        if ctx.plan.inverse is not None:
            for resource in ctx.plan.inverse.target_resources:
                inv_targets.add(_canonical_target(resource))

        conditions: list[z3.BoolRef] = [has_inverse]

        # Target count equality
        conditions.append(z3.IntVal(len(inv_targets)) == z3.IntVal(len(plan_targets)))

        # Forward containment: every plan target must be in inverse targets
        for p in sorted(plan_targets):
            if not inv_targets:
                conditions.append(z3.BoolVal(False))
            else:
                conditions.append(
                    z3.Or([z3.StringVal(p) == z3.StringVal(i) for i in sorted(inv_targets)])
                )

        # Reverse containment: every inverse target must be in plan targets
        for i in sorted(inv_targets):
            if not plan_targets:
                conditions.append(z3.BoolVal(False))
            else:
                conditions.append(
                    z3.Or([z3.StringVal(i) == z3.StringVal(p) for p in sorted(plan_targets)])
                )

        return z3.And(*conditions)


__all__ = ["K9Reversibility"]
