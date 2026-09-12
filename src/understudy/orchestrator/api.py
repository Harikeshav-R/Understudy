"""Orchestrator component protocol interfaces and dependency seam."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from understudy.actuator.api import Actuator
from understudy.common.clock import Clock, SystemClock
from understudy.contracts.incident import Alert
from understudy.contracts.run import RunRecord
from understudy.fleet.api import FleetController
from understudy.graph.api import BlastRadiusCalculator, DependencyGraph
from understudy.kernel.api import SafetyKernel
from understudy.mirror.api import MirrorRegistry
from understudy.notify.api import Notifier
from understudy.orchestrator.checkpoint import (
    PostgresCheckpointSaver,
    StoreCheckpointSaver,
    create_checkpointer,
)
from understudy.orchestrator.state import State
from understudy.planner.api import Planner
from understudy.playbook.api import PlaybookLibrary
from understudy.signals.api import AlertSource, DeployHistory, ObservabilityAdapter
from understudy.store.api import CheckpointStore, EvalStore, PlaybookStore, RunStore
from understudy.tournament.api import Tournament


@dataclass(frozen=True)
class Deps:
    """Dependency-injection container holding instances of all component protocols."""

    run_store: RunStore
    playbook_store: PlaybookStore
    eval_store: EvalStore
    alert_source: AlertSource
    observability: ObservabilityAdapter
    deploy_history: DeployHistory
    dependency_graph: DependencyGraph
    blast_calculator: BlastRadiusCalculator
    playbook_library: PlaybookLibrary
    planner: Planner
    fleet_controller: FleetController
    mirror_registry: MirrorRegistry
    tournament: Tournament
    safety_kernel: SafetyKernel
    actuator: Actuator
    notifier: Notifier
    clock: Clock = field(default_factory=SystemClock)
    checkpoint_store: CheckpointStore | None = None


@runtime_checkable
class Orchestrator(Protocol):
    """Orchestrator protocol for incident response execution."""

    async def run_incident(self, alert: Alert) -> RunRecord:
        """Run the incident response loop from alert to terminal outcome."""
        raise NotImplementedError


def build_graph(deps: Deps, checkpointer: Any = ...) -> Any:
    """Compile and return the executable LangGraph state graph using provided dependencies."""
    from understudy.orchestrator.graph import build_graph as _build_graph

    if checkpointer is ...:
        return _build_graph(deps)
    return _build_graph(deps, checkpointer=checkpointer)


async def run_incident(alert: Alert, deps: Deps) -> RunRecord:
    """Execute the full incident control loop given an alert and dependencies."""
    from understudy.orchestrator.graph import run_incident as _run_incident

    return await _run_incident(alert, deps)


async def run_demo(
    seed: int = 42,
    force_veto: bool = False,
    on_transition: Callable[[str], None] | None = None,
    deps: Deps | None = None,
    clock: Clock | None = None,
) -> tuple[list[str], RunRecord]:
    """Execute a synthetic demo through the full control loop on fakes."""
    from understudy.orchestrator.demo import run_demo as _run_demo

    return await _run_demo(
        seed=seed,
        force_veto=force_veto,
        on_transition=on_transition,
        deps=deps,
        clock=clock,
    )


def render_graph_png(
    output_path: Path | str | None = None,
    deps: Deps | None = None,
) -> bytes:
    """Render the orchestrator StateGraph to PNG, optionally saving to output_path."""
    from understudy.orchestrator.demo import render_graph_png as _render_graph_png

    return _render_graph_png(output_path=output_path, deps=deps)


def render_graph_mermaid(deps: Deps | None = None) -> str:
    """Return the Mermaid diagram definition string for the orchestrator StateGraph."""
    from understudy.orchestrator.demo import render_graph_mermaid as _render_graph_mermaid

    return _render_graph_mermaid(deps=deps)


__all__ = [
    "Deps",
    "Orchestrator",
    "PostgresCheckpointSaver",
    "State",
    "StoreCheckpointSaver",
    "build_graph",
    "create_checkpointer",
    "render_graph_mermaid",
    "render_graph_png",
    "run_demo",
    "run_incident",
]
