"""Observe node: gathers telemetry and probes across twins under mirrored traffic."""

from typing import Any

from understudy.common.logging import get_logger
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def observe(state: State, deps: Deps) -> dict[str, Any]:
    """Collect candidate evidence from twin observation probes."""
    logger = get_logger(incident_id=state.incident_id)

    evidence, tournament_result = await deps.tournament.observe_and_score(state.twins, state.plans)
    logger.info("twins_observed", evidence_count=len(evidence))

    return {
        "evidence": evidence,
        "tournament": tournament_result,
    }


node = observe

__all__ = ["node", "observe"]
