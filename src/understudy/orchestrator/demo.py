"""Demo run execution and graph visualization for the Understudy orchestrator.

Implements build-plan step B1.5 and Checkpoint B1:
- Runs synthetic alert through LangGraph control loop on deterministic fakes.
- Streams node transitions in order.
- Renders control loop Mermaid and PNG diagrams.
"""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

from understudy.common.clock import Clock, resolve_clock
from understudy.common.errors import OrchestratorError
from understudy.contracts.incident import Alert
from understudy.contracts.run import RunRecord
from understudy.orchestrator.api import Deps
from understudy.orchestrator.fakes import create_fake_deps
from understudy.orchestrator.graph import build_graph
from understudy.orchestrator.state import State


async def run_demo(
    seed: int = 42,
    force_veto: bool = False,
    on_transition: Callable[[str], None] | None = None,
    deps: Deps | None = None,
    clock: Clock | None = None,
) -> tuple[list[str], RunRecord]:
    """Run a synthetic alert through the full orchestrator graph with fakes.

    Args:
        seed: Random seed for deterministic fake behavior.
        force_veto: If True, forces safety kernel to issue a VETO verdict on K3.
        on_transition: Optional callback invoked with node_name upon each node execution.
        deps: Optional injected Deps container. Defaults to create_fake_deps(seed, force_veto).
        clock: Optional injected clock for deterministic time derivation.

    Returns:
        tuple of (ordered list of node transition names, final persisted RunRecord).
    """
    active_clock = resolve_clock(clock)
    active_deps = deps or create_fake_deps(
        seed=seed,
        clock=active_clock,
        force_veto=force_veto,
    )
    graph = build_graph(active_deps)

    alert = Alert(
        alert_id=f"demo_{seed}",
        source="synthetic",
        title="Synthetic p99 latency breach",
        service="edge-gateway",
        severity="critical",
        fired_at=active_clock.now(),
        raw={"details": "p99 > 400ms"},
    )
    incident_id = f"inc_{alert.alert_id}"
    initial_state = State(alert=alert, incident_id=incident_id)
    config: RunnableConfig = {"configurable": {"thread_id": incident_id}}

    transitions: list[str] = []
    async for chunk in graph.astream(initial_state, config=config, stream_mode="updates"):
        for node_name in chunk:
            transitions.append(node_name)
            if on_transition is not None:
                on_transition(node_name)

    record = await active_deps.run_store.get_run(f"run_{incident_id}")
    if record is None:
        raise OrchestratorError(f"Run record run_{incident_id} not found in store")

    return transitions, record


def render_graph_png(
    output_path: Path | str | None = None,
    deps: Deps | None = None,
) -> bytes:
    """Render the orchestrator StateGraph to PNG, optionally saving to output_path."""
    active_deps = deps or create_fake_deps()
    graph = build_graph(active_deps)
    png_data: bytes = graph.get_graph().draw_mermaid_png()
    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(png_data)
    return png_data


def render_graph_mermaid(deps: Deps | None = None) -> str:
    """Return the Mermaid diagram definition string for the orchestrator StateGraph."""
    active_deps = deps or create_fake_deps()
    graph = build_graph(active_deps)
    return graph.get_graph().draw_mermaid()


__all__ = [
    "render_graph_mermaid",
    "render_graph_png",
    "run_demo",
]
