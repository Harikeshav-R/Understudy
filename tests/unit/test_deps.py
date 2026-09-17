"""Unit tests for production dependency injection wiring.

Implements build-plan step 5.5:
- Validates that create_real_deps() constructs all real component implementations.
- Asserts that every field satisfies its respective Protocol.
- Tests dependency overrides and custom clock/settings/db injection.
- Tests fallback when dependencies.yaml is not present.
- Tests create_deps(fake=True) vs create_deps(fake=False).
- Tests IncidentOrchestrator protocol conformance and execution.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from understudy.actuator.api import Actuator
from understudy.actuator.production import ProductionActuator
from understudy.common.clock import Clock, FrozenClock, SystemClock
from understudy.contracts.enums import RunOutcome
from understudy.contracts.incident import Alert
from understudy.contracts.run import RunRecord
from understudy.deps import (
    IncidentOrchestrator,
    create_deps,
    create_real_deps,
)
from understudy.fleet.api import FleetController
from understudy.fleet.controller import K8sFleetController
from understudy.graph.api import (
    BlastRadiusCalculator,
    DependencyGraph,
    ServiceBlastRadiusCalculator,
    ServiceDependencyGraph,
)
from understudy.kernel.api import SafetyKernel, Z3SafetyKernel
from understudy.mirror.api import HttpMirrorRegistry, MirrorRegistry
from understudy.notify.api import (
    CompositeNotifier,
    Notifier,
    PagerDutyNotifier,
    SlackNotifier,
)
from understudy.orchestrator.api import (
    Deps,
    Orchestrator,
    create_fake_deps,
)
from understudy.orchestrator.fakes import FakeOrchestrator
from understudy.planner.api import LLMPlanner, Planner
from understudy.planner.fakes import FakePlanner
from understudy.playbook.api import PlaybookLibrary, PlaybookRetriever
from understudy.signals.api import (
    AlertSource,
    DeployHistory,
    GitHubDeployHistory,
    ObservabilityAdapter,
    PagerDutyAlertSource,
    PrometheusLokiAdapter,
)
from understudy.store.api import (
    EvalStore,
    PlaybookStore,
    RunStore,
)
from understudy.store.database import StoreDatabase
from understudy.store.fakes import FakeRunStore
from understudy.store.postgres import (
    PostgresEvalStore,
    PostgresPlaybookStore,
    PostgresRunStore,
)
from understudy.tournament.api import (
    RehearsalTournament,
    Tournament,
)


def test_create_real_deps_defaults() -> None:
    """Verify create_real_deps constructs all real component implementations."""
    deps = create_real_deps()

    assert isinstance(deps, Deps)

    # Store subsystem
    assert isinstance(deps.run_store, RunStore)
    assert isinstance(deps.run_store, PostgresRunStore)
    assert isinstance(deps.playbook_store, PlaybookStore)
    assert isinstance(deps.playbook_store, PostgresPlaybookStore)
    assert isinstance(deps.eval_store, EvalStore)
    assert isinstance(deps.eval_store, PostgresEvalStore)

    # Signals subsystem
    assert isinstance(deps.alert_source, AlertSource)
    assert isinstance(deps.alert_source, PagerDutyAlertSource)
    assert isinstance(deps.observability, ObservabilityAdapter)
    assert isinstance(deps.observability, PrometheusLokiAdapter)
    assert isinstance(deps.deploy_history, DeployHistory)
    assert isinstance(deps.deploy_history, GitHubDeployHistory)

    # Graph subsystem
    assert isinstance(deps.dependency_graph, DependencyGraph)
    assert isinstance(deps.dependency_graph, ServiceDependencyGraph)
    assert isinstance(deps.blast_calculator, BlastRadiusCalculator)
    assert isinstance(deps.blast_calculator, ServiceBlastRadiusCalculator)

    # Playbook & Planner
    assert isinstance(deps.playbook_library, PlaybookLibrary)
    assert isinstance(deps.playbook_library, PlaybookRetriever)
    assert isinstance(deps.planner, Planner)
    assert isinstance(deps.planner, LLMPlanner)

    # Fleet & Mirror
    assert isinstance(deps.fleet_controller, FleetController)
    assert isinstance(deps.fleet_controller, K8sFleetController)
    assert isinstance(deps.mirror_registry, MirrorRegistry)
    assert isinstance(deps.mirror_registry, HttpMirrorRegistry)

    # Tournament
    assert isinstance(deps.tournament, Tournament)
    assert isinstance(deps.tournament, RehearsalTournament)

    # Safety kernel
    assert isinstance(deps.safety_kernel, SafetyKernel)
    assert isinstance(deps.safety_kernel, Z3SafetyKernel)

    # Actuator
    assert isinstance(deps.actuator, Actuator)
    assert isinstance(deps.actuator, ProductionActuator)

    # Notifiers
    assert isinstance(deps.notifier, Notifier)
    assert isinstance(deps.notifier, CompositeNotifier)
    assert isinstance(deps.notifier.slack, SlackNotifier)
    assert isinstance(deps.notifier.pagerduty, PagerDutyNotifier)

    # Clock
    assert isinstance(deps.clock, Clock)
    assert isinstance(deps.clock, SystemClock)


def test_create_real_deps_custom_clock_and_db(tmp_path: Path) -> None:
    """Verify create_real_deps wires custom clock, settings, and database."""
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    frozen_clock = FrozenClock(initial_time=now)
    custom_db = StoreDatabase(dsn="postgresql://postgres:postgres@localhost:5434/test_db")

    manifest = tmp_path / "custom_dep.yaml"
    manifest.write_text(
        """
