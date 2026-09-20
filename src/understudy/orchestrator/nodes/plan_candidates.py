"""Plan candidates node: produces LLM and playbook remediation candidates."""

from typing import TYPE_CHECKING, Any

from understudy.common.errors import OrchestratorError
from understudy.common.logging import get_logger
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State

if TYPE_CHECKING:
    from understudy.contracts.plan import RemediationPlan


async def plan_candidates(state: State, deps: Deps) -> dict[str, Any]:
    """Generate candidate remediation plans and check playbook library."""
    if state.context is None:
        raise OrchestratorError("Cannot plan candidates without incident context")

    logger = get_logger(incident_id=state.incident_id)
    playbook_candidate = None
    try:
        playbook_candidate = await deps.playbook_library.retrieve_candidate(state.context)
    except Exception as exc:
        logger.warning("playbook_retrieval_failed", error=str(exc))
        playbook_candidate = None

    planner_plans = await deps.planner.generate_candidates(
        state.context,
        count=3,
        playbook_candidate=playbook_candidate,
    )

    all_plans: list[RemediationPlan] = list(planner_plans)
    if playbook_candidate is not None:
        duplicate_plan = next(
            (
                p
                for p in all_plans
                if p.action == playbook_candidate.action and p.params == playbook_candidate.params
            ),
            None,
        )
        if duplicate_plan is not None:
            all_plans = [
                p.model_copy(
                    update={
                        "origin": "playbook",
                        "playbook_id": playbook_candidate.playbook_id,
                    }
                )
                if p is duplicate_plan
                else p
                for p in all_plans
            ]
        else:
            all_plans.append(playbook_candidate)

    indexed_plans = [
        plan.model_copy(update={"candidate_index": idx}) for idx, plan in enumerate(all_plans)
    ]

    logger.info("candidates_planned", count=len(indexed_plans))
    return {"plans": indexed_plans}


node = plan_candidates

__all__ = ["node", "plan_candidates"]
