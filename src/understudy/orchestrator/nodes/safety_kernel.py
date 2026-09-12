"""Safety kernel node: evaluates formal Z3 invariants on winning remediation plan."""

from typing import Any

from understudy.common.logging import get_logger
from understudy.contracts.enums import ActionType, KernelVerdictType, RunOutcome
from understudy.contracts.kernel import Fact
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
    target_namespaces = {r.namespace for r in winner_plan.target_resources} or {"ust-prod"}
    has_inverse = winner_plan.inverse is not None or winner_plan.action == ActionType.NO_ACTION

    facts: list[Fact] = [
        Fact(
            name="plan_target_namespaces",
            value=target_namespaces,
            source="config",
            observed_at=now,
        ),
        Fact(
            name="plan_has_inverse",
            value=has_inverse,
            source="config",
            observed_at=now,
        ),
        Fact(
            name="declared_blast_set",
            value=set(winner_plan.declared_blast_set),
            source="graph",
            observed_at=now,
        ),
    ]

    verdict = await deps.safety_kernel.verify(winner_plan, facts)
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
