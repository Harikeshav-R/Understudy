"""Unit tests for orchestrator state definition, explicit reducers, and LangGraph integration."""

from datetime import UTC, datetime
from typing import Any

import pytest
from langgraph.graph import StateGraph

from understudy.common.errors import OrchestratorError
from understudy.contracts.enums import (
    ActionType,
    KernelVerdictType,
    RunOutcome,
    TournamentOutcome,
)
from understudy.contracts.evidence import CandidateEvidence, CandidateScore, TournamentResult
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.kernel import KernelVerdict
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.contracts.twin import MirrorStats, TwinHandle
from understudy.orchestrator.state import (
    State,
    reduce_alert,
    reduce_context,
    reduce_errors,
    reduce_evidence,
    reduce_finished_at,
    reduce_incident_id,
    reduce_optional_str,
    reduce_outcome,
    reduce_plans,
    reduce_prod_outcome,
    reduce_started_at,
    reduce_tournament,
    reduce_twins,
    reduce_verdict,
)


def _sample_alert(alert_id: str = "alt_1") -> Alert:
    return Alert(
        alert_id=alert_id,
        source="synthetic",
        title="Elevated error rate",
        service="edge-gateway",
        severity="error",
        fired_at=datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC),
        raw={"key": "val"},
    )


def _sample_context(incident_id: str = "inc_123") -> IncidentContext:
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    return IncidentContext(
        incident_id=incident_id,
        alert=_sample_alert(),
        signatures=[],
        metrics_window=MetricWindow(service="edge-gateway", start_time=now, end_time=now),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(nodes=["edge-gateway"], edges=[], observed_at=now),
        gathered_at=now,
    )


def _sample_plan(plan_id: str = "plan_0", candidate_index: int = 0) -> RemediationPlan:
    return RemediationPlan(
        plan_id=plan_id,
        candidate_index=candidate_index,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="edge-gateway"),
        target_resources=[],
        declared_blast_set=["edge-gateway"],
        inverse=None,
        rationale="Baseline rehearsal candidate",
        origin="planner",
    )


def _sample_twin(twin_id: str = "twin_0", state: str = "ready") -> TwinHandle:
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    return TwinHandle(
        twin_id=twin_id,
        incident_id="inc_123",
        candidate_index=0,
        namespace=f"ust-twin-{twin_id}",
        database=f"twin_{twin_id}",
        forked_from_snapshot_at=now,
        ready_at=now,
        state=state,  # type: ignore[arg-type]
    )


def _sample_evidence(plan_id: str = "plan_0", twin_id: str = "twin_0") -> CandidateEvidence:
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    return CandidateEvidence(
        plan_id=plan_id,
        twin_id=twin_id,
        applied_at=now,
        probes=[],
        recovered=True,
        recovery_seconds=10.5,
        observed_blast_set=["edge-gateway"],
        downstream_error_delta=0.0,
        invariant_violations=[],
        mirror_stats=MirrorStats(twin_id=twin_id, delivered=100, dropped=0),
        evidence_complete=True,
    )


def _sample_tournament(incident_id: str = "inc_123") -> TournamentResult:
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    return TournamentResult(
        incident_id=incident_id,
        outcome=TournamentOutcome.DECIDED,
        scores=[
            CandidateScore(
                plan_id="plan_0",
                composite=0.1,
                components={"recovery": 0.1},
                disqualified=False,
                disqualification_reason=None,
            )
        ],
        winner_plan_id="plan_0",
        runner_up_plan_id=None,
        margin=0.5,
        decided_at=now,
    )


def _sample_verdict(incident_id: str = "inc_123", plan_id: str = "plan_0") -> KernelVerdict:
    return KernelVerdict(
        incident_id=incident_id,
        plan_id=plan_id,
        verdict=KernelVerdictType.PASS,
        results=[],
        missing_facts=[],
        solver_ms=2.5,
        human_reason="All invariants satisfied",
    )


