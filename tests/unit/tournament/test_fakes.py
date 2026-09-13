"""Unit tests for tournament fakes."""

from datetime import UTC, datetime

import pytest

from understudy.common.clock import FrozenClock
from understudy.contracts.enums import ActionType, TournamentOutcome
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.contracts.twin import TwinHandle
from understudy.tournament.api import Tournament
from understudy.tournament.fakes import FakeTournament


@pytest.mark.asyncio
async def test_fake_tournament_observe_and_score() -> None:
    """FakeTournament produces deterministic rehearsal evidence and result."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    fake = FakeTournament(clock=clock, seed=42)
    assert isinstance(fake, Tournament)

    now = clock.now()
    twins = [
        TwinHandle(
            twin_id="twin_0",
            incident_id="inc_1",
            candidate_index=0,
            namespace="ust-twin-0",
            database="db0",
            forked_from_snapshot_at=now,
            state="observing",
        ),
        TwinHandle(
            twin_id="twin_1",
            incident_id="inc_1",
            candidate_index=1,
            namespace="ust-twin-1",
            database="db1",
            forked_from_snapshot_at=now,
            state="observing",
        ),
    ]
    params = ActionParams(workload="edge-gateway")
    plans = [
        RemediationPlan(
            plan_id="plan_0",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=params,
            origin="planner",
            rationale="Rollback",
        ),
        RemediationPlan(
            plan_id="plan_1",
            candidate_index=1,
            action=ActionType.RESTART_WORKLOAD,
            params=params,
            origin="planner",
            rationale="Restart",
        ),
    ]

    evidences, result = await fake.observe_and_score(twins, plans)
    assert len(evidences) == 2
    assert evidences[0].recovered is True
    assert evidences[1].recovered is False
    assert result.outcome == TournamentOutcome.DECIDED
    assert result.winner_plan_id == "plan_0"


def test_fake_tournament_arbitrate_empty_scores() -> None:
    """Empty scores in arbitrate yields NO_VIABLE_CANDIDATE."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    fake = FakeTournament(clock=clock)
    result = fake.arbitrate([], [])
    assert result.outcome == TournamentOutcome.NO_VIABLE_CANDIDATE
    assert result.scores == []


@pytest.mark.asyncio
async def test_fake_tournament_force_ambiguous() -> None:
    """force_ambiguous flag forces outcome=AMBIGUOUS."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    fake = FakeTournament(force_ambiguous=True, clock=clock)

    now = clock.now()
    twins = [
        TwinHandle(
            twin_id="twin_0",
            incident_id="inc_1",
            candidate_index=0,
            namespace="ust-twin-0",
            database="db0",
            forked_from_snapshot_at=now,
            state="observing",
        )
    ]
    plans = [
        RemediationPlan(
            plan_id="plan_0",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=ActionParams(workload="edge-gateway"),
            origin="planner",
            rationale="Rollback",
        )
    ]
    _, result = await fake.observe_and_score(twins, plans)
    assert result.outcome == TournamentOutcome.AMBIGUOUS
    assert result.winner_plan_id is None
