"""Invariant K3: Migration boundary formal proof verification.

Implements build-plan step B4.3c:
A ROLLBACK_DEPLOY may not target a commit that predates the most recent schema
migration, and may not itself be a commit containing a migration.
"""

from collections.abc import Sequence

import z3

from understudy.contracts.enums import ActionType, InvariantTier
from understudy.kernel.dsl import Invariant, KernelContext


class K3MigrationBoundary(Invariant):
    """Safety invariant proving that rollback plans do not violate migration boundaries."""

    id: str = "K3"
    tier: InvariantTier = InvariantTier.PROOF
    statement: str = (
        "A ROLLBACK_DEPLOY may not target a commit that predates the most recent "
        "schema migration, and may not itself be a commit containing a migration."
    )
    required_facts: Sequence[str] = (
        "last_migration_commit_time",
        "rollback_target_commit_time",
        "target_contains_migration",
    )

    def build(self, ctx: KernelContext) -> z3.BoolRef:
        """Build SMT formula asserting rollback target respects migration boundaries.

        SMT shape (docs/03-invariants.md §3.4):
        plan.action = ROLLBACK_DEPLOY ⟹
            rollback_target_commit_time ≥ last_migration_commit_time
          ∧ ¬target_contains_migration

        1. If plan.action is not ROLLBACK_DEPLOY, the implication is vacuously true,
           returning z3.BoolVal(True) without requiring rollback-specific facts.
        2. If plan.action is ROLLBACK_DEPLOY:
           - Retrieves 'last_migration_commit_time' (timestamp as Real)
           - Retrieves 'rollback_target_commit_time' (timestamp as Real)
           - Retrieves 'target_contains_migration' (Bool)
           Any missing fact raises MissingFact immediately (Rule 5.6 zero defaults).
        3. Returns z3.And(
               rollback_target_commit_time >= last_migration_commit_time,
               z3.Not(target_contains_migration),
           )
        """
        if ctx.plan.action != ActionType.ROLLBACK_DEPLOY:
            return z3.BoolVal(True)

        last_migration = ctx.datetime("last_migration_commit_time")
        target_commit_time = ctx.datetime("rollback_target_commit_time")
        target_has_migration = ctx.bool("target_contains_migration")

        return z3.And(
            target_commit_time >= last_migration,
            z3.Not(target_has_migration),
        )


__all__ = ["K3MigrationBoundary"]
