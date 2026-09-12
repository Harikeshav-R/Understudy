"""Handle failure node: failure path implementing §2.9 loop error recovery."""

from datetime import UTC, datetime
from typing import Any

from understudy.common.logging import get_logger
from understudy.contracts.enums import RunOutcome
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def handle_failure(state: State, deps: Deps) -> dict[str, Any]:
    """Clean up twin fleet, escalate failure to PagerDuty, and persist failed run."""
    logger = get_logger(incident_id=state.incident_id)
    now = datetime.now(UTC)

    if state.incident_id:
        await deps.fleet_controller.teardown_all(state.incident_id)

    reason = "; ".join(state.errors) if state.errors else "Unhandled loop failure"
    await deps.notifier.escalate_pagerduty(
        incident_id=state.incident_id or "unknown",
        reason=reason,
        partial_evidence=state.evidence,
    )

    finished_at = state.finished_at or now
    failed_state = state.model_copy(
        update={"outcome": RunOutcome.FAILED, "finished_at": finished_at}
    )

    if failed_state.context is not None:
        record = failed_state.to_run_record()
        await deps.run_store.record_run(record)

    logger.info("failure_handled", incident_id=state.incident_id, reason=reason)

    return {
        "outcome": RunOutcome.FAILED,
        "finished_at": finished_at,
    }


node = handle_failure

__all__ = ["handle_failure", "node"]