version: "1.0"
services:
  - name: edge-gateway
    description: Ingress gateway
  - name: auth-service
    description: Auth
edges:
  - source: edge-gateway
    target: auth-service
"""
    )

    deps = create_real_deps(
        clock=frozen_clock,
        store_db=custom_db,
        dependencies_path=manifest,
    )

    assert deps.clock is frozen_clock
    run_store = deps.run_store
    assert isinstance(run_store, PostgresRunStore)
    assert run_store._db is custom_db
    assert deps.dependency_graph.dependents("auth-service") == {"edge-gateway"}


def test_create_real_deps_fallback_manifest_when_file_not_found(tmp_path: Path) -> None:
    """Verify fallback DAG construction when dependencies manifest path does not exist."""
    missing_manifest = tmp_path / "non_existent_dependencies.yaml"
    deps = create_real_deps(dependencies_path=missing_manifest)

    assert isinstance(deps.dependency_graph, ServiceDependencyGraph)
    # Default fallback nodes contain the canonical demo services
    snapshot = deps.dependency_graph.snapshot()
    assert "edge-gateway" in snapshot.nodes
    assert "data-service" in snapshot.nodes


def test_create_real_deps_with_injected_dependency_graph() -> None:
    """Verify explicit dependency_graph argument bypasses manifest loading."""
    custom_graph = ServiceDependencyGraph(
        nodes=["srv-a", "srv-b"],
        edges=[("srv-a", "srv-b")],
    )
    deps = create_real_deps(dependency_graph=custom_graph)
    assert deps.dependency_graph is custom_graph
    assert deps.blast_calculator.calculate("srv-b", {"srv-a"}) >= 0.0


def test_create_real_deps_with_component_overrides() -> None:
    """Verify component override keyword arguments are properly assigned."""
    fake_planner = FakePlanner(seed=123)
    fake_store = FakeRunStore()

    deps = create_real_deps(
        planner=fake_planner,
        run_store=fake_store,
    )

    assert deps.planner is fake_planner
    assert deps.run_store is fake_store
    # Remaining components are still real
    assert isinstance(deps.tournament, RehearsalTournament)
    assert isinstance(deps.safety_kernel, Z3SafetyKernel)
    assert isinstance(deps.actuator, ProductionActuator)


def test_create_deps_factory() -> None:
    """Verify create_deps routes cleanly to fake or real based on fake flag."""
    fake_deps = create_deps(fake=True, seed=99, force_veto=True)
    assert isinstance(fake_deps, Deps)
    assert isinstance(fake_deps.run_store, FakeRunStore)
    assert isinstance(fake_deps.planner, FakePlanner)

    real_deps = create_deps(fake=False)
    assert isinstance(real_deps, Deps)
    assert isinstance(real_deps.run_store, PostgresRunStore)
    assert isinstance(real_deps.planner, LLMPlanner)


@pytest.mark.asyncio
async def test_incident_orchestrator_conformance_and_execution() -> None:
    """Verify IncidentOrchestrator protocol conformance and run_incident delegation."""
    fake_deps = create_fake_deps(seed=42)
    orchestrator = IncidentOrchestrator(deps=fake_deps)

    assert isinstance(orchestrator, Orchestrator)
    assert orchestrator.deps is fake_deps

    # Test with default deps construction
    with patch("understudy.deps.create_real_deps", return_value=fake_deps):
        default_orch = IncidentOrchestrator()
        assert default_orch.deps is fake_deps

    # Test run_incident execution
    alert = Alert(
        alert_id="test_orch_alert",
        source="synthetic",
        title="Test Alert",
        service="edge-gateway",
        severity="critical",
        fired_at=datetime.now(UTC),
        raw={},
    )

    record = await orchestrator.run_incident(alert)
    assert isinstance(record, RunRecord)
    assert record.outcome in (RunOutcome.EXECUTED, RunOutcome.ESCALATED)


def test_fake_incident_orchestrator_clock_wiring() -> None:
    """Verify FakeOrchestrator resolves clock from explicit clock or deps."""
    clock = FrozenClock()
    orch1 = FakeOrchestrator(clock=clock)
    assert orch1.clock is clock

    fake_deps = create_fake_deps(clock=clock)
    orch2 = FakeOrchestrator(deps=fake_deps)
    assert orch2.clock is clock
