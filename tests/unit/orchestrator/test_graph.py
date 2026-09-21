from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from understudy.contracts.plan import RemediationPlan
    from understudy.contracts.run import RunRecord

import pytest

from understudy.actuator.fakes import FakeActuator
from understudy.common.config import TimeoutSettings
from understudy.common.errors import OrchestratorError
from understudy.contracts.enums import (
    InvariantTier,
    KernelVerdictType,
    RunOutcome,
    TournamentOutcome,
)
from understudy.contracts.evidence import CandidateEvidence, TournamentResult
from understudy.contracts.incident import Alert, IncidentContext
from understudy.contracts.kernel import InvariantResult, KernelVerdict
from understudy.kernel.fakes import FakeSafetyKernel
from understudy.notify.fakes import FakeNotifier
from understudy.orchestrator.api import Deps
from understudy.orchestrator.api import build_graph as api_build_graph
from understudy.orchestrator.api import run_incident as api_run_incident
from understudy.orchestrator.fakes import create_fake_deps
from understudy.orchestrator.graph import (
    build_graph,
    route_actuation,
    route_linear,
    route_safety_kernel,
    route_tournament,
    run_incident,
)
from understudy.orchestrator.nodes import ALL_NODES
from understudy.orchestrator.state import State
from understudy.store.fakes import FakeRunStore
from understudy.tournament.fakes import FakeTournament


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


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
    config = _config("inc_alt_happy")
    async for event in graph.astream(State(alert=alert), config=config, stream_mode="updates"):
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

    # Verify LangGraph checkpointing persisted into CheckpointStore
    assert deps.checkpoint_store is not None
    cps = await deps.checkpoint_store.list_checkpoints(thread_id="inc_alt_happy")
    assert len(cps) >= 13


@pytest.mark.asyncio
async def test_tournament_ambiguous_escalation_path() -> None:
    """Verify ambiguous tournament outcome routes to escalate_pagerduty and skips actuate."""
    deps = create_fake_deps()
    fake_tournament = FakeTournament(force_ambiguous=True)
    deps = deps.__class__(**{**deps.__dict__, "tournament": fake_tournament})

    alert = _sample_alert("alt_ambiguous")
    graph = build_graph(deps)

    visited_nodes: list[str] = []
    config = _config("inc_alt_ambiguous")
    async for event in graph.astream(State(alert=alert), config=config, stream_mode="updates"):
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
    config = _config("inc_unviable")
    async for event in graph.astream(State(alert=alert), config=config, stream_mode="updates"):
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
    config = _config("inc_alt_veto")
    async for event in graph.astream(State(alert=alert), config=config, stream_mode="updates"):
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
    config = _config("inc_alt_uncertain")
    async for event in graph.astream(State(alert=alert), config=config, stream_mode="updates"):
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
    config = _config("inc_alt_fail")
    async for event in graph.astream(State(alert=alert), config=config, stream_mode="updates"):
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
    res = await graph.ainvoke(State(), config=_config("test_empty"))
    assert "ValueError: alert source unreachable" in res["errors"][0]


def test_route_actuation_branches() -> None:
    """Verify route_actuation pages on-call unless production came back resolved."""
    assert route_actuation(State(errors=["boom"], prod_outcome="resolved")) == "handle_failure"
    assert route_actuation(State(prod_outcome="resolved")) == "notify_slack"
    assert route_actuation(State(prod_outcome="not_resolved")) == "escalate_pagerduty"
    assert route_actuation(State(prod_outcome=None)) == "escalate_pagerduty"


@pytest.mark.asyncio
async def test_unresolved_production_escalates() -> None:
    """A remediation that applies but does not resolve production must page a human."""

    class UnresolvingActuator(FakeActuator):
        async def apply_to_production(
            self,
            plan: RemediationPlan,
            verdict: KernelVerdict,
            context: IncidentContext | None = None,
        ) -> bool:
            await super().apply_to_production(plan, verdict, context=context)
            return False

    deps = create_fake_deps()
    deps = deps.__class__(**{**deps.__dict__, "actuator": UnresolvingActuator()})

    record = await run_incident(_sample_alert("alt_unresolved"), deps)

    notifier = deps.notifier
    assert isinstance(notifier, FakeNotifier)
    assert len(notifier.pagerduty_escalations) == 1
    assert "did not resolve incident" in notifier.pagerduty_escalations[0]["reason"]
    assert notifier.slack_posts == []

    assert record.outcome == RunOutcome.ESCALATED
    assert record.prod_outcome == "not_resolved"
    assert record.prod_applied_plan_id is not None
    assert record.escalation_reason is not None