def test_reduce_incident_id() -> None:
    # Empty update returns current
    assert reduce_incident_id("inc_1", None) == "inc_1"
    assert reduce_incident_id("inc_1", "") == "inc_1"

    # Empty current accepts update
    assert reduce_incident_id("", "inc_1") == "inc_1"

    # Same update returns current
    assert reduce_incident_id("inc_1", "inc_1") == "inc_1"

    # Conflicting update raises OrchestratorError
    with pytest.raises(
        OrchestratorError, match="Cannot overwrite incident_id 'inc_1' with 'inc_2'"
    ):
        reduce_incident_id("inc_1", "inc_2")


def test_reduce_alert() -> None:
    alert = _sample_alert("alt_1")
    assert reduce_alert(alert, None) is alert

    # Dict update validated to Alert
    alert_dict = alert.model_dump(mode="json")
    updated = reduce_alert(None, alert_dict)
    assert isinstance(updated, Alert)
    assert updated.alert_id == "alt_1"

    # Alert instance update
    alert2 = _sample_alert("alt_2")
    assert reduce_alert(alert, alert2) is alert2


def test_reduce_context() -> None:
    ctx = _sample_context("inc_1")
    assert reduce_context(ctx, None) is ctx

    # Dict update validated to IncidentContext
    ctx_dict = ctx.model_dump(mode="json")
    updated = reduce_context(None, ctx_dict)
    assert isinstance(updated, IncidentContext)
    assert updated.incident_id == "inc_1"

    # Direct model update
    ctx2 = _sample_context("inc_2")
    assert reduce_context(ctx, ctx2) is ctx2


def test_reduce_plans() -> None:
    p0 = _sample_plan("plan_0", 0)
    p1 = _sample_plan("plan_1", 1)
    p2 = _sample_plan("plan_2", 2)

    # None update keeps current
    assert reduce_plans([p0], None) == [p0]

    # Initial update from empty
    assert reduce_plans([], [p0, p1]) == [p0, p1]

    # Single plan update
    assert reduce_plans([], p0) == [p0]

    # Single dict update
    p0_dict = p0.model_dump(mode="json")
    reduced_single_dict = reduce_plans([], p0_dict)
    assert len(reduced_single_dict) == 1
    assert reduced_single_dict[0].plan_id == "plan_0"

    # Sequence with dict and model
    p1_dict = p1.model_dump(mode="json")
    reduced_seq = reduce_plans([], [p0, p1_dict])
    assert len(reduced_seq) == 2

    # Reconcile: update existing by plan_id in place, preserve order, append new
    p0_updated = RemediationPlan(
        plan_id="plan_0",
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload="edge-gateway"),
        target_resources=[],
        declared_blast_set=["edge-gateway"],
        inverse=None,
        rationale="Updated rationale",
        origin="planner",
    )
    result = reduce_plans([p0, p1], [p0_updated, p2])
    assert len(result) == 3
    assert result[0].plan_id == "plan_0"
    assert result[0].action == ActionType.RESTART_WORKLOAD
    assert result[1].plan_id == "plan_1"
    assert result[2].plan_id == "plan_2"


def test_reduce_twins() -> None:
    t0 = _sample_twin("twin_0", "forking")
    t1 = _sample_twin("twin_1", "forking")
    t2 = _sample_twin("twin_2", "forking")

    # None update
    assert reduce_twins([t0], None) == [t0]

    # Initial empty update
    assert reduce_twins([], [t0, t1]) == [t0, t1]

    # Single twin update
    assert reduce_twins([], t0) == [t0]

    # Single dict update
    t0_dict = t0.model_dump(mode="json")
    reduced_dict = reduce_twins([], t0_dict)
    assert len(reduced_dict) == 1
    assert reduced_dict[0].twin_id == "twin_0"

    # Update lifecycle state in place
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    t0_ready = TwinHandle(
        twin_id="twin_0",
        incident_id="inc_123",
        candidate_index=0,
        namespace="ust-twin-twin_0",
        database="twin_twin_0",
        forked_from_snapshot_at=now,
        ready_at=now,
        state="ready",
    )
    res = reduce_twins([t0, t1], [t0_ready, t2])
    assert len(res) == 3
    assert res[0].twin_id == "twin_0"
    assert res[0].state == "ready"
    assert res[1].twin_id == "twin_1"
    assert res[2].twin_id == "twin_2"


