"""Record run node: persists immutable execution history to append-only store."""

from typing import Any

from understudy.common.logging import get_logger
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def record_run(state: State, deps: Deps) -> dict[str, Any]:
    """Construct and persist RunRecord, recording playbook outcomes if applicable."""
    logger = get_logger(incident_id=state.incident_id)
    finished_at = deps.clock.now()

    updated_state = state.model_copy(update={"finished_at": finished_at})
    record = updated_state.to_run_record(now=finished_at)

    await deps.run_store.record_run(record)

    applied_plan = next(
        (p for p in state.plans if p.plan_id == state.prod_applied_plan_id),
        None,
    )
    if applied_plan is not None:
        if state.prod_outcome == "resolved" and state.context is not None:
            await deps.playbook_library.record_resolved_run(
                context=state.context,
                plan=applied_plan,
                run_id=record.run_id,
            )
        elif (
            applied_plan.origin == "playbook"
            and applied_plan.playbook_id is not None
            and state.prod_outcome != "resolved"
        ):
            await deps.playbook_library.record_outcome(
                playbook_id=applied_plan.playbook_id,
                success=False,
                evidence_run_id=record.run_id,
            )

    logger.info("run_recorded", run_id=record.run_id, outcome=record.outcome.value)

    return {
        "finished_at": finished_at,
        "outcome": record.outcome,
    }


node = record_run

__all__ = ["node", "record_run"]
