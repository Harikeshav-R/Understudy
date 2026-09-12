"""Actuate node: applies a PASS-verified candidate to production (enforces K10)."""

from typing import Any

from understudy.common.errors import ActuationError, OrchestratorError
from understudy.common.logging import get_logger
from understudy.contracts.enums import KernelVerdictType, RunOutcome
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def actuate(state: State, deps: Deps) -> dict[str, Any]:
    """Apply winning plan to production, enforcing invariant K10."""
    logger = get_logger(incident_id=state.incident_id)

    if state.verdict is None or state.verdict.verdict != KernelVerdictType.PASS:
        msg = (
            f"Cannot actuate on production without PASS verdict "
            f"(got {state.verdict.verdict if state.verdict else 'None'})"
        )
        logger.error("actuation_denied_k10", reason=msg)
        raise ActuationError(msg)

    winner_id = state.tournament.winner_plan_id if state.tournament else None
    winner_plan = next((p for p in state.plans if p.plan_id == winner_id), None)

    if winner_plan is None:
        raise OrchestratorError("Cannot actuate: winning plan not found in state")

    success = await deps.actuator.apply_to_production(winner_plan, state.verdict)
    prod_outcome = "resolved" if success else "not_resolved"
    outcome = RunOutcome.EXECUTED if success else RunOutcome.FAILED

    logger.info(
        "production_actuated",
        plan_id=winner_plan.plan_id,
        prod_outcome=prod_outcome,
        outcome=outcome.value,
    )

    return {
        "prod_applied_plan_id": winner_plan.plan_id,
        "prod_outcome": prod_outcome,
        "outcome": outcome,
    }


node = actuate

__all__ = ["actuate", "node"]
