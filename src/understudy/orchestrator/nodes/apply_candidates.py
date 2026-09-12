"""Apply candidates node: applies each candidate plan to its dedicated twin."""

from typing import TYPE_CHECKING, Any

from understudy.common.errors import OrchestratorError
from understudy.common.logging import get_logger
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State

if TYPE_CHECKING:
    from understudy.contracts.twin import TwinHandle


async def apply_candidates(state: State, deps: Deps) -> dict[str, Any]:
    """Apply each remediation plan to its assigned twin environment."""
    if len(state.plans) != len(state.twins):
        raise OrchestratorError(
            f"Mismatched plans ({len(state.plans)}) and twins ({len(state.twins)})"
        )

    logger = get_logger(incident_id=state.incident_id)
    updated_twins: list[TwinHandle] = []

    for plan, twin in zip(state.plans, state.twins, strict=True):
        await deps.actuator.apply(plan, twin.namespace)
        updated_twins.append(twin.model_copy(update={"state": "applied"}))

    logger.info("candidates_applied", count=len(updated_twins))
    return {"twins": updated_twins}


node = apply_candidates

__all__ = ["apply_candidates", "node"]
