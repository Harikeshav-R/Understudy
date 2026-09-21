"""Production dependency container wiring and incident orchestrator implementation.

Implements build-plan step 5.5 and ADR-007:
- Assembles production Deps container from real component implementations.
- Connects telemetry adapters, stores, planner, safety kernel, tournament, actuator, and notifiers.
- Provides unified create_deps(fake=...) factory preserving --fake behavior.
- Implements IncidentOrchestrator conforming to Orchestrator protocol.
"""

from pathlib import Path
from typing import Any

from understudy.actuator.api import Actuator
from understudy.actuator.production import ProductionActuator
from understudy.common.clock import Clock, SystemClock, resolve_clock
from understudy.common.config import Settings, TimeoutSettings, get_settings
from understudy.contracts.incident import Alert
from understudy.contracts.run import RunRecord
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
from understudy.orchestrator.api import Deps, Orchestrator
from understudy.orchestrator.fakes import create_fake_deps
from understudy.planner.api import LLMPlanner, Planner
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
    CheckpointStore,
    EvalStore,
    PlaybookStore,
    RunStore,
)
from understudy.store.database import StoreDatabase
from understudy.store.postgres import (
    PostgresEvalStore,
    PostgresPlaybookStore,
    PostgresRunStore,
)
from understudy.tournament.api import (
    AdvisoryLLMJudge,
    BlastCoordinator,
    DeterministicCandidateScorer,
    ProbeSampler,
    RehearsalTournament,
    Tournament,
    TournamentArbiter,
)


def create_real_deps(
    settings: Settings | None = None,
    clock: Clock | None = None,
    store_db: StoreDatabase | None = None,
    dependencies_path: Path | str | None = None,
    *,
    run_store: RunStore | None = None,
    playbook_store: PlaybookStore | None = None,
    eval_store: EvalStore | None = None,
    alert_source: AlertSource | None = None,
    observability: ObservabilityAdapter | None = None,
    deploy_history: DeployHistory | None = None,
    dependency_graph: DependencyGraph | None = None,
    blast_calculator: BlastRadiusCalculator | None = None,
    playbook_library: PlaybookLibrary | None = None,
    planner: Planner | None = None,
    fleet_controller: FleetController | None = None,
    mirror_registry: MirrorRegistry | None = None,
    tournament: Tournament | None = None,
    safety_kernel: SafetyKernel | None = None,
    actuator: Actuator | None = None,
    notifier: Notifier | None = None,
    checkpoint_store: CheckpointStore | None = None,
    timeouts: TimeoutSettings | None = None,
) -> Deps:
    """Construct and return a Deps container wired with production implementations."""
    active_settings = settings or get_settings()
    active_clock = resolve_clock(clock or SystemClock())
    res_timeouts = timeouts or active_settings.timeouts

    # 1. Store subsystem
    db = store_db or StoreDatabase(dsn=active_settings.endpoints.postgres_system)
    res_run_store = run_store or PostgresRunStore(db=db)
    res_playbook_store = playbook_store or PostgresPlaybookStore(db=db)
    res_eval_store = eval_store or PostgresEvalStore(db=db)

    # 2. Signals subsystem
    res_alert_source = alert_source or PagerDutyAlertSource(clock=active_clock)
    res_observability = observability or PrometheusLokiAdapter(clock=active_clock)
    res_deploy_history = deploy_history or GitHubDeployHistory(clock=active_clock)

    # 3. Graph topology and blast calculation
    if dependency_graph is not None:
        res_dep_graph = dependency_graph
    else:
        manifest_file = Path(dependencies_path or "deploy/prod/dependencies.yaml")
        if manifest_file.is_file():
            res_dep_graph = ServiceDependencyGraph.from_yaml(manifest_file, clock=active_clock)
        else:
            res_dep_graph = ServiceDependencyGraph(
                nodes=["edge-gateway", "auth-service", "data-service", "worker"],
                edges=[
                    ("edge-gateway", "auth-service"),
                    ("edge-gateway", "data-service"),
                    ("auth-service", "data-service"),
                    ("worker", "data-service"),
                ],
                clock=active_clock,
            )

    res_blast_calc = blast_calculator or ServiceBlastRadiusCalculator(graph=res_dep_graph)

    # 4. Playbook library and planner
    res_playbook_lib = playbook_library or PlaybookRetriever(
        store=res_playbook_store,
        settings=active_settings,
    )
    res_planner = planner or LLMPlanner(
        settings=active_settings,
    )

    # 5. Fleet controller and mirror registry
    res_fleet = fleet_controller or K8sFleetController(
        clock=active_clock,
        settings=active_settings,
    )
    res_mirror = mirror_registry or HttpMirrorRegistry(
        settings=active_settings,
    )

    # 6. Tournament subsystem
    probe = ProbeSampler(observability=res_observability, clock=active_clock)
    blast = BlastCoordinator(
        observability=res_observability,
        dependency_graph=res_dep_graph,
        clock=active_clock,
    )
    scorer = DeterministicCandidateScorer()
    judge = AdvisoryLLMJudge()
    arbiter = TournamentArbiter(clock=active_clock)
    res_tournament = tournament or RehearsalTournament(
        probe=probe,
        blast=blast,
        scorer=scorer,
        judge=judge,
        mirror=res_mirror,
        arbiter=arbiter,
        clock=active_clock,
    )

    # 7. Safety kernel
    res_kernel = safety_kernel or Z3SafetyKernel(clock=active_clock)

    # 8. Actuator
    res_actuator = actuator or ProductionActuator(
        probe=probe,
        run_store=res_run_store,
        deploy_history=res_deploy_history,
        clock=active_clock,
        settings=active_settings,
    )

    # 9. Notification channels
    res_notifier = notifier or CompositeNotifier(
        slack=SlackNotifier(),
        pagerduty=PagerDutyNotifier(),
    )

    return Deps(
        run_store=res_run_store,
        playbook_store=res_playbook_store,
        eval_store=res_eval_store,
        alert_source=res_alert_source,
        observability=res_observability,
        deploy_history=res_deploy_history,
        dependency_graph=res_dep_graph,
        blast_calculator=res_blast_calc,
        playbook_library=res_playbook_lib,
        planner=res_planner,
        fleet_controller=res_fleet,
        mirror_registry=res_mirror,
        tournament=res_tournament,
        safety_kernel=res_kernel,
        actuator=res_actuator,
        notifier=res_notifier,
        clock=active_clock,
        checkpoint_store=checkpoint_store,
        timeouts=res_timeouts,
        workload_reader=getattr(res_fleet, "workload_reader", None),
    )


def create_deps(
    fake: bool = False,
    *,
    seed: int = 42,
    force_veto: bool = False,
    clock: Clock | None = None,
    settings: Settings | None = None,
    **overrides: Any,
) -> Deps:
    """Factory creating Deps container: real implementations by default, fakes when fake=True."""
    if fake:
        return create_fake_deps(seed=seed, clock=clock, force_veto=force_veto)
    return create_real_deps(settings=settings, clock=clock, **overrides)


class IncidentOrchestrator(Orchestrator):
    """Production orchestrator executing compiled StateGraph with real dependencies."""

    def __init__(self, deps: Deps | None = None) -> None:
        self.deps = deps or create_real_deps()

    async def run_incident(self, alert: Alert) -> RunRecord:
        """Run incident control loop from alert to terminal outcome using wired dependencies."""
        from understudy.orchestrator.graph import run_incident as _run_incident

        return await _run_incident(alert, self.deps)


__all__ = [
    "IncidentOrchestrator",
    "create_deps",
    "create_real_deps",
]
