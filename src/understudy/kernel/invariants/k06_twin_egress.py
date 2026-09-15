"""Invariant K6: Twin egress containment runtime invariant.

Enforces that no twin environment may reach ust-prod or the public internet.
Enforced at fork time via NetworkPolicy, egress-stub redirection, and in-service middleware.
"""

from collections.abc import Sequence

import z3

from understudy.contracts.enums import InvariantTier
from understudy.kernel.dsl import Invariant, KernelContext


class K6TwinEgressContainment(Invariant):
    """Runtime invariant asserting twin egress network containment."""

    id: str = "K6"
    name: str = "Twin egress containment"
    tier: InvariantTier = InvariantTier.RUNTIME
    tier_display: str = "RUNTIME (policy-backed)"
    statement: str = "No twin may reach `ust-prod` or the public internet."
    required_facts: Sequence[str] = ("twin_egress_policy_present",)
    enforcement: str = (
        "NetworkPolicy at fork time; `egress-stub` redirection; in-service\n"
        "middleware. The kernel asserts `twin_egress_policy_present` as a "
        "precondition for accepting\n"
        "any fork, and the fleet controller refuses to mark a twin ready without it."
    )
    why_runtime: str = (
        "Proving a NetworkPolicy denies a flow requires\n"
        "modelling the CNI's policy semantics, which is a research project. We check that "
        "the policy\n"
        "object exists and matches a golden spec, and we test the denial empirically in\n"
        "`tests/integration/test_twin_egress_denied.py`. The brief says exactly this."
    )
    why_runtime_label: str = "Why it is RUNTIME and not PROOF."
    why_it_exists: str | None = None
    smt_shape: str | None = None

    def build(self, ctx: KernelContext) -> z3.BoolRef:
        """K6 is a runtime invariant evaluated at fork/execution time, not proved in SMT."""
        _ = ctx
        raise NotImplementedError(
            "Runtime invariants are evaluated at actuation/execution time, not proved in SMT."
        )


__all__ = ["K6TwinEgressContainment"]
