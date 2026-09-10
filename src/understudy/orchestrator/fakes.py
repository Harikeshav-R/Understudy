"""Deterministic fake orchestrator and fake dependency builder."""

from understudy.actuator.fakes import FakeActuator
from understudy.common.clock import Clock, SystemClock
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


def create_fake_deps(seed: int = 42, clock: Clock | None = None) -> Deps:
    """Construct and return a Deps container wired entirely with deterministic fakes."""
    active_clock = clock or SystemClock()
    return Deps(
        run_store=FakeRunStore(),
        playbook_store=FakePlaybookStore(),
        eval_store=FakeEvalStore(),
        alert_source=FakeAlertSource(clock=active_clock),
        observability=FakeObservabilityAdapter(seed=seed, clock=active_clock),
        deploy_history=FakeDeployHistory(clock=active_clock),
        dependency_graph=FakeDependencyGraph(clock=active_clock),
        blast_calculator=FakeBlastRadiusCalculator(),
        playbook_library=FakePlaybookLibrary(),
        planner=FakePlanner(seed=seed),
        fleet_controller=FakeFleetController(clock=active_clock),
        mirror_registry=FakeMirrorRegistry(),
        tournament=FakeTournament(seed=seed, clock=active_clock),
        safety_kernel=FakeSafetyKernel(),
        actuator=FakeActuator(),
        notifier=FakeNotifier(),
    )


class FakeOrchestrator(Orchestrator):
    """Deterministic orchestrator fake returning executed run records."""

    def __init__(
        self,
        deps: Deps | None = None,
        seed: int = 42,
        clock: Clock | None = None,
    ) -> None:
        self.clock = clock or SystemClock()
        self.deps = deps or create_fake_deps(seed=seed, clock=self.clock)
        self.seed = seed

    async def run_incident(self, alert: Alert) -> RunRecord:
        """Simulate incident control loop using fake dependencies."""
        now = self.clock.now()
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
