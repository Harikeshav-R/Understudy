"""Invariant K7: Production mutation budget formal proof verification.

Implements build-plan step B4.3g:
At most mutation_budget (default 3) production mutations in any rolling
15-minute window.
"""

from collections.abc import Sequence

import z3

from understudy.contracts.enums import ActionType, InvariantTier
from understudy.kernel.dsl import Invariant, KernelContext


class K7MutationBudget(Invariant):
    """Safety invariant proving that candidate plans do not exceed the mutation budget.

    SMT shape (docs/03-invariants.md §3.4):
    prod_mutations_in_window + 1 ≤ mutation_budget
    """

    id: str = "K7"
    tier: InvariantTier = InvariantTier.PROOF
    statement: str = (
        "At most `mutation_budget` (default 3) production mutations in any rolling "
        "15-minute window."
    )
    required_facts: Sequence[str] = (
        "prod_mutations_in_window",
        "mutation_budget",
    )

    def build(self, ctx: KernelContext) -> z3.BoolRef:
        """Build SMT formula asserting post-execution mutation budget adherence.

        SMT shape (docs/03-invariants.md §3.4):
        prod_mutations_in_window + 1 <= mutation_budget

        1. NO_ACTION performs zero mutations against production and trivially
           satisfies K7, returning z3.BoolVal(True).
        2. For mutating actions (ROLLBACK_DEPLOY, RESTART_WORKLOAD, SCALE_WORKLOAD,
           DISABLE_FLAG, REVERT_CONFIG):
           - Retrieves 'prod_mutations_in_window' (Int constant asserted to fact value)
           - Retrieves 'mutation_budget' (Int constant asserted to fact value)
           Missing facts raise MissingFact immediately (Rule 5.6 zero defaults).
        3. Asserts:
           prod_mutations_in_window + 1 <= mutation_budget
        """
        if ctx.plan.action == ActionType.NO_ACTION:
            return z3.BoolVal(True)

        mutations = ctx.int("prod_mutations_in_window")
        budget = ctx.int("mutation_budget")

        return mutations + z3.IntVal(1) <= budget


__all__ = ["K7MutationBudget"]
