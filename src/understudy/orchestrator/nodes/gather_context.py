"""Gather context node: collects metrics, logs, deploys, and graph topology."""

from datetime import UTC, datetime
from typing import Any

from understudy.common.errors import OrchestratorError
from understudy.common.logging import get_logger
from understudy.contracts.incident import IncidentContext
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def gather_context(state: State, deps: Deps) -> dict[str, Any]:
    """Gather telemetry and metadata to construct comprehensive IncidentContext."""
    alert = state.alert
    if alert is None:
        raise OrchestratorError("Cannot gather context without an active alert in state")

    incident_id = state.incident_id or f"inc_{alert.alert_id}"
    logger = get_logger(incident_id=incident_id)
    service = alert.service or "default"

    metrics_window = await deps.observability.metric_window(service=service, since=alert.fired_at)
    signatures = await deps.observability.error_signatures(service=service, since=alert.fired_at)
    recent_deploys = await deps.deploy_history.recent_deploys(limit=5)
    dep_graph = deps.dependency_graph.snapshot()

    now = datetime.now(UTC)
    context = IncidentContext(
        incident_id=incident_id,
        alert=alert,
        signatures=signatures,
        metrics_window=metrics_window,
        recent_deploys=recent_deploys,
        dependency_graph=dep_graph,
        inferred_failure_class=None,
        gathered_at=now,
    )

    logger.info(
        "context_gathered",
        service=service,
        deploy_count=len(recent_deploys),
        signature_count=len(signatures),
    )

    return {"context": context}


node = gather_context

__all__ = ["gather_context", "node"]
