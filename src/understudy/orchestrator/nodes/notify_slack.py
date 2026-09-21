"""Notify Slack node: delivers structured incident resolution updates."""

from typing import Any

from understudy.common.logging import get_logger
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def notify_slack(state: State, deps: Deps) -> dict[str, Any]:
    """Publish incident remediation outcome to Slack."""
    logger = get_logger(incident_id=state.incident_id)
    applied_plan = next(
        (p for p in state.plans if p.plan_id == state.prod_applied_plan_id),
        None,
    )

    status = state.prod_outcome or "executed"
    message = (
        f"Incident {state.incident_id} remediation status: {status}. "
        f"Plan: {state.prod_applied_plan_id or 'none'}."
    )

    await deps.notifier.notify_slack(
        incident_id=state.incident_id,
        message=message,
        plan=applied_plan,
        result=state.tournament,
        verdict=state.verdict,
        context=state.context,
        plans=state.plans,
        evidence=state.evidence,
        prod_outcome=state.prod_outcome,
        run_id=f"run_{state.incident_id}" if state.incident_id else None,
    )

    logger.info("slack_notified", incident_id=state.incident_id)
    return {}


node = notify_slack

__all__ = ["node", "notify_slack"]
