"""Invariant K10: Actuation authorisation runtime invariant.

Enforces that the actuator applies to ust-prod only when holding a KernelVerdict with
verdict == PASS whose plan_id matches the plan it is applying and whose age is under 60 seconds.
"""

from collections.abc import Sequence

import z3

from understudy.contracts.enums import InvariantTier
from understudy.kernel.dsl import Invariant, KernelContext


class K10ActuationAuthorisation(Invariant):
    """Runtime invariant asserting actuation authorisation preconditions."""

    id: str = "K10"
    name: str = "Actuation authorisation"
    tier: InvariantTier = InvariantTier.RUNTIME
    tier_display: str = "RUNTIME"
    statement: str = (
        "The actuator applies to `ust-prod` only when it holds a `KernelVerdict` with\n"
        "`verdict == PASS` whose `plan_id` matches the plan it is applying and whose "
        "age is under 60\n"
        "seconds."
    )
    required_facts: Sequence[str] = ()
    enforcement: str = (
        "Asserted in `Actuator.apply_to_production`, tested in\n"
        "`tests/unit/test_actuator_requires_pass.py`, and covered by the offline TLA+ "
        "protocol spec\n"
        "if P2 lands."
    )
    why_runtime: str = (
        "It is a property of the program's control flow, not of the cluster\n"
        "state, so it belongs to the code and the protocol spec rather than to Z3."
    )
    why_runtime_label: str = "Why it is RUNTIME."
    why_it_exists: str | None = None
    smt_shape: str | None = None

    def build(self, ctx: KernelContext) -> z3.BoolRef:
        """K10 is a runtime invariant asserted during actuation execution, not proved in SMT."""
        _ = ctx
        raise NotImplementedError(
            "Runtime invariants are evaluated at actuation/execution time, not proved in SMT."
        )


__all__ = ["K10ActuationAuthorisation"]
