"""LangGraph control loop assembly and compilation for Understudy orchestrator.

Implements build-plan step B1.3 and architecture §2.2 & §2.9:
- Compiles StateGraph over State with all 15 control loop nodes.
- Conditional edges for tournament outcome and safety kernel verdict.
- Single error edge routing to handle_failure for loop failure handling.
- Guaranteed execution of terminal nodes (teardown_fleet, record_run).
"""

from collections.abc import Callable
from typing import Any, Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from understudy.common.errors import OrchestratorError
from understudy.common.logging import get_logger
from understudy.contracts.enums import (
    KernelVerdictType,
    RunOutcome,
    TournamentOutcome,
)
from understudy.contracts.incident import Alert
from understudy.contracts.run import RunRecord
from understudy.orchestrator.api import Deps
from understudy.orchestrator.nodes import ALL_NODES, NodeFunc
from understudy.orchestrator.state import State


class NodeRunner(Protocol):
    """Protocol for state graph node execution with named state parameter."""

    def __call__(self, state: State) -> Any: ...


def _make_node_runner(name: str, fn: NodeFunc, deps: Deps) -> NodeRunner:
    """Wrap a node function with an exception barrier for orchestrator error edge."""

    async def _runner(state: State) -> dict[str, Any]:
        try:
            return await fn(state, deps)
        except Exception as exc:
            # Catching Exception: Architecture §2.9 loop error edge routes node failures
            # to handle_failure; never swallow exceptions in components (ADR-005, rule 5.5).
            logger = get_logger(incident_id=state.incident_id or "unknown")
            logger.error("node_execution_failed", node=name, error=str(exc))
            return {"errors": [f"{type(exc).__name__}: {exc}"]}

    return _runner


def route_linear(next_node: str) -> Callable[[State], str]:
    """Route to next sequential node or divert to handle_failure on error."""

    def _router(state: State) -> str:
        if state.errors:
            return "handle_failure"
        return next_node

    return _router


def route_tournament(state: State) -> str:
    """Route tournament outcome: decided winner to kernel, ambiguous/unviable to escalate."""
    if state.errors:
        return "handle_failure"
    if (
        state.tournament is None
        or state.tournament.outcome != TournamentOutcome.DECIDED
        or not state.tournament.winner_plan_id
        or state.outcome == RunOutcome.ESCALATED
    ):
        return "escalate_pagerduty"
    return "safety_kernel"


def route_safety_kernel(state: State) -> str:
    """Route kernel verdict: PASS to actuate, VETO/UNCERTAIN to escalate."""
    if state.errors:
        return "handle_failure"
    if (
        state.verdict is None
        or state.verdict.verdict != KernelVerdictType.PASS
        or state.outcome == RunOutcome.ESCALATED
    ):
        return "escalate_pagerduty"
    return "actuate"


CompiledGraph = CompiledStateGraph[State, None, State, State]


def build_graph(deps: Deps) -> CompiledGraph:
    """Compile and return the executable LangGraph state graph using provided dependencies."""
    builder: StateGraph[State, None, State, State] = StateGraph(State)

    for name, fn in ALL_NODES.items():
        builder.add_node(name, _make_node_runner(name, fn, deps))

    builder.add_edge(START, "ingest")

    # Linear nodes with error edge
    for src, dst in [
        ("ingest", "gather_context"),
        ("gather_context", "plan_candidates"),
        ("plan_candidates", "fork_fleet"),
        ("fork_fleet", "register_mirrors"),
        ("register_mirrors", "apply_candidates"),
        ("apply_candidates", "observe"),
        ("observe", "tournament"),
    ]:
        builder.add_conditional_edges(
            src,
            route_linear(dst),
            path_map={dst: dst, "handle_failure": "handle_failure"},
        )

    # Tournament branching
    builder.add_conditional_edges(
        "tournament",
        route_tournament,
        path_map={
            "safety_kernel": "safety_kernel",
            "escalate_pagerduty": "escalate_pagerduty",
            "handle_failure": "handle_failure",
        },
    )

    # Safety kernel branching
    builder.add_conditional_edges(
        "safety_kernel",
        route_safety_kernel,
        path_map={
            "actuate": "actuate",
            "escalate_pagerduty": "escalate_pagerduty",
            "handle_failure": "handle_failure",
        },
    )

    # Post-actuation and escalation convergence
    builder.add_conditional_edges(
        "actuate",
        route_linear("notify_slack"),
        path_map={"notify_slack": "notify_slack", "handle_failure": "handle_failure"},
    )
    builder.add_conditional_edges(
        "notify_slack",
        route_linear("teardown_fleet"),
        path_map={"teardown_fleet": "teardown_fleet", "handle_failure": "handle_failure"},
    )
    builder.add_conditional_edges(
        "escalate_pagerduty",
        route_linear("teardown_fleet"),
        path_map={"teardown_fleet": "teardown_fleet", "handle_failure": "handle_failure"},
    )

    # Terminal node guarantees
    builder.add_conditional_edges(
        "teardown_fleet",
        route_linear("record_run"),
        path_map={"record_run": "record_run", "handle_failure": "handle_failure"},
    )
    builder.add_edge("record_run", END)
    builder.add_edge("handle_failure", END)

    return builder.compile()


async def run_incident(alert: Alert, deps: Deps) -> RunRecord:
    """Execute the full incident control loop given an alert and dependencies."""
    graph = build_graph(deps)
    initial_state = State(alert=alert)
    final_output = await graph.ainvoke(initial_state)
    final_state = (
        final_output if isinstance(final_output, State) else State.model_validate(final_output)
    )

    if final_state.incident_id:
        existing = await deps.run_store.get_run(f"run_{final_state.incident_id}")
        if existing is not None:
            return existing

    if final_state.context is None:
        raise OrchestratorError("Cannot produce RunRecord: incident context was not gathered")

    return final_state.to_run_record()


__all__ = [
    "build_graph",
    "route_linear",
    "route_safety_kernel",
    "route_tournament",
    "run_incident",
]
