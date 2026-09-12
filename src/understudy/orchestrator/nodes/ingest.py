"""Ingest node: acquires the initial incident alert and initializes tracking state."""

from typing import Any

from understudy.common.ids import new_incident_id
from understudy.common.logging import get_logger
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def ingest(state: State, deps: Deps) -> dict[str, Any]:
    """Ingest alert and initialize incident identifiers and timestamps."""
    alert = state.alert
    if alert is None:
        alert = await deps.alert_source.receive_alert()

    if state.incident_id:
        incident_id = state.incident_id
    elif alert.alert_id:
        incident_id = f"inc_{alert.alert_id}"
    else:
        incident_id = new_incident_id()

    started_at = state.started_at or deps.clock.now()
    logger = get_logger(incident_id=incident_id)
    logger.info("incident_ingested", alert_id=alert.alert_id, service=alert.service)

    return {
        "incident_id": incident_id,
        "alert": alert,
        "started_at": started_at,
    }


node = ingest

__all__ = ["ingest", "node"]
