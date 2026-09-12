"""Tournament node: arbitrates deterministic scores and advisory LLM rankings."""

from typing import Any

from understudy.common.logging import get_logger
from understudy.contracts.enums import RunOutcome, TournamentOutcome
from understudy.orchestrator.api import Deps
from understudy.orchestrator.state import State


async def tournament(state: State, deps: Deps) -> dict[str, Any]:
    """Evaluate tournament results and determine winning candidate."""
    logger = get_logger(incident_id=state.incident_id)
    result = state.tournament
    if result is None:
        _, result = await deps.tournament.observe_and_score(state.twins, state.plans)

    updates: dict[str, Any] = {"tournament": result}

    if result.outcome == TournamentOutcome.AMBIGUOUS:
        reason = f"Tournament ambiguous: margin {result.margin} below confidence threshold"
        logger.info("tournament_ambiguous", margin=result.margin)
        updates["outcome"] = RunOutcome.ESCALATED
        updates["escalation_reason"] = reason
    elif result.outcome == TournamentOutcome.NO_VIABLE_CANDIDATE:
        reason = "Tournament finished with no viable candidate"
        logger.info("tournament_no_viable_candidate")
        updates["outcome"] = RunOutcome.ESCALATED
        updates["escalation_reason"] = reason
    else:
        logger.info("tournament_decided", winner=result.winner_plan_id, margin=result.margin)

    return updates


node = tournament

__all__ = ["node", "tournament"]
