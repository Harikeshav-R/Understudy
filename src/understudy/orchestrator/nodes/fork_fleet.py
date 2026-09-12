"""Fork fleet node: allocates isolated twin namespaces and database clones."""

from typing import Any

from understudy.common.errors import OrchestratorError
from understudy.common.logging import get_logger
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def fork_fleet(state: State, deps: Deps) -> dict[str, Any]:
    """Fork isolated twin environments matching the number of candidate plans."""
    if not state.incident_id:
        raise OrchestratorError("Cannot fork fleet without incident_id")
    if not state.plans:
        raise OrchestratorError("Cannot fork fleet without remediation plans")

    logger = get_logger(incident_id=state.incident_id)
    count = len(state.plans)
    twins = await deps.fleet_controller.fork(incident_id=state.incident_id, n=count)

    logger.info("fleet_forked", count=len(twins))
    return {"twins": twins}


node = fork_fleet

__all__ = ["fork_fleet", "node"]