def test_reduce_evidence() -> None:
    e0 = _sample_evidence("plan_0", "twin_0")
    e1 = _sample_evidence("plan_1", "twin_1")
    e2 = _sample_evidence("plan_2", "twin_2")

    # None update
    assert reduce_evidence([e0], None) == [e0]

    # Empty update
    assert reduce_evidence([], [e0, e1]) == [e0, e1]

    # Single evidence and dict
    assert reduce_evidence([], e0) == [e0]
    e0_dict = e0.model_dump(mode="json")
    res_dict = reduce_evidence([], e0_dict)
    assert len(res_dict) == 1
    assert res_dict[0].plan_id == "plan_0"

    # Reconcile in place and append
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    e0_updated = CandidateEvidence(
        plan_id="plan_0",
        twin_id="twin_0",
        applied_at=now,
        probes=[],
        recovered=False,
        recovery_seconds=None,
        observed_blast_set=[],
        downstream_error_delta=0.5,
        invariant_violations=["K3"],
        mirror_stats=MirrorStats(twin_id="twin_0", delivered=100, dropped=10),
        evidence_complete=False,
    )
    res = reduce_evidence([e0, e1], [e0_updated, e2])
    assert len(res) == 3
    assert res[0].plan_id == "plan_0"
    assert res[0].recovered is False
    assert res[1].plan_id == "plan_1"
    assert res[2].plan_id == "plan_2"


def test_reduce_tournament() -> None:
    tour = _sample_tournament("inc_1")
    assert reduce_tournament(tour, None) is tour

    # Dict update
    tour_dict = tour.model_dump(mode="json")
    res = reduce_tournament(None, tour_dict)
    assert isinstance(res, TournamentResult)
    assert res.incident_id == "inc_1"

    # Direct model update
    tour2 = _sample_tournament("inc_2")
    assert reduce_tournament(tour, tour2) is tour2


def test_reduce_verdict() -> None:
    verd = _sample_verdict("inc_1", "plan_0")
    assert reduce_verdict(verd, None) is verd

    # Dict update
    verd_dict = verd.model_dump(mode="json")
    res = reduce_verdict(None, verd_dict)
    assert isinstance(res, KernelVerdict)
    assert res.plan_id == "plan_0"

    # Direct model update
    verd2 = _sample_verdict("inc_2", "plan_1")
    assert reduce_verdict(verd, verd2) is verd2


def test_reduce_outcome() -> None:
    assert reduce_outcome(RunOutcome.EXECUTED, None) == RunOutcome.EXECUTED
    assert reduce_outcome(None, "executed") == RunOutcome.EXECUTED
    assert reduce_outcome(RunOutcome.EXECUTED, RunOutcome.ESCALATED) == RunOutcome.ESCALATED


def test_reduce_errors() -> None:
    assert reduce_errors(["e1"], None) == ["e1"]
    assert reduce_errors(["e1"], "e2") == ["e1", "e2"]
    assert reduce_errors(["e1"], ["e2", "e3"]) == ["e1", "e2", "e3"]


def test_lifecycle_reducers() -> None:
    now1 = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    now2 = datetime(2026, 9, 11, 12, 5, 0, tzinfo=UTC)

    # started_at: immutable earliest
    assert reduce_started_at(None, now1) == now1
    assert reduce_started_at(now1, now2) == now1

    # finished_at: updates if provided
    assert reduce_finished_at(None, now1) == now1
    assert reduce_finished_at(now1, now2) == now2
    assert reduce_finished_at(now2, None) == now2

    # optional_str: updates if provided
    assert reduce_optional_str(None, "plan_0") == "plan_0"
    assert reduce_optional_str("plan_0", "plan_1") == "plan_1"
    assert reduce_optional_str("plan_1", None) == "plan_1"

    # prod_outcome: updates if provided
    assert reduce_prod_outcome(None, "resolved") == "resolved"
    assert reduce_prod_outcome("resolved", "worsened") == "worsened"
    assert reduce_prod_outcome("resolved", None) == "resolved"