@pytest.mark.asyncio
async def test_record_run_failure_reaches_handle_failure() -> None:
    """A persistence failure in record_run must not end the graph silently."""

    class FlakyRunStore(FakeRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def record_run(self, record: RunRecord) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("run store unavailable")
            await super().record_run(record)

    store = FlakyRunStore()
    deps = create_fake_deps()
    deps = deps.__class__(**{**deps.__dict__, "run_store": store})

    record = await run_incident(_sample_alert("alt_flaky"), deps)

    # record_run raised, so the error edge ran handle_failure, which paged and
    # persisted the run instead of the loop returning an unpersisted EXECUTED record.
    assert store.attempts == 2
    assert record.outcome == RunOutcome.FAILED
    assert "run store unavailable" in (record.escalation_reason or "")

    notifier = deps.notifier
    assert isinstance(notifier, FakeNotifier)
    assert len(notifier.pagerduty_escalations) == 1

    stored = await store.get_run(record.run_id)
    assert stored is not None
    assert stored.outcome == RunOutcome.FAILED


def test_build_graph_checkpointer_variants() -> None:
    """Verify build_graph handles various checkpointer configurations."""
    from understudy.orchestrator.checkpoint import StoreCheckpointSaver
    from understudy.store.fakes import FakeCheckpointStore

    # 1. deps.checkpoint_store is None (falls back to no checkpointer)
    deps_no_store = create_fake_deps()
    deps_no_store = deps_no_store.__class__(**{**deps_no_store.__dict__, "checkpoint_store": None})
    g1 = build_graph(deps_no_store)
    assert g1.checkpointer is None

    # 2. explicit checkpointer=None override
    deps = create_fake_deps()
    g2 = build_graph(deps, checkpointer=None)
    assert g2.checkpointer is None

    # 3. explicit BaseCheckpointSaver instance
    saver = StoreCheckpointSaver(FakeCheckpointStore())
    g3 = build_graph(deps, checkpointer=saver)
    assert g3.checkpointer is saver

    # 4. invalid checkpointer argument is rejected rather than silently dropped
    with pytest.raises(TypeError, match="must be a BaseCheckpointSaver or None"):
        build_graph(deps, checkpointer="invalid_saver")


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

    api_graph_custom = api_build_graph(deps, checkpointer=None)
    assert api_graph_custom.checkpointer is None

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
async def test_run_incident_empty_incident_id_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify run_incident when final_state.incident_id is empty."""
    from understudy.orchestrator.nodes.gather_context import gather_context

    deps = create_fake_deps()
    alert = _sample_alert("alt_empty_id")
    gather_res = await gather_context(State(alert=alert), deps)
    ctx = gather_res["context"]

    mock_graph = AsyncMock()
    mock_graph.ainvoke.return_value = State(incident_id="", context=ctx)
    monkeypatch.setattr(
        "understudy.orchestrator.graph.build_graph", lambda *_args, **_kwargs: mock_graph
    )

    record = await run_incident(alert, deps)
    assert record.outcome == RunOutcome.EXECUTED
    assert record.incident_id == ""


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


@pytest.mark.asyncio
async def test_node_timeout_in_graph_routes_to_handle_failure() -> None:
    """A node that exceeds its timeout raises NodeTimeoutError and diverts to handle_failure."""
    deps = create_fake_deps(timeouts=TimeoutSettings(fork_seconds=1))

    async def slow_fork(state: State, d: Deps) -> dict[str, Any]:
        _ = (state, d)
        await asyncio.sleep(0.1)
        return {}

    from understudy.orchestrator.nodes import fork_fleet as orig_fork

    ALL_NODES["fork_fleet"] = slow_fork
    try:
        graph = build_graph(deps, custom_node_timeouts={"fork_fleet": 0.01})
        alert = _sample_alert("alt_node_timeout")
        incident_id = f"inc_{alert.alert_id}"
        initial_state = State(alert=alert, incident_id=incident_id)
        config: RunnableConfig = {"configurable": {"thread_id": incident_id}}
        final_output = await graph.ainvoke(initial_state, config=config)
        final_state = (
            final_output if isinstance(final_output, State) else State.model_validate(final_output)
        )
        assert final_state.outcome == RunOutcome.FAILED
        assert any("NodeTimeoutError" in err for err in final_state.errors)
        assert "fork_fleet" in (final_state.escalation_reason or "")
    finally:
        ALL_NODES["fork_fleet"] = orig_fork


@pytest.mark.asyncio
async def test_run_incident_whole_incident_watchdog_timeout() -> None:
    """When incident execution exceeds incident_seconds, watchdog escalates and returns record."""
    deps = create_fake_deps(timeouts=TimeoutSettings(incident_seconds=1))
    deps = deps.__class__(**{**deps.__dict__, "timeouts": TimeoutSettings(incident_seconds=0.02)})

    async def slow_observe(state: State, d: Deps) -> dict[str, Any]:
        _ = (state, d)
        await asyncio.sleep(0.2)
        return {}

    from understudy.orchestrator.nodes import observe as orig_observe

    ALL_NODES["observe"] = slow_observe
    try:
        alert = _sample_alert("alt_watchdog_to")
        record = await run_incident(alert, deps)
        assert record.outcome == RunOutcome.ESCALATED
        assert "Incident watchdog timeout exceeded" in (record.escalation_reason or "")

        notifier = deps.notifier
        assert isinstance(notifier, FakeNotifier)
        assert len(notifier.pagerduty_escalations) == 1
        assert "watchdog timeout exceeded" in notifier.pagerduty_escalations[0]["reason"]
    finally:
        ALL_NODES["observe"] = orig_observe


@pytest.mark.asyncio
async def test_run_incident_watchdog_timeout_before_context() -> None:
    """When watchdog fires before context is gathered, run_incident raises OrchestratorError."""
    deps = create_fake_deps()
    deps = deps.__class__(**{**deps.__dict__, "timeouts": TimeoutSettings(incident_seconds=0.02)})

    async def hanging_ingest(state: State, d: Deps) -> dict[str, Any]:
        _ = (state, d)
        await asyncio.sleep(0.2)
        return {}

    from understudy.orchestrator.nodes import ingest as orig_ingest

    ALL_NODES["ingest"] = hanging_ingest
    try:
        alert = _sample_alert("alt_watchdog_no_ctx")
        with pytest.raises(OrchestratorError, match="Incident watchdog timed out"):
            await run_incident(alert, deps)
    finally:
        ALL_NODES["ingest"] = orig_ingest
