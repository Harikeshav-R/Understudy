"""Register mirrors node: registers twin environments with the traffic mirror gateway."""

from typing import Any

from understudy.common.logging import get_logger
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def register_mirrors(state: State, deps: Deps) -> dict[str, Any]:
    """Register all active twins with the mirror registry."""
    logger = get_logger(incident_id=state.incident_id)
    for twin in state.twins:
        await deps.mirror_registry.register_twin(twin)

    logger.info("mirrors_registered", count=len(state.twins))
    return {}


node = register_mirrors

__all__ = ["node", "register_mirrors"]
