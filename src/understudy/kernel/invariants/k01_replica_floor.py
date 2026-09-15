"""Invariant K1: Replica floor formal proof verification.

Implements build-plan step B4.3a:
No plan may leave any service with fewer healthy replicas than its configured
minimum, accounting for the plan's own effect.
"""

from collections.abc import Sequence

import z3

from understudy.contracts.enums import ActionType, InvariantTier
from understudy.kernel.dsl import Invariant, KernelContext


class K1ReplicaFloor(Invariant):
    """Safety invariant proving that candidate plans maintain replica minimums."""

    id: str = "K1"
    name: str = "Replica floor"
    tier: InvariantTier = InvariantTier.PROOF
    tier_display: str = "PROOF"
    statement: str = (
        "No plan may leave any service with fewer healthy replicas than its\n"
        "configured minimum, accounting for the plan's own effect."
    )
    required_facts: Sequence[str] = (
        "replicas[svc]",
        "min_replicas[svc]",
        "healthy_replicas[svc]",
    )
    smt_shape: str | None = (
        "```\n"
        "∀ svc ∈ services:\n"
        "    post_replicas[svc] = replicas[svc] + delta(plan, svc)\n"
        "    assert post_replicas[svc] ≥ min_replicas[svc]\n"
        "```\n"
        "where `delta` is non-zero only for `SCALE_WORKLOAD` on the target, and for\n"
        "`RESTART_WORKLOAD` is modelled as a transient reduction of `maxUnavailable` from the\n"
        "Deployment's rolling-update strategy — that transient is what makes restart unsafe "
        "during a\n"
        "partial outage, and modelling it is the point."
    )
    why_it_exists: str | None = (
        'The obvious "scale down to stop the error storm" candidate is exactly\n'
        "the kind of empirically-attractive, operationally-fatal fix a tournament will rank highly."
    )

    def build(self, ctx: KernelContext) -> z3.BoolRef:
        """Build SMT formula asserting post-execution replica floor adherence.

        The SMT encoding models both desired replicas and transient healthy replica
        reductions across all workloads targeted by the plan:
        1. SCALE_WORKLOAD adds replica_delta to both desired and healthy counts.
        2. RESTART_WORKLOAD models Kubernetes rolling-update semantics where
           up to maxUnavailable (1) pod is taken down concurrently. If healthy
           replicas were already degraded (partial outage), the transient reduction
           can cause healthy instances to fall below min_replicas, triggering a VETO.
        3. Other action types (NO_ACTION, ROLLBACK_DEPLOY, DISABLE_FLAG, REVERT_CONFIG)
           do not alter replica counts (delta = 0).
        """
        plan = ctx.plan

        # Determine all distinct Deployment workloads affected by this plan
        services: set[str] = set()
        if plan.params.workload:
            services.add(plan.params.workload)
        for target in plan.target_resources:
            if target.kind == "Deployment":
                services.add(target.name)

        # Plans not mutating any deployment workloads trivially satisfy K1
        if not services:
            return z3.BoolVal(True)

        conditions: list[z3.BoolRef] = []

        for svc in sorted(services):
            # Missing facts raise MissingFact immediately (Rule 5.6 zero defaults)
            min_reps = ctx.int(f"min_replicas[{svc}]")
            reps = ctx.int(f"replicas[{svc}]")
            healthy_reps = ctx.int(f"healthy_replicas[{svc}]")

            delta_replicas = 0
            delta_healthy = 0

            if plan.action == ActionType.SCALE_WORKLOAD and plan.params.workload == svc:
                rep_delta = (
                    plan.params.replica_delta if plan.params.replica_delta is not None else 0
                )
                delta_replicas = rep_delta
                delta_healthy = rep_delta
            elif plan.action == ActionType.RESTART_WORKLOAD and (
                plan.params.workload == svc
                or any(t.kind == "Deployment" and t.name == svc for t in plan.target_resources)
            ):
                # Kubernetes rolling restart transient: maxUnavailable
                max_unavail = (
                    ctx.get_int(f"max_unavailable[{svc}]")
                    if ctx.has_fact(f"max_unavailable[{svc}]")
                    else 1
                )
                delta_replicas = 0
                delta_healthy = -max_unavail

            post_replicas = reps + z3.IntVal(delta_replicas)
            post_healthy = healthy_reps + z3.IntVal(delta_healthy)

            conditions.append(
                z3.And(
                    post_replicas >= min_reps,
                    post_healthy >= min_reps,
                )
            )

        if len(conditions) == 1:
            return conditions[0]
        return z3.And(*conditions)


__all__ = ["K1ReplicaFloor"]
