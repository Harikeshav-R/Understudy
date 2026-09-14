"""Invariant K2: Namespace scope formal proof verification.

Implements build-plan step B4.3b:
Every resource a plan mutates lies in the single authorized namespace for its
execution context. A plan destined for production touches only `ust-prod`; a plan
destined for a twin touches only that twin's namespace.
"""

from collections.abc import Sequence

import z3

from understudy.contracts.enums import InvariantTier
from understudy.kernel.dsl import Invariant, KernelContext


class K2NamespaceScope(Invariant):
    """Safety invariant proving that candidate plans only mutate authorized namespaces."""

    id: str = "K2"
    tier: InvariantTier = InvariantTier.PROOF
    statement: str = (
        "Every resource a plan mutates lies in the single authorized namespace for its "
        "execution context. A plan destined for production touches only `ust-prod`; "
        "a plan destined for a twin touches only that twin's namespace."
    )
    required_facts: Sequence[str] = (
        "plan_target_namespaces",
        "authorized_namespace",
    )

    def build(self, ctx: KernelContext) -> z3.BoolRef:
        """Build SMT formula asserting all mutated resources match the authorized namespace.

        Enforces ADR-002 machine-checked namespace boundary:
        1. Retrieves authorized_namespace string constant (e.g. 'ust-prod' or 'ust-twin-*').
           Missing fact raises MissingFact immediately (Rule 5.6 zero defaults).
        2. Retrieves plan_target_namespaces fact set. Missing fact raises MissingFact.
        3. Collects all candidate target namespaces from the fact base and any resources
           explicitly listed in ctx.plan.target_resources.
        4. If no targets are mutated, returns z3.BoolVal(True).
        5. For each distinct target namespace, asserts z3.StringVal(ns) == auth_ns.
        """
        auth_ns = ctx.string("authorized_namespace")
        raw_target_ns = ctx.get_set("plan_target_namespaces")

        target_namespaces: set[str] = {str(ns) for ns in raw_target_ns}
        for resource in ctx.plan.target_resources:
            if resource.namespace:
                target_namespaces.add(resource.namespace)

        if not target_namespaces:
            return z3.BoolVal(True)

        conditions: list[z3.BoolRef] = [
            z3.StringVal(ns) == auth_ns for ns in sorted(target_namespaces)
        ]

        if len(conditions) == 1:
            return conditions[0]
        return z3.And(*conditions)


__all__ = ["K2NamespaceScope"]
