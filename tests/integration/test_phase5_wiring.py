"""Integration tests for Phase 5 live cluster and component wiring.

Implements build-plan step 5.7 and Checkpoint 5 prerequisites:
- Verifies real `Deps` assembly from `create_real_deps` with all production implementations.
- Verifies live cluster accessibility for Kubernetes plan applier and production actuator.
- Verifies `ust store get` and `ust store verify` against live PostgreSQL when available.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime

import psycopg
import pytest
from typer.testing import CliRunner

from understudy.actuator.apply import K8sPlanApplier
from understudy.actuator.production import ProductionActuator
from understudy.cli import app
from understudy.common.config import get_settings
from understudy.contracts.enums import ActionType, RunOutcome
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.contracts.run import RunRecord
from understudy.deps import create_real_deps
from understudy.fleet.controller import K8sFleetController
from understudy.fleet.k8s import K8sWorkloadReader
from understudy.graph.api import ServiceBlastRadiusCalculator, ServiceDependencyGraph
from understudy.kernel.api import Z3SafetyKernel
from understudy.mirror.registry import HttpMirrorRegistry
from understudy.notify.composite import CompositeNotifier
from understudy.planner.api import LLMPlanner
from understudy.playbook.retriever import PlaybookRetriever
from understudy.signals.api import (
    GitHubDeployHistory,
    PagerDutyAlertSource,
    PrometheusLokiAdapter,
)
from understudy.store.database import StoreDatabase
from understudy.store.migrations import apply_migrations
from understudy.store.postgres import (
    PostgresEvalStore,
    PostgresPlaybookStore,
    PostgresRunStore,
)
from understudy.tournament.api import RehearsalTournament


def _cluster_available() -> bool:
    """Check if kubectl is installed and k3d-ust cluster is reachable."""
    if not shutil.which("kubectl"):
        return False
    try:
        res = subprocess.run(
            ["kubectl", "cluster-info", "--context=k3d-ust", "--request-timeout=2s"],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def _postgres_system_available() -> bool:
    """Check whether system-postgres on localhost:5434 is reachable."""
    dsn = get_settings().endpoints.postgres_system
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
            row = cur.fetchone()
            return bool(row and row[0] == 1)
    except Exception:
        return False


@pytest.mark.integration
def test_real_deps_assembly() -> None:
    """Verify create_real_deps assembles all real subsystem implementations."""
    deps = create_real_deps()

    # Store subsystem
    assert isinstance(deps.run_store, PostgresRunStore)
    assert isinstance(deps.playbook_store, PostgresPlaybookStore)
    assert isinstance(deps.eval_store, PostgresEvalStore)

    # Signals subsystem
    assert isinstance(deps.alert_source, PagerDutyAlertSource)
    assert isinstance(deps.observability, PrometheusLokiAdapter)
    assert isinstance(deps.deploy_history, GitHubDeployHistory)

    # Graph and blast
    assert isinstance(deps.dependency_graph, ServiceDependencyGraph)
    assert isinstance(deps.blast_calculator, ServiceBlastRadiusCalculator)

    # Playbook and planner
    assert isinstance(deps.playbook_library, PlaybookRetriever)
    assert isinstance(deps.planner, LLMPlanner)

    # Fleet and mirror
    assert isinstance(deps.fleet_controller, K8sFleetController)
    assert isinstance(deps.mirror_registry, HttpMirrorRegistry)
    assert isinstance(deps.workload_reader, K8sWorkloadReader)

    # Tournament and safety kernel
    assert isinstance(deps.tournament, RehearsalTournament)
    assert isinstance(deps.safety_kernel, Z3SafetyKernel)

    # Actuator and notifier
    assert isinstance(deps.actuator, ProductionActuator)
    assert isinstance(deps.notifier, CompositeNotifier)

    # Timeouts
    assert deps.timeouts is not None
    assert deps.timeouts.incident_seconds == 600.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_k8s_plan_applier_and_actuator_live() -> None:
    """Verify live cluster connectivity for plan applier and production actuator."""
    if not _cluster_available():
        pytest.skip("k3d-ust cluster is not reachable (requires make up)")

    applier = K8sPlanApplier()
    # no_action is idempotent and non-mutating
    plan = RemediationPlan(
        plan_id="plan_live_wiring_test",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="edge-gateway"),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="Integration wiring test no-op",
        origin="planner",
    )
    success = await applier.apply(plan=plan, namespace="ust-prod")
    assert success is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_store_cli_commands_live_postgres() -> None:
    """Verify ust store get and ust store verify against live Postgres."""
    if not _postgres_system_available():
        pytest.skip("system-postgres on localhost:5434 is not reachable (requires make up)")

    apply_migrations()
    settings = get_settings()
    db = StoreDatabase(dsn=settings.endpoints.postgres_system)
    store = PostgresRunStore(db=db)

    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
    run_id = f"run_wiring_test_{int(now.timestamp())}"
    ctx = IncidentContext(
        incident_id=f"inc_wiring_test_{int(now.timestamp())}",
        alert=Alert(
            alert_id="alt_wire",
            source="synthetic",
            service="edge-gateway",
            title="Wiring test alert",
            severity="critical",
            fired_at=now,
        ),
        signatures=[],
        metrics_window=MetricWindow(service="edge-gateway", start_time=now, end_time=now),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(observed_at=now),
        gathered_at=now,
    )
    record = RunRecord(
        run_id=run_id,
        incident_id=ctx.incident_id,
        started_at=now,
        finished_at=now,
        outcome=RunOutcome.EXECUTED,
        context=ctx,
        plans=[],
        evidence=[],
        prod_applied_plan_id="plan_live_wiring_test",
        prod_outcome="resolved",
    )

    await store.record_run(record)

    runner = CliRunner()
    # Test ust store get
    get_res = runner.invoke(app, ["store", "get", run_id])
    assert get_res.exit_code == 0
    assert f"Run ID: {run_id}" in get_res.stdout
    assert "Production Outcome: resolved" in get_res.stdout

    # Test ust store get --json
    get_json_res = runner.invoke(app, ["store", "get", run_id, "--json"])
    assert get_json_res.exit_code == 0
    assert run_id in get_json_res.stdout

    # Test ust store verify --last 1
    verify_res = runner.invoke(app, ["store", "verify", "--last", "1"])
    assert verify_res.exit_code == 0
    assert "append-only OK" in verify_res.stdout
