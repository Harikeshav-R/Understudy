"""Escalate PagerDuty node: escalates vetoed, ambiguous, or failed incidents."""

from typing import Any

from understudy.common.logging import get_logger
from understudy.contracts.enums import RunOutcome
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def escalate_pagerduty(state: State, deps: Deps) -> dict[str, Any]:
    """Trigger on-call escalation with diagnosis details and partial evidence."""
    logger = get_logger(incident_id=state.incident_id)

    reason = state.escalation_reason
    if not reason:
        if state.verdict is not None:
            reason = state.verdict.human_reason
        elif state.errors:
            reason = "; ".join(state.errors)
        else:
            reason = "Incident escalated to human operator"

    await deps.notifier.escalate_pagerduty(
        incident_id=state.incident_id,
        reason=reason,
        partial_evidence=state.evidence,
    )

    logger.info("pagerduty_escalated", incident_id=state.incident_id, reason=reason)

    return {
        "outcome": RunOutcome.ESCALATED,
        "escalation_reason": reason,
    }


node = escalate_pagerduty

__all__ = ["escalate_pagerduty", "node"]
