"""Teardown fleet node: idempotently destroys twins and unregisters mirror traffic."""

from typing import TYPE_CHECKING, Any

from understudy.common.logging import get_logger
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State

if TYPE_CHECKING:
    from understudy.contracts.twin import TwinHandle


async def teardown_fleet(state: State, deps: Deps) -> dict[str, Any]:
    """Unregister twin traffic and tear down twin environments."""
    logger = get_logger(incident_id=state.incident_id)

    for twin in state.twins:
        await deps.mirror_registry.unregister_twin(twin.twin_id)

    if state.incident_id:
        await deps.fleet_controller.teardown_all(state.incident_id)

    torn_down_twins: list[TwinHandle] = [
        twin.model_copy(update={"state": "torn_down"}) for twin in state.twins
    ]

    logger.info("fleet_torn_down", count=len(torn_down_twins))
    return {"twins": torn_down_twins}


node = teardown_fleet

__all__ = ["node", "teardown_fleet"]
