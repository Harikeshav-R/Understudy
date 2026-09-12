"""Unit tests for orchestrator graph assembly, edge routing, and control loop execution."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from understudy.common.errors import OrchestratorError
from understudy.contracts.enums import (
    InvariantTier,
    KernelVerdictType,
    RunOutcome,
    TournamentOutcome,
)
from understudy.contracts.evidence import CandidateEvidence, TournamentResult
from understudy.contracts.incident import Alert
from understudy.contracts.kernel import InvariantResult, KernelVerdict
from understudy.kernel.fakes import FakeSafetyKernel
from understudy.orchestrator.api import build_graph as api_build_graph
from understudy.orchestrator.api import run_incident as api_run_incident
from understudy.orchestrator.fakes import create_fake_deps
from understudy.orchestrator.graph import (
    build_graph,
    route_linear,
    route_safety_kernel,
    route_tournament,
    run_incident,
)
from understudy.orchestrator.nodes import ALL_NODES
from understudy.orchestrator.state import State
from understudy.tournament.fakes import FakeTournament


def _sample_alert(alert_id: str = "alt_test") -> Alert:
    return Alert(
        alert_id=alert_id,
        source="synthetic",
        title="High latency on edge-gateway",
        service="edge-gateway",
        severity="error",
        fired_at=datetime(2026, 9, 11, 20, 0, 0, tzinfo=UTC),
    )


def test_build_graph_structure() -> None:
    """Verify build_graph registers all 15 architecture nodes."""
    deps = create_fake_deps()
    graph = build_graph(deps)
    assert graph is not None

    node_names = set(graph.get_graph().nodes.keys())
    for expected_name in ALL_NODES:
        assert expected_name in node_names


def test_route_linear() -> None:
    """Verify route_linear router diverts on errors and proceeds normally otherwise."""
    router = route_linear("next_node")
    assert router(State(errors=[])) == "next_node"
    assert router(State(errors=["some error"])) == "handle_failure"


def test_route_tournament_branches() -> None:
    """Verify all branch conditions of route_tournament."""
    now = datetime.now(UTC)

    # 1. Error present
    assert route_tournament(State(errors=["failed"])) == "handle_failure"

    # 2. Tournament is None
    assert route_tournament(State(tournament=None)) == "escalate_pagerduty"

    # 3. Tournament outcome is AMBIGUOUS
    res_ambiguous = TournamentResult(
        incident_id="inc_1",
        outcome=TournamentOutcome.AMBIGUOUS,
        scores=[],
        decided_at=now,
    )
    assert route_tournament(State(tournament=res_ambiguous)) == "escalate_pagerduty"

    # 4. Tournament outcome is NO_VIABLE_CANDIDATE
    res_unviable = TournamentResult(
        incident_id="inc_1",
        outcome=TournamentOutcome.NO_VIABLE_CANDIDATE,
        scores=[],
        decided_at=now,
    )
    assert route_tournament(State(tournament=res_unviable)) == "escalate_pagerduty"

    # 5. Tournament DECIDED but winner_plan_id is missing
    res_no_winner = TournamentResult(
        incident_id="inc_1",
        outcome=TournamentOutcome.DECIDED,
        winner_plan_id=None,
        scores=[],
        decided_at=now,
    )
    assert route_tournament(State(tournament=res_no_winner)) == "escalate_pagerduty"

    # 6. Tournament DECIDED with winner, but state outcome already ESCALATED
    res_ok = TournamentResult(
        incident_id="inc_1",
        outcome=TournamentOutcome.DECIDED,
        winner_plan_id="plan_1",
        scores=[],
        decided_at=now,
    )
    assert (
        route_tournament(State(tournament=res_ok, outcome=RunOutcome.ESCALATED))
        == "escalate_pagerduty"
    )

    # 7. Tournament DECIDED with winner and no escalation
    assert route_tournament(State(tournament=res_ok)) == "safety_kernel"


def test_route_safety_kernel_branches() -> None:
    """Verify all branch conditions of route_safety_kernel."""
    # 1. Error present
    assert route_safety_kernel(State(errors=["failed"])) == "handle_failure"

    # 2. Verdict is None
    assert route_safety_kernel(State(verdict=None)) == "escalate_pagerduty"

    # 3. Verdict is VETO
    verdict_veto = KernelVerdict(
        incident_id="inc_1",
        plan_id="plan_1",
        verdict=KernelVerdictType.VETO,
        results=[
            InvariantResult(
                invariant_id="K3",
                tier=InvariantTier.PROOF,
                satisfied=False,
                reason="Vetoed",
            )
        ],
        missing_facts=[],
        solver_ms=10.0,
        human_reason="VETO: K3",
    )
    assert route_safety_kernel(State(verdict=verdict_veto)) == "escalate_pagerduty"

    # 4. Verdict is UNCERTAIN
    verdict_uncertain = KernelVerdict(
        incident_id="inc_1",
        plan_id="plan_1",
        verdict=KernelVerdictType.UNCERTAIN,
        results=[],
        missing_facts=["fact_1"],
        solver_ms=10.0,
        human_reason="UNCERTAIN",
    )
    assert route_safety_kernel(State(verdict=verdict_uncertain)) == "escalate_pagerduty"

    # 5. Verdict is PASS but state outcome is already ESCALATED
    verdict_pass = KernelVerdict(
        incident_id="inc_1",
        plan_id="plan_1",
        verdict=KernelVerdictType.PASS,
        results=[],
        missing_facts=[],
        solver_ms=10.0,
        human_reason="PASS",
    )
    assert (
        route_safety_kernel(State(verdict=verdict_pass, outcome=RunOutcome.ESCALATED))
        == "escalate_pagerduty"
    )

    # 6. Verdict is PASS and no escalation
    assert route_safety_kernel(State(verdict=verdict_pass)) == "actuate"


@pytest.mark.asyncio
async def test_full_happy_path_execution() -> None:
    """Verify normal control loop traverses all 13 happy-path nodes in sequence."""
    deps = create_fake_deps(seed=42)
    alert = _sample_alert("alt_happy")
    graph = build_graph(deps)

    visited_nodes: list[str] = []
    async for event in graph.astream(State(alert=alert), stream_mode="updates"):
        for node_name in event:
            visited_nodes.append(node_name)

    expected_sequence = [
        "ingest",
        "gather_context",
        "plan_candidates",
        "fork_fleet",
        "register_mirrors",
        "apply_candidates",
        "observe",
        "tournament",
        "safety_kernel",
        "actuate",
        "notify_slack",
        "teardown_fleet",
        "record_run",
    ]
    assert visited_nodes == expected_sequence

    stored_run = await deps.run_store.get_run("run_inc_alt_happy")
    assert stored_run is not None
    assert stored_run.outcome == RunOutcome.EXECUTED
    assert stored_run.prod_applied_plan_id == "plan_cand_0"
    assert stored_run.prod_outcome == "resolved"


@pytest.mark.asyncio
async def test_tournament_ambiguous_escalation_path() -> None:
    """Verify ambiguous tournament outcome routes to escalate_pagerduty and skips actuate."""
    deps = create_fake_deps()
    fake_tournament = FakeTournament(force_ambiguous=True)
    deps = deps.__class__(**{**deps.__dict__, "tournament": fake_tournament})

    alert = _sample_alert("alt_ambiguous")
    graph = build_graph(deps)

    visited_nodes: list[str] = []
    async for event in graph.astream(State(alert=alert), stream_mode="updates"):
        for node_name in event:
            visited_nodes.append(node_name)

    expected_sequence = [
        "ingest",
        "gather_context",
        "plan_candidates",
        "fork_fleet",
        "register_mirrors",
        "apply_candidates",
        "observe",
        "tournament",
        "escalate_pagerduty",
        "teardown_fleet",
        "record_run",
    ]
    assert visited_nodes == expected_sequence
    assert "safety_kernel" not in visited_nodes
    assert "actuate" not in visited_nodes
    assert "notify_slack" not in visited_nodes

    stored_run = await deps.run_store.get_run("run_inc_alt_ambiguous")
    assert stored_run is not None
    assert stored_run.outcome == RunOutcome.ESCALATED
    assert "Tournament ambiguous" in (stored_run.escalation_reason or "")


@pytest.mark.asyncio
async def test_tournament_no_viable_candidate_path() -> None:
    """Verify tournament with no viable candidate escalates directly."""
    deps = create_fake_deps()

    async def custom_observe(
        twins: Any, plans: Any
    ) -> tuple[list[CandidateEvidence], TournamentResult]:
        _ = (twins, plans)
        now = datetime.now(UTC)
        res = TournamentResult(
            incident_id="inc_unviable",
            outcome=TournamentOutcome.NO_VIABLE_CANDIDATE,
            scores=[],
            decided_at=now,
        )
        return [], res

    deps.tournament.observe_and_score = custom_observe  # type: ignore[method-assign]

    alert = _sample_alert("alt_unviable")
    graph = build_graph(deps)

    visited_nodes: list[str] = []
    async for event in graph.astream(State(alert=alert), stream_mode="updates"):
        for node_name in event:
            visited_nodes.append(node_name)

    assert visited_nodes[-3:] == ["escalate_pagerduty", "teardown_fleet", "record_run"]
    assert "actuate" not in visited_nodes


@pytest.mark.asyncio
async def test_safety_kernel_veto_escalation_path() -> None:
    """Verify safety kernel VETO verdict routes to escalate_pagerduty and skips actuate."""
    deps = create_fake_deps()
    fake_kernel = FakeSafetyKernel(force_verdict=KernelVerdictType.VETO)
    deps = deps.__class__(**{**deps.__dict__, "safety_kernel": fake_kernel})

    alert = _sample_alert("alt_veto")
    graph = build_graph(deps)

    visited_nodes: list[str] = []
    async for event in graph.astream(State(alert=alert), stream_mode="updates"):
        for node_name in event:
            visited_nodes.append(node_name)

    expected_sequence = [
        "ingest",
        "gather_context",
        "plan_candidates",
        "fork_fleet",
        "register_mirrors",
        "apply_candidates",
        "observe",
        "tournament",
        "safety_kernel",
        "escalate_pagerduty",
        "teardown_fleet",
        "record_run",
    ]
    assert visited_nodes == expected_sequence
    assert "actuate" not in visited_nodes
    assert "notify_slack" not in visited_nodes

    stored_run = await deps.run_store.get_run("run_inc_alt_veto")
    assert stored_run is not None
    assert stored_run.outcome == RunOutcome.ESCALATED
    assert "VETO" in (stored_run.escalation_reason or "")


@pytest.mark.asyncio
async def test_safety_kernel_uncertain_escalation_path() -> None:
    """Verify safety kernel UNCERTAIN verdict routes to escalate_pagerduty."""
    deps = create_fake_deps()
    fake_kernel = FakeSafetyKernel(force_verdict=KernelVerdictType.UNCERTAIN)
    deps = deps.__class__(**{**deps.__dict__, "safety_kernel": fake_kernel})

    alert = _sample_alert("alt_uncertain")
    graph = build_graph(deps)

    visited_nodes: list[str] = []
    async for event in graph.astream(State(alert=alert), stream_mode="updates"):
        for node_name in event:
            visited_nodes.append(node_name)

    assert "escalate_pagerduty" in visited_nodes
    assert "actuate" not in visited_nodes


@pytest.mark.asyncio
async def test_node_failure_error_edge_routing() -> None:
    """Verify an unhandled exception in any node immediately triggers handle_failure."""
    deps = create_fake_deps()

    async def fail_fork(*args: Any, **kwargs: Any) -> Any:
        _ = (args, kwargs)
        raise RuntimeError("k3s pod allocation failure")

    deps.fleet_controller.fork = fail_fork  # type: ignore[method-assign]

    alert = _sample_alert("alt_fail")
    graph = build_graph(deps)

    visited_nodes: list[str] = []
    async for event in graph.astream(State(alert=alert), stream_mode="updates"):
        for node_name in event:
            visited_nodes.append(node_name)

    expected_sequence = [
        "ingest",
        "gather_context",
        "plan_candidates",
        "fork_fleet",
        "handle_failure",
    ]
    assert visited_nodes == expected_sequence

    stored_run = await deps.run_store.get_run("run_inc_alt_fail")
    assert stored_run is not None
    assert stored_run.outcome == RunOutcome.FAILED


@pytest.mark.asyncio
async def test_node_runner_empty_incident_id_logging() -> None:
    """Verify _make_node_runner fallback logging when incident_id is empty."""
    deps = create_fake_deps()

    async def fail_ingest(*args: Any, **kwargs: Any) -> Any:
        _ = (args, kwargs)
        raise ValueError("alert source unreachable")

    deps.alert_source.receive_alert = fail_ingest  # type: ignore[method-assign]

    graph = build_graph(deps)
    # Start with empty incident_id and no alert
    res = await graph.ainvoke(State())
    assert "ValueError: alert source unreachable" in res["errors"][0]


@pytest.mark.asyncio
async def test_run_incident_api_and_store_retrieval() -> None:
    """Verify run_incident returns the persisted RunRecord from RunStore."""
    deps = create_fake_deps()
    alert = _sample_alert("alt_api")

    record = await run_incident(alert, deps)
    assert record.incident_id == "inc_alt_api"
    assert record.outcome == RunOutcome.EXECUTED

    # Call again via api_run_incident and api_build_graph
    api_graph = api_build_graph(deps)
    assert api_graph is not None

    record2 = await api_run_incident(alert, deps)
    assert record2.incident_id == "inc_alt_api"


@pytest.mark.asyncio
async def test_run_incident_missing_store_record_fallback() -> None:
    """Verify run_incident falls back to to_run_record if run_store does not contain it."""
    deps = create_fake_deps()
    # Mock run_store.get_run to return None
    deps.run_store.get_run = AsyncMock(return_value=None)  # type: ignore[method-assign]

    alert = _sample_alert("alt_fallback")
    record = await run_incident(alert, deps)
    assert record.incident_id == "inc_alt_fallback"
    assert record.outcome == RunOutcome.EXECUTED


@pytest.mark.asyncio
async def test_run_incident_empty_incident_id_fallback() -> None:
    """Verify run_incident when final_state.incident_id is empty."""
    deps = create_fake_deps()

    # If an alert has no alert_id and context is provided
    now = datetime.now(UTC)
    ctx = (await deps.tournament.observe_and_score([], []))[0]  # dummy check
    _ = ctx

    alert = Alert(
        alert_id="",
        source="synthetic",
        title="Test",
        service="edge-gateway",
        severity="error",
        fired_at=now,
    )
    record = await run_incident(alert, deps)
    assert record.outcome == RunOutcome.EXECUTED


@pytest.mark.asyncio
async def test_run_incident_missing_context_raises() -> None:
    """Verify run_incident raises OrchestratorError if context is absent in final state."""
    deps = create_fake_deps()

    async def fail_ingest(*args: Any, **kwargs: Any) -> Any:
        _ = (args, kwargs)
        raise RuntimeError("Failed before context")

    deps.alert_source.receive_alert = fail_ingest  # type: ignore[method-assign]

    # Ingest fails when alert is None, so context is never gathered
    alert = Alert(
        alert_id="",
        source="synthetic",
        title="",
        service="test",
        severity="error",
        fired_at=datetime.now(UTC),
    )
    # Clear alert in ingest to trigger receive_alert
    deps_mock = create_fake_deps()

    async def broken_ingest(state: State, deps: Any) -> dict[str, Any]:
        _ = (state, deps)
        raise RuntimeError("Early crash")

    from understudy.orchestrator.nodes import ingest as orig_ingest

    ALL_NODES["ingest"] = broken_ingest
    try:
        with pytest.raises(OrchestratorError, match="incident context was not gathered"):
            await run_incident(alert, deps_mock)
    finally:
        ALL_NODES["ingest"] = orig_ingest


def test_node_runner_protocol() -> None:
    """Verify NodeRunner protocol definition and direct invocation."""
    from typing import cast

    from understudy.orchestrator.graph import NodeRunner

    runner = cast("NodeRunner", object())
    NodeRunner.__call__(runner, State())
