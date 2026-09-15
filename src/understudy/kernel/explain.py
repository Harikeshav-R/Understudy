"""Safety kernel veto explanation and actionable prose rendering.

Implements build-plan step B4.6:
Render a safety kernel veto into prose a human on-call can act on —
identifying which invariant was violated, which facts caused it, what the
concrete counterexample was (extracting from Z3 solver models), and actionable
guidance for incident operators.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from understudy.contracts.enums import ActionType, InvariantTier
from understudy.kernel.invariants.k05_single_writer import _canonical_target

if TYPE_CHECKING:
    import z3

    from understudy.contracts.plan import RemediationPlan
    from understudy.kernel.dsl import Invariant, KernelContext


class VetoExplanation(BaseModel):
    """Structured explanation of a formal safety kernel invariant veto."""

    model_config = ConfigDict(frozen=True)

    invariant_id: str
    invariant_name: str
    tier: InvariantTier = InvariantTier.PROOF
    statement: str
    failing_facts: dict[str, Any] = Field(default_factory=dict)
    counterexample: dict[str, Any] = Field(default_factory=dict)
    unsat_core: list[str] = Field(default_factory=list)
    human_reason: str
    guidance: str
    actionable_prose: str


def _safe_eval_model(
    model: z3.ModelRef | None,
    constant: z3.ExprRef | None,
    fallback_val: Any,
) -> Any:
    """Safely extract evaluated value from Z3 model if available."""
    if model is None or constant is None:
        return fallback_val
    try:
        val = model.eval(constant)
        if hasattr(val, "as_decimal"):
            return float(str(val.as_decimal(5)).rstrip("?"))
        if hasattr(val, "as_long"):
            return val.as_long()
        return fallback_val
    except Exception:
        return fallback_val


def format_actionable_prose(
    *,
    invariant_id: str,
    invariant_name: str,
    statement: str,
    tier: InvariantTier,
    human_reason: str,
    failing_facts: dict[str, Any],
    counterexample: dict[str, Any],
    guidance: str,
) -> str:
    """Format structured veto explanation into clean multi-line actionable prose."""
    lines: list[str] = [
        f"VETO: Invariant {invariant_id} ({invariant_name}) Violated",
        "",
        "Summary:",
        f"  {human_reason}",
        "",
        "Invariant Definition:",
        f"  Statement: {statement.strip()}",
        f"  Tier: {tier.value}",
        "",
        "Failing Facts:",
    ]
    if failing_facts:
        for k, v in failing_facts.items():
            lines.append(f"  - {k}: {v}")
    else:
        lines.append("  - (none recorded)")

    lines.extend(["", "Counterexample:"])
    if counterexample:
        for k, v in counterexample.items():
            lines.append(f"  - {k}: {v}")
    else:
        lines.append("  - (none recorded)")

    lines.extend(["", "Actionable Operator Guidance:", f"  {guidance}"])
    return "\n".join(lines)


def explain_veto(
    inv: Invariant | str,
    plan: RemediationPlan,
    ctx: KernelContext,
    model: z3.ModelRef | None = None,
) -> VetoExplanation:
    """Construct a comprehensive VetoExplanation from an invariant violation.

    Extracts concrete counterexample values from the Z3 solver model when present,
    identifies the exact contributing system facts, generates operator guidance,
    and produces both the single-line summary (human_reason) and multi-line actionable prose.

    Args:
        inv: The violated safety Invariant instance or invariant ID string.
        plan: The candidate RemediationPlan that was evaluated.
        ctx: The KernelContext holding observed facts and solver constants.
        model: The Z3 solver model produced when negation was satisfiable.

    Returns:
        VetoExplanation containing structured breakdown and actionable prose.
    """
    if isinstance(inv, str):
        from understudy.kernel.verify import PROOF_INVARIANTS

        inv_id = inv
        matching = next((i for i in PROOF_INVARIANTS if i.id == inv_id), None)
        if matching is not None:
            inv = matching
            inv_name = getattr(inv, "name", inv_id)
            statement = inv.statement
            tier = inv.tier
        else:
            inv_name = inv_id
            tier = InvariantTier.PROOF
            statement = f"Unknown invariant {inv_id} violated."
    else:
        inv_id = inv.id
        inv_name = getattr(inv, "name", inv_id)
        statement = inv.statement
        tier = inv.tier

    failing_facts: dict[str, Any] = {}
    counterexample: dict[str, Any] = {}
    unsat_core: list[str] = []
    human_reason = ""
    guidance = ""

    if inv_id == "K1":
        workloads: set[str] = set()
        if plan.params.workload:
            workloads.add(plan.params.workload)
        for target in plan.target_resources:
            if target.kind == "Deployment":
                workloads.add(target.name)

        reasons: list[str] = []
        guidance_items: list[str] = []

        for svc in sorted(workloads):
            min_reps_raw = ctx.get_int(f"min_replicas[{svc}]")
            reps_raw = ctx.get_int(f"replicas[{svc}]")
            healthy_raw = ctx.get_int(f"healthy_replicas[{svc}]")

            min_reps = _safe_eval_model(
                model, ctx.constants.get(f"min_replicas[{svc}]"), min_reps_raw
            )
            reps = _safe_eval_model(model, ctx.constants.get(f"replicas[{svc}]"), reps_raw)
            healthy_reps = _safe_eval_model(
                model, ctx.constants.get(f"healthy_replicas[{svc}]"), healthy_raw
            )

            failing_facts[f"min_replicas[{svc}]"] = min_reps
            failing_facts[f"replicas[{svc}]"] = reps
            failing_facts[f"healthy_replicas[{svc}]"] = healthy_reps

            delta = 0
            if plan.action == ActionType.SCALE_WORKLOAD and plan.params.workload == svc:
                delta = plan.params.replica_delta or 0
            elif plan.action == ActionType.RESTART_WORKLOAD:
                delta = -1

            post_reps = reps + delta
            post_healthy = healthy_reps + delta

            if post_reps < min_reps or post_healthy < min_reps:
                reasons.append(
                    f"scaling/restarting '{svc}' leaves post-intervention replicas ({post_reps}) "
                    f"or healthy replicas ({post_healthy}) below minimum floor ({min_reps})"
                )
                unsat_core.append(
                    f"healthy_replicas[{svc}] ({post_healthy}) < min_replicas[{svc}] ({min_reps})"
                )
                counterexample[svc] = {
                    "min_replicas": min_reps,
                    "pre_intervention_replicas": reps,
                    "pre_intervention_healthy": healthy_reps,
                    "post_intervention_replicas": post_reps,
                    "post_intervention_healthy": post_healthy,
                    "post_replicas": post_reps,
                    "post_healthy": post_healthy,
                    "delta": delta,
                }
                guidance_items.append(
                    f"Workload '{svc}' requires at least {min_reps} healthy replica(s). "
                    f"Action delta ({delta}) causes replicas to drop to {post_healthy}. "
                    "Do not down-scale or rolling-restart during partial outage; "
                    "adjust replica delta."
                )

        detail = "; ".join(reasons)
        human_reason = f"VETO: Invariant K1 violated: {detail}"
        guidance = " ".join(guidance_items)

    elif inv_id == "K2":
        auth_ns = ctx.get_str("authorized_namespace")
        failing_facts["authorized_namespace"] = auth_ns

        target_namespaces: set[str] = {str(ns) for ns in ctx.get_set("plan_target_namespaces")}
        for resource in plan.target_resources:
            if resource.namespace:
                target_namespaces.add(resource.namespace)
        failing_facts["plan_target_namespaces"] = sorted(target_namespaces)

        unauthorized = sorted(target_namespaces - {auth_ns})
        unauth_str = ", ".join(unauthorized)
        human_reason = (
            f"VETO: Invariant K2 violated: plan mutates unauthorized namespace(s): {unauth_str}, "
            f"authorized is '{auth_ns}'"
        )
        unsat_core = [f"namespace({ns}) != authorized_namespace({auth_ns})" for ns in unauthorized]
        counterexample["unauthorized_namespaces"] = unauthorized
        counterexample["authorized_namespace"] = auth_ns
        guidance = (
            f"Remediation plan attempts to mutate resource(s) in unauthorized namespace(s): "
            f"'{unauth_str}'. Automated execution is strictly confined to '{auth_ns}'. "
            f"Ensure plan target resources do not span beyond '{auth_ns}'."
        )

    elif inv_id == "K3":
        target_commit = plan.params.target_commit or "unknown"
        last_mig_commit = (
            ctx.get_str("last_migration_commit")
            if ctx.has_fact("last_migration_commit")
            else "migration"
        )
        last_mig_time = ctx.get_datetime("last_migration_commit_time")
        failing_facts["last_migration_commit"] = last_mig_commit
        failing_facts["last_migration_commit_time"] = last_mig_time.isoformat()

        if target_commit != "unknown" and ctx.has_fact(
            f"target_contains_migration[{target_commit}]"
        ):
            has_mig = ctx.get_bool(f"target_contains_migration[{target_commit}]")
        else:
            has_mig = ctx.get_bool("target_contains_migration")

        if has_mig:
            failing_facts[f"target_contains_migration[{target_commit}]"] = True
            human_reason = (
                f"VETO: Invariant K3 violated: rollback target commit {target_commit} "
                f"contains a schema migration"
            )
            unsat_core = [f"target_contains_migration({target_commit}) == True"]
            counterexample["target_commit"] = target_commit
            counterexample["target_contains_migration"] = True
            guidance = (
                f"Rollback target commit '{target_commit}' contains database schema migrations. "
                f"Rolling back directly to a migration commit will cause schema-code mismatch. "
                f"Revert application code separately from schema migrations."
            )
        else:
            if target_commit != "unknown" and ctx.has_fact(
                f"rollback_target_commit_time[{target_commit}]"
            ):
                target_time = ctx.get_datetime(f"rollback_target_commit_time[{target_commit}]")
            else:
                target_time = ctx.get_datetime("rollback_target_commit_time")

            failing_facts[f"rollback_target_commit_time[{target_commit}]"] = target_time.isoformat()
            human_reason = (
                f"VETO: Invariant K3 violated: rollback target commit {target_commit} deployed at "
                f"{target_time.isoformat()} predates schema migration commit {last_mig_commit} at "
                f"{last_mig_time.isoformat()}"
            )
            unsat_core = [
                f"rollback_target_commit_time ({target_time.isoformat()}) < "
                f"last_migration_commit_time ({last_mig_time.isoformat()})"
            ]
            counterexample["rollback_target_commit"] = target_commit
            counterexample["rollback_target_commit_time"] = target_time.isoformat()
            counterexample["last_migration_commit"] = last_mig_commit
            counterexample["last_migration_commit_time"] = last_mig_time.isoformat()
            guidance = (
                f"Rollback target commit '{target_commit}' deployed at {target_time.isoformat()} "
                f"predates migration commit '{last_mig_commit}' ({last_mig_time.isoformat()}). "
                f"Rolling application code back past this migration boundary would execute code "
                f"incompatible with the current database schema. Select a target commit after "
                f"{last_mig_time.isoformat()} or roll back database migrations first."
            )

    elif inv_id == "K4":
        declared = ctx.get_set("declared_blast_set")
        observed = ctx.get_set("observed_blast_set")
        declared_str_set = {str(s) for s in declared}
        observed_str_set = {str(s) for s in observed}

        failing_facts["declared_blast_set"] = sorted(declared_str_set)
        failing_facts["observed_blast_set"] = sorted(observed_str_set)

        if not observed_str_set.issubset(declared_str_set):
            unpredicted = sorted(observed_str_set - declared_str_set)
            human_reason = (
                f"VETO: Invariant K4 violated: observed blast set touched unpredicted service(s): "
                f"{', '.join(unpredicted)}"
            )
            unsat_core = [f"observed service '{s}' not in declared blast set" for s in unpredicted]
            counterexample["unpredicted_observed_services"] = unpredicted
            guidance = (
                f"Twin rehearsal affected service(s) ({', '.join(unpredicted)}) not declared in "
                "the candidate's blast set. Investigate unanticipated dependencies "
                "before actuating."
            )
        else:
            human_reason = (
                "VETO: Invariant K4 violated: declared blast set exceeds reachable "
                "dependency graph dependents"
            )
            unsat_core = ["declared_blast_set not subset of reachable dependents"]
            counterexample["declared_blast_set"] = sorted(declared_str_set)
            guidance = (
                "Declared blast set includes services beyond the reachable dependency "
                "graph closure. Verify dependencies.yaml and target service topology."
            )

    elif inv_id == "K5":
        plan_targets: set[str] = {_canonical_target(r) for r in plan.target_resources}
        raw_pts = ctx.get_raw("plan_targets") if ctx.has_fact("plan_targets") else []
        pt_list = list(raw_pts) if isinstance(raw_pts, (list, tuple, set, frozenset)) else [raw_pts]
        plan_targets.update(_canonical_target(t) for t in pt_list)

        raw_in_flight = (
            ctx.get_raw("in_flight_plan_targets") if ctx.has_fact("in_flight_plan_targets") else []
        )
        inf_list = (
            list(raw_in_flight)
            if isinstance(raw_in_flight, (list, tuple, set, frozenset))
            else [raw_in_flight]
        )
        in_flight: set[str] = {_canonical_target(t) for t in inf_list}

        failing_facts["plan_targets"] = sorted(plan_targets)
        failing_facts["in_flight_plan_targets"] = sorted(in_flight)

        colliding = sorted(plan_targets & in_flight)
        col_str = ", ".join(colliding)
        human_reason = (
            f"VETO: Invariant K5 violated: target resource(s) already held by an in-flight "
            f"execution: {col_str}"
        )
        unsat_core = [f"target '{c}' in in_flight_plan_targets" for c in colliding]
        counterexample["colliding_resources"] = colliding
        guidance = (
            f"Target resource(s) ({col_str}) are currently locked by an active remediation "
            "or shadow run. Wait for in-flight execution to complete or terminate the "
            "conflicting run."
        )

    elif inv_id == "K7":
        mutations_raw = ctx.get_int("prod_mutations_in_window")
        budget_raw = ctx.get_int("mutation_budget")

        mutations = _safe_eval_model(
            model, ctx.constants.get("prod_mutations_in_window"), mutations_raw
        )
        budget = _safe_eval_model(model, ctx.constants.get("mutation_budget"), budget_raw)

        failing_facts["prod_mutations_in_window"] = mutations
        failing_facts["mutation_budget"] = budget

        human_reason = (
            f"VETO: Invariant K7 violated: production mutations in 15-minute window "
            f"({mutations} + 1) exceeds mutation budget ({budget})"
        )
        unsat_core = [f"prod_mutations_in_window ({mutations}) + 1 > mutation_budget ({budget})"]
        counterexample["prod_mutations_in_window"] = mutations
        counterexample["mutation_budget"] = budget
        counterexample["projected_mutations"] = mutations + 1
        guidance = (
            f"Production mutation budget of {budget} changes per 15-minute window has been "
            f"reached ({mutations} recorded). Automated actuation is halted to prevent "
            "remediation thrashing. Investigate root cause or wait for mutation rate window "
            "to reset."
        )

    elif inv_id == "K8":
        age_raw = ctx.get_float("evidence_age_seconds")
        samples_raw = ctx.get_int("probe_sample_count")
        drop_raw = ctx.get_float("max_drop_ratio")

        age = _safe_eval_model(model, ctx.constants.get("evidence_age_seconds"), age_raw)
        samples = _safe_eval_model(model, ctx.constants.get("probe_sample_count"), samples_raw)
        drop = _safe_eval_model(model, ctx.constants.get("max_drop_ratio"), drop_raw)

        issues: list[str] = []
        if age > 300.0:
            issues.append(f"evidence age {age:.1f}s exceeds 300s limit")
            failing_facts["evidence_age_seconds"] = age
            counterexample["evidence_age_seconds"] = age
        if samples < 60:
            issues.append(f"probe samples ({samples}) below 60 minimum")
            failing_facts["probe_sample_count"] = samples
            counterexample["probe_sample_count"] = samples
        if drop > 0.05:
            issues.append(f"max drop ratio ({drop:.3f}) exceeds 0.05 ceiling")
            failing_facts["max_drop_ratio"] = drop
            counterexample["max_drop_ratio"] = drop

        human_reason = f"VETO: Invariant K8 violated: {'; '.join(issues)}"
        unsat_core = issues
        guidance = (
            f"Tournament rehearsal evidence failed fidelity criteria: {'; '.join(issues)}. "
            f"Re-run twin tournament with fresh mirror traffic before applying fix."
        )

    elif inv_id == "K9":
        has_inverse = ctx.get_bool("plan_has_inverse")
        raw_inv = ctx.get_raw("inverse_targets") if ctx.has_fact("inverse_targets") else []
        inv_list = (
            list(raw_inv) if isinstance(raw_inv, (list, tuple, set, frozenset)) else [raw_inv]
        )
        inv_targets = {_canonical_target(t) for t in inv_list}

        raw_pts = ctx.get_raw("plan_targets") if ctx.has_fact("plan_targets") else []
        pt_list = list(raw_pts) if isinstance(raw_pts, (list, tuple, set, frozenset)) else [raw_pts]
        plan_targets = {_canonical_target(t) for t in pt_list}

        failing_facts["plan_has_inverse"] = has_inverse
        failing_facts["inverse_targets"] = sorted(inv_targets)
        failing_facts["plan_targets"] = sorted(plan_targets)

        human_reason = (
            "VETO: Invariant K9 violated: plan lacks a deterministic inverse or inverse "
            "targets do not strictly match plan targets"
        )
        unsat_core = ["plan_has_inverse == False or inverse_targets != plan_targets"]
        counterexample["plan_has_inverse"] = has_inverse
        counterexample["inverse_targets"] = sorted(inv_targets)
        counterexample["plan_targets"] = sorted(plan_targets)
        guidance = (
            "Plan lacks a valid deterministic inverse or inverse targets do not match "
            "plan targets. Automated safety requires every non-trivial plan to be reversible."
        )

    else:
        human_reason = f"VETO: Invariant {inv_id} violated: {statement}"
        unsat_core = [f"{inv_id} negation satisfied in solver model"]
        counterexample["invariant_id"] = inv_id
        counterexample["statement"] = statement
        guidance = f"Review invariant {inv_id} constraint specification."

    actionable_prose = format_actionable_prose(
        invariant_id=inv_id,
        invariant_name=inv_name,
        statement=statement,
        tier=tier,
        human_reason=human_reason,
        failing_facts=failing_facts,
        counterexample=counterexample,
        guidance=guidance,
    )

    return VetoExplanation(
        invariant_id=inv_id,
        invariant_name=inv_name,
        tier=tier,
        statement=statement,
        failing_facts=failing_facts,
        counterexample=counterexample,
        unsat_core=unsat_core,
        human_reason=human_reason,
        guidance=guidance,
        actionable_prose=actionable_prose,
    )


def render_veto_reason(
    inv: Invariant,
    plan: RemediationPlan,
    ctx: KernelContext,
    model: z3.ModelRef | None = None,
) -> tuple[str, list[str]]:
    """Render an invariant violation into human_reason prose and unsat_core list.

    Maintains full backwards compatibility with existing verification and test assertions
    by delegating to explain_veto.
    """
    explanation = explain_veto(inv=inv, plan=plan, ctx=ctx, model=model)
    return explanation.human_reason, explanation.unsat_core


__all__ = [
    "VetoExplanation",
    "explain_veto",
    "format_actionable_prose",
    "render_veto_reason",
]
