"""Apply candidates node: applies each candidate plan to its dedicated twin."""

from typing import TYPE_CHECKING, Any

from understudy.common.errors import OrchestratorError
from understudy.common.logging import get_logger
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State

if TYPE_CHECKING:
    from understudy.contracts.twin import TwinHandle


async def apply_candidates(state: State, deps: Deps) -> dict[str, Any]:
    """Apply each remediation plan to the twin sharing its candidate_index."""
    logger = get_logger(incident_id=state.incident_id)

    # Pair on candidate_index rather than list position: reduce_twins appends twins it
    # cannot match by twin_id, so a re-forked fleet would otherwise shift every plan
    # onto another candidate's twin. Later entries win, being the newer fork.
    twins_by_index: dict[int, TwinHandle] = {twin.candidate_index: twin for twin in state.twins}

    missing = [p.candidate_index for p in state.plans if p.candidate_index not in twins_by_index]
    if missing:
        raise OrchestratorError(
            f"No twin forked for candidate_index {missing} "
            f"(plans {len(state.plans)}, twins {len(state.twins)})"
        )

    updated_twins: list[TwinHandle] = []
    for plan in state.plans:
        twin = twins_by_index[plan.candidate_index]
        await deps.actuator.apply(plan, twin.namespace)
        updated_twins.append(twin.model_copy(update={"state": "applied"}))

    applied_ids = {twin.twin_id for twin in updated_twins}
    skipped = [twin.twin_id for twin in state.twins if twin.twin_id not in applied_ids]
    if skipped:
        logger.warning("twins_not_applied", twin_ids=skipped)

    logger.info("candidates_applied", count=len(updated_twins))
    return {"twins": updated_twins}


node = apply_candidates

__all__ = ["apply_candidates", "node"]
