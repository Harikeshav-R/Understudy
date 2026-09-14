"""Invariant K4: Blast-radius containment formal proof verification.

Implements build-plan step B4.3e:
A plan's observed blast set must be a subset of its declared blast set, and its
declared blast set must be a subset of the dependency-graph reachable set of its
targets.
"""

from collections.abc import Sequence

import z3

from understudy.contracts.enums import ActionType, InvariantTier
from understudy.contracts.plan import ResourceRef
from understudy.kernel.dsl import Invariant, KernelContext


class K4BlastContainment(Invariant):
    """Safety invariant proving that blast radius is strictly contained.

    SMT shape (docs/03-invariants.md §3.4):
    observed_blast_set <= declared_blast_set <= union_{t in plan_targets} dependents*(service(t))
    """

    id: str = "K4"
    tier: InvariantTier = InvariantTier.PROOF
    statement: str = (
        "A plan's observed blast set must be a subset of its declared blast set, "
        "and its declared blast set must be a subset of the dependency-graph "
        "reachable set of its targets."
    )
    required_facts: Sequence[str] = (
        "dependents[svc]",
        "declared_blast_set",
        "observed_blast_set",
    )

    def build(self, ctx: KernelContext) -> z3.BoolRef:
        """Build SMT formula asserting blast radius containment.

        Discharges the two-stage subset containment condition:
        1. observed_blast_set <= declared_blast_set:
           The actual blast measured during twin rehearsal must not touch any service
           that was not explicitly declared by the plan.
        2. declared_blast_set <= union_{t in plan_targets} dependents*(service(t)):
           The predicted blast declared by the plan must be a subset of the structural
           transitive dependents (plus target itself) derived from the dependency graph.

        Zero-default safety (Rule 5.6):
        - Missing 'declared_blast_set' raises MissingFact.
        - Missing 'observed_blast_set' raises MissingFact.
        - Missing 'dependents[svc]' for any target service raises MissingFact.
        - Vacuously true for NO_ACTION with empty facts and no declared blast.
        """
        if (
            ctx.plan.action == ActionType.NO_ACTION
            and not ctx.has_fact("declared_blast_set")
            and not ctx.has_fact("observed_blast_set")
            and not ctx.plan.declared_blast_set
        ):
            return z3.BoolVal(True)

        raw_declared = ctx.get_set("declared_blast_set")
        raw_observed = ctx.get_set("observed_blast_set")

        declared_blast: set[str] = {str(s).strip() for s in raw_declared}
        for s in ctx.plan.declared_blast_set:
            declared_blast.add(s.strip())

        observed_blast: set[str] = {str(s).strip() for s in raw_observed}

        target_services: set[str] = set()
        if ctx.plan.params.workload:
            target_services.add(ctx.plan.params.workload.strip())
        for target in ctx.plan.target_resources:
            if target.kind == "Deployment":
                target_services.add(target.name.strip())

        if ctx.has_fact("plan_targets"):
            raw_targets = ctx.get_raw("plan_targets")
            target_list = (
                list(raw_targets)
                if isinstance(raw_targets, (list, tuple, set, frozenset))
                else [raw_targets]
            )
            for t in target_list:
                if isinstance(t, ResourceRef):
                    if t.kind == "Deployment":
                        target_services.add(t.name.strip())
                elif isinstance(t, dict) and t.get("kind") == "Deployment" and "name" in t:
                    target_services.add(str(t["name"]).strip())

        reachable_set: set[str] = set()
        for svc in sorted(target_services):
            deps = ctx.get_set(f"dependents[{svc}]")
            reachable_set.add(svc)
            reachable_set.update(str(d).strip() for d in deps)

        conditions: list[z3.BoolRef] = []

        # Condition 1: observed_blast_set ⊆ declared_blast_set
        for o in sorted(observed_blast):
            if not declared_blast:
                conditions.append(z3.BoolVal(False))
            else:
                conditions.append(
                    z3.Or([z3.StringVal(o) == z3.StringVal(d) for d in sorted(declared_blast)])
                )

        # Condition 2: declared_blast_set ⊆ reachable_set
        for d in sorted(declared_blast):
            if not reachable_set:
                conditions.append(z3.BoolVal(False))
            else:
                conditions.append(
                    z3.Or([z3.StringVal(d) == z3.StringVal(r) for r in sorted(reachable_set)])
                )

        if not conditions:
            return z3.BoolVal(True)
        if len(conditions) == 1:
            return conditions[0]
        return z3.And(*conditions)


__all__ = ["K4BlastContainment"]
