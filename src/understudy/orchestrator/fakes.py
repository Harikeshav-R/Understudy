"""Deterministic fake orchestrator and fake dependency builder."""

from datetime import UTC, datetime

from understudy.actuator.fakes import FakeActuator
from understudy.contracts.enums import RunOutcome
from understudy.contracts.incident import (
    Alert,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.run import RunRecord
from understudy.fleet.fakes import FakeFleetController
from understudy.graph.fakes import FakeBlastRadiusCalculator, FakeDependencyGraph
from understudy.kernel.fakes import FakeSafetyKernel
from understudy.mirror.fakes import FakeMirrorRegistry
from understudy.notify.fakes import FakeNotifier
from understudy.orchestrator.api import Deps, Orchestrator
from understudy.planner.fakes import FakePlanner
from understudy.playbook.fakes import FakePlaybookLibrary
from understudy.signals.fakes import (
    FakeAlertSource,
    FakeDeployHistory,
    FakeObservabilityAdapter,
)
from understudy.store.fakes import FakeEvalStore, FakePlaybookStore, FakeRunStore
from understudy.tournament.fakes import FakeTournament


def create_fake_deps(seed: int = 42) -> Deps:
    """Construct and return a Deps container wired entirely with deterministic fakes."""
    return Deps(
        run_store=FakeRunStore(),
        playbook_store=FakePlaybookStore(),
        eval_store=FakeEvalStore(),
        alert_source=FakeAlertSource(),
        observability=FakeObservabilityAdapter(seed=seed),
        deploy_history=FakeDeployHistory(),
        dependency_graph=FakeDependencyGraph(),
        blast_calculator=FakeBlastRadiusCalculator(),
        playbook_library=FakePlaybookLibrary(),
        planner=FakePlanner(seed=seed),
        fleet_controller=FakeFleetController(),
        mirror_registry=FakeMirrorRegistry(),
        tournament=FakeTournament(seed=seed),
        safety_kernel=FakeSafetyKernel(),
        actuator=FakeActuator(),
        notifier=FakeNotifier(),
    )


class FakeOrchestrator(Orchestrator):
    """Deterministic orchestrator fake returning executed run records."""

    def __init__(self, deps: Deps | None = None, seed: int = 42) -> None:
        self.deps = deps or create_fake_deps(seed=seed)
        self.seed = seed

    async def run_incident(self, alert: Alert) -> RunRecord:
        """Simulate incident control loop using fake dependencies."""
        now = datetime.now(UTC)
        context = IncidentContext(
            incident_id=f"inc_fake_{alert.alert_id}",
            alert=alert,
            signatures=[],
            metrics_window=MetricWindow(service=alert.service, start_time=now, end_time=now),
            recent_deploys=[],
            dependency_graph=self.deps.dependency_graph.snapshot(),
            gathered_at=now,
        )
        record = RunRecord(
            run_id=f"run_{alert.alert_id}",
            incident_id=context.incident_id,
            started_at=now,
            finished_at=now,
            outcome=RunOutcome.EXECUTED,
            context=context,
            plans=[],
            evidence=[],
            prod_applied_plan_id="plan_cand_0",
            prod_outcome="resolved",
        )
        await self.deps.run_store.record_run(record)
        return record
