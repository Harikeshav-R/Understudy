"""Safety kernel node: evaluates formal Z3 invariants on winning remediation plan."""

from typing import Any

from understudy.common.logging import get_logger
from understudy.contracts.enums import KernelVerdictType, RunOutcome
from understudy.contracts.kernel import Fact
from understudy.kernel.api import WorkloadReaderFactAdapter, extract_facts
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def safety_kernel(state: State, deps: Deps) -> dict[str, Any]:
    """Formally verify safety invariants for the tournament winner."""
    logger = get_logger(incident_id=state.incident_id)

    winner_id = state.tournament.winner_plan_id if state.tournament else None
    winner_plan = next((p for p in state.plans if p.plan_id == winner_id), None)

    if winner_plan is None:
        reason = "No winning plan available for safety kernel verification"
        logger.info("kernel_skipped_no_winner")
        return {
            "outcome": RunOutcome.ESCALATED,
            "escalation_reason": reason,
        }

    now = deps.clock.now()

    # Find candidate evidence for the winning plan if tournament ran
    winner_evidence = None
    if state.evidence:
        winner_evidence = next(
            (e for e in state.evidence if e.plan_id == winner_plan.plan_id),
            None,
        )

    # Resolve K8s fact source from fleet controller if available
    workload_reader = getattr(deps.fleet_controller, "workload_reader", None)
    k8s_source = (
        WorkloadReaderFactAdapter(workload_reader=workload_reader)
        if workload_reader is not None
        else None
    )

    # Ensure plan destined for production targets ust-prod for safety verification
    prod_target_resources = [
        r.model_copy(update={"namespace": "ust-prod"})
        if (not r.namespace or "twin" in r.namespace)
        else r
        for r in winner_plan.target_resources
    ]
    verified_plan = winner_plan.model_copy(update={"target_resources": prod_target_resources})
    if verified_plan.inverse is not None:
        inv_resources = [
            r.model_copy(update={"namespace": "ust-prod"})
            if (not r.namespace or "twin" in r.namespace)
            else r
            for r in verified_plan.inverse.target_resources
        ]
        verified_plan = verified_plan.model_copy(
            update={
                "inverse": verified_plan.inverse.model_copy(
                    update={"target_resources": inv_resources}
                )
            }
        )

    # Extract complete timestamped facts across K8s, GitHub, store, graph, and evidence
    facts = await extract_facts(
        verified_plan,
        evidence=winner_evidence,
        context=state.context,
        k8s_source=k8s_source,
        deploy_history=deps.deploy_history,
        run_store=deps.run_store,
        dependency_graph=deps.dependency_graph,
        clock=deps.clock,
    )

    # Ensure plan_target_namespaces has fallback to {"ust-prod"} if empty
    ns_fact = next((f for f in facts if f.name == "plan_target_namespaces"), None)
    if ns_fact is None or not ns_fact.value:
        facts = [f for f in facts if f.name != "plan_target_namespaces"] + [
            Fact(
                name="plan_target_namespaces",
                value={r.namespace for r in winner_plan.target_resources if r.namespace}
                or {"ust-prod"},
                source="config",
                observed_at=now,
            )
        ]

    verdict = await deps.safety_kernel.verify(winner_plan, facts)
    if state.incident_id and verdict.incident_id != state.incident_id:
        verdict = verdict.model_copy(update={"incident_id": state.incident_id})
    logger.info(
        "kernel_evaluated",
        verdict=verdict.verdict.value,
        plan_id=winner_plan.plan_id,
        solver_ms=verdict.solver_ms,
    )

    updates: dict[str, Any] = {"verdict": verdict}
    if verdict.verdict != KernelVerdictType.PASS:
        updates["outcome"] = RunOutcome.ESCALATED
        updates["escalation_reason"] = verdict.human_reason

    return updates


node = safety_kernel

__all__ = ["node", "safety_kernel"]