def test_state_defaults() -> None:
    state = State()
    assert state.incident_id == ""
    assert state.context is None
    assert state.plans == []
    assert state.twins == []
    assert state.evidence == []
    assert state.tournament is None
    assert state.verdict is None
    assert state.outcome is None
    assert state.errors == []
    assert state.alert is None
    assert state.started_at is None
    assert state.finished_at is None
    assert state.prod_applied_plan_id is None
    assert state.prod_outcome is None
    assert state.escalation_reason is None
    assert state.scenario_id is None


def test_to_run_record() -> None:
    state = State(incident_id="inc_123")
    # Context required
    with pytest.raises(
        OrchestratorError, match="Cannot convert State to RunRecord without context"
    ):
        state.to_run_record()

    # Success conversion with defaults
    ctx = _sample_context("inc_123")
    state_with_ctx = State(incident_id="inc_123", context=ctx)
    record = state_with_ctx.to_run_record()
    assert record.run_id == "run_inc_123"
    assert record.incident_id == "inc_123"
    assert record.outcome == RunOutcome.EXECUTED
    assert record.context == ctx
    assert record.started_at is not None
    assert record.finished_at is not None

    # Failed outcome defaulted when errors present
    state_err = State(incident_id="inc_123", context=ctx, errors=["something broke"])
    record_err = state_err.to_run_record(run_id="custom_run_id")
    assert record_err.run_id == "custom_run_id"
    assert record_err.outcome == RunOutcome.FAILED

    # Explicit values respected
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    state_full = State(
        incident_id="inc_123",
        context=ctx,
        started_at=now,
        finished_at=now,
        outcome=RunOutcome.ESCALATED,
        prod_applied_plan_id="plan_0",
        prod_outcome="resolved",
        escalation_reason="K3 veto",
        scenario_id="scenario_42",
    )
    record_full = state_full.to_run_record()
    assert record_full.outcome == RunOutcome.ESCALATED
    assert record_full.prod_applied_plan_id == "plan_0"
    assert record_full.prod_outcome == "resolved"
    assert record_full.escalation_reason == "K3 veto"
    assert record_full.scenario_id == "scenario_42"


def test_langgraph_stategraph_integration() -> None:
    p0 = _sample_plan("plan_0", 0)
    p1 = _sample_plan("plan_1", 1)
    t0 = _sample_twin("twin_0", "ready")
    ev0 = _sample_evidence("plan_0", "twin_0")
    tour = _sample_tournament("inc_123")
    verd = _sample_verdict("inc_123", "plan_0")

    builder = StateGraph(State)

    def node_ingest(state: State) -> dict[str, Any]:
        incident_id = state.incident_id or "inc_123"
        return {"incident_id": incident_id, "errors": ["warning_1"]}

    def node_plan(state: State) -> dict[str, Any]:
        assert state.incident_id == "inc_123"
        return {
            "plans": [p0, p1],
            "twins": [t0],
            "evidence": [ev0],
            "tournament": tour,
            "verdict": verd,
            "outcome": "executed",
            "prod_applied_plan_id": "plan_0",
            "prod_outcome": "resolved",
            "errors": ["warning_2"],
        }

    builder.add_node("ingest", node_ingest)
    builder.add_node("plan", node_plan)
    builder.set_entry_point("ingest")
    builder.add_edge("ingest", "plan")
    builder.set_finish_point("plan")

    graph = builder.compile()
    result = graph.invoke(State(incident_id="inc_123"))

    assert result["incident_id"] == "inc_123"
    assert len(result["plans"]) == 2
    assert len(result["twins"]) == 1
    assert len(result["evidence"]) == 1
    assert result["tournament"].winner_plan_id == "plan_0"
    assert result["verdict"].verdict == KernelVerdictType.PASS
    assert result["outcome"] == RunOutcome.EXECUTED
    assert result["prod_applied_plan_id"] == "plan_0"
    assert result["prod_outcome"] == "resolved"
    assert result["errors"] == ["warning_1", "warning_2"]
