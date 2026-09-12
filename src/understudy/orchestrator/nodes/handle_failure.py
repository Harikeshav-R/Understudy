"""Handle failure node: failure path implementing §2.9 loop error recovery."""

from typing import Any

from understudy.common.logging import get_logger
from understudy.contracts.enums import RunOutcome
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def _cleanup(state: State, deps: Deps) -> list[str]:
    """Unregister mirrors and tear down twins best-effort, returning cleanup failures."""
    cleanup_errors: list[str] = []

    for twin in state.twins:
        try:
            await deps.mirror_registry.unregister_twin(twin.twin_id)
        except Exception as exc:
            # Catching Exception: cleanup is best-effort; one leaked twin must not stop
            # the remaining teardown or the run record write (architecture §2.9).
            cleanup_errors.append(f"unregister_twin({twin.twin_id}) failed: {exc}")

    if state.incident_id:
        try:
            await deps.fleet_controller.teardown_all(state.incident_id)
        except Exception as exc:
            # Catching Exception: see above; teardown_all is idempotent and retried by
            # `ust fleet gc`, so a failure here is recorded rather than raised.
            cleanup_errors.append(f"teardown_all({state.incident_id}) failed: {exc}")

    return cleanup_errors


async def handle_failure(state: State, deps: Deps) -> dict[str, Any]:
    """Clean up twin fleet, escalate failure to PagerDuty, and persist failed run."""
    logger = get_logger(incident_id=state.incident_id)
    now = deps.clock.now()

    cleanup_errors = await _cleanup(state, deps)

    reasons = [
        part
        for part in (
            state.escalation_reason,
            "; ".join(state.errors) if state.errors else None,
            "; ".join(cleanup_errors) if cleanup_errors else None,
        )
        if part
    ]
    reason = " | ".join(reasons) if reasons else "Unhandled loop failure"

    # A human was already paged for this incident; paging again for the same run would
    # duplicate the alert and bury the original escalation reason.
    if state.escalated:
        logger.info("escalation_already_sent", incident_id=state.incident_id)
    else:
        await deps.notifier.escalate_pagerduty(
            incident_id=state.incident_id or "unknown",
            reason=reason,
            partial_evidence=state.evidence,
        )

    outcome = RunOutcome.ESCALATED if state.escalated else RunOutcome.FAILED
    finished_at = state.finished_at or now
    updates: dict[str, Any] = {
        "outcome": outcome,
        "finished_at": finished_at,
        "escalation_reason": reason,
        "escalated": True,
    }

    failed_state = state.model_copy(update=updates)
    if failed_state.context is not None:
        await deps.run_store.record_run(failed_state.to_run_record(now=now))

    logger.info(
        "failure_handled",
        incident_id=state.incident_id,
        reason=reason,
        outcome=outcome.value,
    )

    return updates


node = handle_failure

__all__ = ["handle_failure", "node"]
