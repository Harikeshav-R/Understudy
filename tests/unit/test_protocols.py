"""Tests for Protocol definitions across all components."""

import inspect
from datetime import UTC, datetime
from typing import cast

import pytest

from understudy.actuator.api import Actuator
from understudy.contracts.incident import Alert
from understudy.eval.api import EvalHarness
from understudy.fleet.api import FleetController
from understudy.graph.api import BlastRadiusCalculator, DependencyGraph
from understudy.kernel.api import SafetyKernel
from understudy.mirror.api import MirrorRegistry
from understudy.notify.api import Notifier
from understudy.orchestrator.api import Deps, Orchestrator, build_graph, run_incident
from understudy.planner.api import Planner
from understudy.playbook.api import PlaybookLibrary
from understudy.shadow.api import ShadowLoop
from understudy.signals.api import AlertSource, DeployHistory, ObservabilityAdapter
from understudy.store.api import EvalStore, PlaybookStore, RunStore
from understudy.tournament.api import Tournament


def test_all_protocols_are_protocols() -> None:
    """Verify that all component interfaces are valid typing protocols."""
    protocols = [
        RunStore,
        PlaybookStore,
        EvalStore,
        AlertSource,
        ObservabilityAdapter,
        DeployHistory,
        DependencyGraph,
        BlastRadiusCalculator,
        PlaybookLibrary,
        Planner,
        FleetController,
        MirrorRegistry,
        Tournament,
        SafetyKernel,
        Actuator,
        Notifier,
        ShadowLoop,
        Orchestrator,
        EvalHarness,
    ]
    for proto in protocols:
        assert inspect.isclass(proto)
        assert hasattr(proto, "_is_protocol") or hasattr(proto, "_is_runtime_protocol")


def test_orchestrator_function_stubs() -> None:
    """Verify that function stubs raise NotImplementedError."""
    deps = cast("Deps", object())
    with pytest.raises(NotImplementedError):
        build_graph(deps)

    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    alert = Alert(
        alert_id="alt_1",
        source="synthetic",
        title="Test",
        service="edge-gateway",
        severity="error",
        fired_at=now,
    )
    with pytest.raises(NotImplementedError):
        import asyncio

        asyncio.run(run_incident(alert, deps))
