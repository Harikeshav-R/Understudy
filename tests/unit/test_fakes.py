"""Comprehensive tests for all component fake implementations."""

from datetime import UTC, datetime

import pytest

from understudy.actuator.fakes import FakeActuator
from understudy.common.clock import SystemClock
from understudy.common.errors import ActuationError
from understudy.contracts.enums import (
    ActionType,
    FailureClass,
    KernelVerdictType,
    RunOutcome,
    TournamentOutcome,
)
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.kernel import Fact, KernelVerdict
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.contracts.run import RunRecord
from understudy.contracts.twin import TwinHandle
from understudy.eval.fakes import FakeEvalHarness
from understudy.fleet.fakes import FakeFleetController, FakeWorkloadReader
from understudy.graph.fakes import FakeBlastRadiusCalculator, FakeDependencyGraph
from understudy.kernel.fakes import FakeSafetyKernel
from understudy.mirror.fakes import FakeMirrorRegistry
from understudy.notify.fakes import FakeNotifier
from understudy.orchestrator.fakes import FakeOrchestrator, create_fake_deps
from understudy.planner.fakes import FakePlanner
from understudy.playbook.fakes import FakePlaybookLibrary
from understudy.shadow.fakes import FakeShadowLoop
from understudy.signals.fakes import (
    FakeAlertSource,
    FakeDeployHistory,
    FakeObservabilityAdapter,
)
from understudy.store.fakes import (
    FakeCheckpointStore,
    FakeEvalStore,
    FakePlaybookStore,
    FakeRunStore,
)
from understudy.tournament.fakes import FakeTournament

FIXED_NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def sample_context() -> IncidentContext:
    now = FIXED_NOW
    return IncidentContext(
        incident_id="inc_001",
        alert=Alert(
            alert_id="alt_001",
            source="synthetic",
            title="High latency",
            service="data-service",
            severity="critical",
            fired_at=now,
        ),
        signatures=[],
        metrics_window=MetricWindow(service="data-service", start_time=now, end_time=now),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(observed_at=now),
        gathered_at=now,
    )


@pytest.fixture
def sample_run_record(sample_context: IncidentContext) -> RunRecord:
    now = FIXED_NOW
    return RunRecord(
        run_id="run_001",
        incident_id="inc_001",
        started_at=now,
        finished_at=None,
        outcome=RunOutcome.EXECUTED,
        context=sample_context,
        plans=[],
        evidence=[],
    )


@pytest.mark.asyncio
async def test_fake_run_store(sample_run_record: RunRecord) -> None:
    store = FakeRunStore()
    assert await store.get_run("run_001") is None
    assert await store.list_runs() == []

    await store.record_run(sample_run_record)
    assert await store.get_run("run_001") == sample_run_record
    assert len(await store.list_runs()) == 1
    assert len(await store.list_runs(incident_id="inc_001")) == 1
    assert len(await store.list_runs(incident_id="other")) == 0
    assert len(await store.list_runs(scenario_id="sc_1")) == 0
    assert await store.get_active_runs() == [sample_run_record]

    finished_record = sample_run_record.model_copy(
        update={"run_id": "run_002", "finished_at": sample_run_record.started_at}
    )
    await store.record_run(finished_record)
    assert await store.get_active_runs() == [sample_run_record]


@pytest.mark.asyncio
async def test_fake_playbook_store() -> None:
    store = FakePlaybookStore()
    assert await store.get_playbook("pb_1") is None
    plan = RemediationPlan(
        plan_id="plan_1",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="data-service"),
        target_resources=[],
        declared_blast_set=[],
        rationale="noop",
        origin="playbook",
    )
    await store.save_playbook(
        playbook_id="pb_1",
        failure_class=FailureClass.CONFIG_DRIFT,
        signature_text="sig text",
        embedding=[0.1, 0.2],
        plan=plan,
        evidence_refs=["run_1"],
        origin="incident",
    )
    assert await store.get_playbook("pb_1") == plan
    search_res = await store.search_playbooks([0.1, 0.2], limit=1)
    assert len(search_res) == 1

    await store.increment_success("pb_1")
    await store.increment_failure("pb_1")
    # Coverage for non-existent key
    await store.increment_success("pb_nonexistent")
    await store.increment_failure("pb_nonexistent")


@pytest.mark.asyncio
async def test_fake_eval_store() -> None:
    store = FakeEvalStore()
    await store.record_scenario_result(
        scenario_id="sc_01",
        run_id="run_01",
        repeat_index=0,
        twin_predicted_success=True,
        prod_actual_success=True,
        expected_escalation=False,
        did_escalate=False,
    )
    results = await store.get_scenario_results()
    assert len(results) == 1
    filtered = await store.get_scenario_results("sc_01")
    assert len(filtered) == 1
    none_filtered = await store.get_scenario_results("other")
    assert len(none_filtered) == 0


@pytest.mark.asyncio
async def test_fake_checkpoint_store() -> None:
    store = FakeCheckpointStore()

    # Initial state empty
    assert await store.get_checkpoint("inc_1", "") is None
    assert await store.list_checkpoints() == []
    assert await store.get_blobs("inc_1", "", {"ch": 1}) == {}
    assert await store.get_writes("inc_1", "", "cp_1") == []

    # Put checkpoint and query
    await store.put_checkpoint(
        thread_id="inc_1",
        checkpoint_ns="",
        checkpoint_id="cp_001",
        parent_checkpoint_id=None,
        checkpoint=("msgpack", b"cp1"),
        metadata=("msgpack", b"md1"),
    )
    await store.put_checkpoint(
        thread_id="inc_1",
        checkpoint_ns="",
        checkpoint_id="cp_002",
        parent_checkpoint_id="cp_001",
        checkpoint=("msgpack", b"cp2"),
        metadata=("msgpack", b"md2"),
    )

    # Get specific and latest
    latest = await store.get_checkpoint("inc_1", "")
    assert latest is not None
    assert latest[0] == "cp_002"
    assert latest[3] == "cp_001"

    specific = await store.get_checkpoint("inc_1", "", "cp_001")
    assert specific is not None
    assert specific[0] == "cp_001"
    assert specific[3] is None

    assert await store.get_checkpoint("inc_1", "", "cp_missing") is None
    assert await store.get_checkpoint("inc_other", "") is None

    # List with filters and limits
    all_cps = await store.list_checkpoints(thread_id="inc_1")
    assert len(all_cps) == 2
    assert all_cps[0][2] == "cp_002"  # Reverse chronological

    before_cps = await store.list_checkpoints(thread_id="inc_1", before_checkpoint_id="cp_002")
    assert len(before_cps) == 1
    assert before_cps[0][2] == "cp_001"

    limited = await store.list_checkpoints(thread_id="inc_1", limit=1)
    assert len(limited) == 1
    assert limited[0][2] == "cp_002"

    # Blobs
    await store.put_blobs(
        [
            ("inc_1", "", "channel_a", 1, ("msgpack", b"blob_a")),
            ("inc_1", "", "channel_b", "v1.0", ("msgpack", b"blob_b")),
        ]
    )
    blobs = await store.get_blobs("inc_1", "", {"channel_a": 1, "channel_b": "v1.0", "ch_c": 2})
    assert len(blobs) == 2
    assert blobs["channel_a"] == ("msgpack", b"blob_a")
    assert blobs["channel_b"] == ("msgpack", b"blob_b")

    # Writes
    await store.put_writes(
        [
            ("inc_1", "", "cp_002", "task_1", 0, "out", ("msgpack", b"val"), "step_a"),
        ]
    )
    writes = await store.get_writes("inc_1", "", "cp_002")
    assert len(writes) == 1
    assert writes[0] == ("task_1", "out", ("msgpack", b"val"), "step_a")
    assert await store.get_writes("inc_1", "", "cp_empty") == []

    # Put data for a second thread to test delete_thread non-matching key branches
    await store.put_checkpoint("inc_2", "", "cp_inc2", None, ("msgpack", b"cp"), ("msgpack", b"md"))
    await store.put_blobs([("inc_2", "", "channel_2", 1, ("msgpack", b"blob_2"))])
    await store.put_writes(
        [("inc_2", "", "cp_inc2", "task_2", 0, "out", ("msgpack", b"val"), "step")]
    )

    # Delete thread inc_1 (inc_2 remains)
    await store.delete_thread("inc_1")
    assert await store.get_checkpoint("inc_1", "") is None
    assert await store.get_blobs("inc_1", "", {"channel_a": 1}) == {}
    assert await store.get_writes("inc_1", "", "cp_002") == []
    assert await store.get_checkpoint("inc_2", "") is not None
    assert await store.get_blobs("inc_2", "", {"channel_2": 1}) != {}
    assert await store.get_writes("inc_2", "", "cp_inc2") != []


@pytest.mark.asyncio
async def test_fake_alert_source() -> None:
    now = FIXED_NOW
    source = FakeAlertSource()
    alert1 = await source.receive_alert()
    assert alert1.alert_id == "alt_fake_001"

    custom_alert = Alert(
        alert_id="alt_custom",
        source="synthetic",
        title="Custom",
        service="worker",
        severity="error",
        fired_at=now,
    )
    await source.inject_synthetic_alert(custom_alert)
    alert2 = await source.receive_alert()
    assert alert2.alert_id == "alt_custom"


@pytest.mark.asyncio
async def test_fake_observability_adapter() -> None:
    adapter = FakeObservabilityAdapter(seed=123)
    now = FIXED_NOW
    window = await adapter.metric_window("edge-gateway", now)
    assert window.service == "edge-gateway"
    assert len(window.series) > 0

    sigs = await adapter.error_signatures("edge-gateway", now)
    assert len(sigs) == 1
    assert sigs[0].service == "edge-gateway"

    assert await adapter.service_health("ust-prod", "edge-gateway") is True


@pytest.mark.asyncio
async def test_fake_deploy_history() -> None:
    history = FakeDeployHistory()
    deploys = await history.recent_deploys(limit=1)
    assert len(deploys) == 1
    assert deploys[0].commit_sha == "c0ffee1"


def test_fake_dependency_graph_and_blast_calculator() -> None:
    graph = FakeDependencyGraph()
    assert "edge-gateway" in graph.dependents("auth-service")
    assert "edge-gateway" in graph.reachable_set("data-service")
    assert graph.request_share("edge-gateway") == 0.40
    assert graph.request_share("unknown") == 0.0
    snapshot = graph.snapshot()
    assert len(snapshot.nodes) == 4

    calc = FakeBlastRadiusCalculator()
    assert calc.calculate("data-service", set()) == 0.0
    assert calc.calculate("data-service", {"auth-service", "edge-gateway"}) == 0.5


@pytest.mark.asyncio
async def test_fake_playbook_library(sample_context: IncidentContext) -> None:
    library = FakePlaybookLibrary()
    cand = await library.retrieve_candidate(sample_context)
    assert cand is not None
    assert cand.origin == "playbook"

    await library.record_outcome("pb_1", success=True, evidence_run_id="run_1")
    assert len(library.recorded_outcomes) == 1


@pytest.mark.asyncio
async def test_fake_planner(sample_context: IncidentContext) -> None:
    planner = FakePlanner(seed=42)
    candidates = await planner.generate_candidates(sample_context, count=3)
    assert len(candidates) == 3
    assert candidates[0].action == ActionType.ROLLBACK_DEPLOY
    assert candidates[1].action == ActionType.SCALE_WORKLOAD
    assert candidates[2].action == ActionType.NO_ACTION


@pytest.mark.asyncio
async def test_fake_fleet_controller() -> None:
    fleet = FakeFleetController()
    twins = await fleet.fork(incident_id="inc_001", n=2)
    assert len(twins) == 2
    assert twins[0].twin_id == "twin_inc_001_0"

    await fleet.teardown(twins[0])
    assert len(fleet._twins["inc_001"]) == 1

    unknown_handle = TwinHandle(
        twin_id="twin_unknown",
        incident_id="inc_unknown",
        candidate_index=0,
        namespace="ust-twin-unknown-0",
        database="twin_unknown_db",
        forked_from_snapshot_at=FIXED_NOW,
        ready_at=None,
        state="ready",
    )
    await fleet.teardown(unknown_handle)

    await fleet.teardown_all("inc_001")
    assert "inc_001" not in fleet._twins


@pytest.mark.asyncio
async def test_fake_workload_reader() -> None:
    reader = FakeWorkloadReader()
    snapshot = await reader.read_workloads("ust-prod")
    assert snapshot.namespace == "ust-prod"
    assert len(snapshot.workloads) == 5
    assert "edge-gateway" in snapshot.workloads
    assert "prod-postgres" in snapshot.workloads
    assert "app-config" in snapshot.config_maps

    filtered = await reader.read_workloads("ust-prod", exclude_components={"database"})
    assert len(filtered.workloads) == 4
    assert "prod-postgres" not in filtered.workloads
    assert "data-service" in filtered.workloads
    data_svc = filtered.workloads["data-service"]
    assert len(data_svc.containers) == 1
    assert data_svc.containers[0].pinned_image.startswith("localhost:5001/data-service@sha256:")


@pytest.mark.asyncio
async def test_fake_mirror_registry() -> None:
    fleet = FakeFleetController()
    twins = await fleet.fork(incident_id="inc_001", n=1)
    mirror = FakeMirrorRegistry(drop_rate=0.05)

    stats_before = await mirror.get_stats("twin_inc_001_0")
    assert stats_before.delivered == 0

    await mirror.register_twin(twins[0])
    stats_after = await mirror.get_stats("twin_inc_001_0")
    assert stats_after.delivered == 100
    assert stats_after.dropped == 5

    await mirror.unregister_twin("twin_inc_001_0")
    stats_unreg = await mirror.get_stats("twin_inc_001_0")
    assert stats_unreg.delivered == 0


@pytest.mark.asyncio
async def test_fake_tournament() -> None:
    fleet = FakeFleetController()
    twins = await fleet.fork("inc_001", 2)
    plans = [
        RemediationPlan(
            plan_id="plan_0",
            candidate_index=0,
            action=ActionType.NO_ACTION,
            params=ActionParams(workload="data-service"),
            target_resources=[],
            declared_blast_set=[],
            rationale="noop",
            origin="planner",
        ),
        RemediationPlan(
            plan_id="plan_1",
            candidate_index=1,
            action=ActionType.SCALE_WORKLOAD,
            params=ActionParams(workload="data-service", replica_delta=1),
            target_resources=[],
            declared_blast_set=[],
            rationale="scale",
            origin="planner",
        ),
    ]
    tournament = FakeTournament(seed=42)
    evidence, result = await tournament.observe_and_score(twins, plans)
    assert len(evidence) == 2
    assert result.outcome == TournamentOutcome.DECIDED
    assert result.winner_plan_id == "plan_0"

    empty_res = tournament.arbitrate([], [])
    assert empty_res.outcome == TournamentOutcome.NO_VIABLE_CANDIDATE

    ambig_tournament = FakeTournament(force_ambiguous=True)
    _, ambig_result = await ambig_tournament.observe_and_score(twins, plans)
    assert ambig_result.outcome == TournamentOutcome.AMBIGUOUS
    assert ambig_result.winner_plan_id is None


@pytest.mark.asyncio
async def test_fake_safety_kernel() -> None:
    plan = RemediationPlan(
        plan_id="plan_0",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="data-service"),
        target_resources=[],
        declared_blast_set=[],
        rationale="noop",
        origin="planner",
    )
    now = FIXED_NOW
    facts = [Fact(name="replicas", value=3, source="k8s", observed_at=now)]

    kernel_pass = FakeSafetyKernel(force_verdict=KernelVerdictType.PASS)
    v_pass = await kernel_pass.verify(plan, facts)
    assert v_pass.verdict == KernelVerdictType.PASS

    kernel_veto = FakeSafetyKernel(force_verdict=KernelVerdictType.VETO)
    v_veto = await kernel_veto.verify(plan, facts)
    assert v_veto.verdict == KernelVerdictType.VETO

    kernel_unc = FakeSafetyKernel(force_verdict=KernelVerdictType.UNCERTAIN)
    v_unc = await kernel_unc.verify(plan, facts)
    assert v_unc.verdict == KernelVerdictType.UNCERTAIN


@pytest.mark.asyncio
async def test_fake_actuator() -> None:
    actuator = FakeActuator()
    plan = RemediationPlan(
        plan_id="plan_0",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="data-service"),
        target_resources=[],
        declared_blast_set=[],
        rationale="noop",
        origin="planner",
    )
    assert await actuator.apply(plan, "ust-twin-0") is True
    assert ("plan_0", "ust-twin-0") in actuator.applied_plans

    pass_verdict = KernelVerdict(
        incident_id="inc_1",
        plan_id="plan_0",
        verdict=KernelVerdictType.PASS,
        solver_ms=1.0,
        human_reason="ok",
    )
    assert await actuator.apply_to_production(plan, pass_verdict) is True

    veto_verdict = KernelVerdict(
        incident_id="inc_1",
        plan_id="plan_0",
        verdict=KernelVerdictType.VETO,
        solver_ms=1.0,
        human_reason="veto",
    )
    with pytest.raises(
        ActuationError, match="Cannot actuate plan plan_0 on production without PASS"
    ):
        await actuator.apply_to_production(plan, veto_verdict)

    assert await actuator.revert(plan, "ust-prod") is True
    assert ("plan_0", "ust-prod") in actuator.reverted_plans


@pytest.mark.asyncio
async def test_fake_notifier() -> None:
    notifier = FakeNotifier()
    await notifier.notify_slack("inc_001", "Plan executed")
    assert len(notifier.slack_posts) == 1

    await notifier.escalate_pagerduty("inc_001", "VETO triggered")
    assert len(notifier.pagerduty_escalations) == 1


@pytest.mark.asyncio
async def test_fake_shadow_loop() -> None:
    shadow = FakeShadowLoop()
    assert shadow.cycle_count == 0
    await shadow.run_cycle()
    assert shadow.cycle_count == 1


@pytest.mark.asyncio
async def test_fake_eval_harness() -> None:
    harness = FakeEvalHarness()
    rec = await harness.run_scenario("scenario_bad_deploy")
    assert rec.scenario_id == "scenario_bad_deploy"
    assert rec.outcome == RunOutcome.EXECUTED

    corpus = await harness.run_corpus()
    assert corpus["total_scenarios"] == 12


@pytest.mark.asyncio
async def test_fake_orchestrator() -> None:
    deps = create_fake_deps(seed=42)
    orchestrator = FakeOrchestrator(deps=deps, seed=42)
    now = FIXED_NOW
    alert = Alert(
        alert_id="alt_001",
        source="synthetic",
        title="Alert",
        service="data-service",
        severity="critical",
        fired_at=now,
    )
    run_rec = await orchestrator.run_incident(alert)
    assert run_rec.outcome == RunOutcome.EXECUTED
    assert run_rec.run_id == "run_alt_001"
    assert await deps.run_store.get_run("run_alt_001") is not None


@pytest.mark.asyncio
async def test_fake_orchestrator_defaults_without_clock_or_deps() -> None:
    orchestrator = FakeOrchestrator()
    assert isinstance(orchestrator.clock, SystemClock)


@pytest.mark.asyncio
async def test_fakes_clock_injection_and_determinism() -> None:
    from understudy.common.clock import FrozenClock

    frozen_time = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    clock = FrozenClock(frozen_time)
    deps = create_fake_deps(seed=42, clock=clock)

    # Verify AlertSource uses frozen clock
    alert = await deps.alert_source.receive_alert()
    assert alert.fired_at == frozen_time

    # Verify ObservabilityAdapter uses frozen clock
    metrics = await deps.observability.metric_window("data-service", frozen_time)
    assert metrics.end_time == frozen_time

    # Verify DeployHistory uses frozen clock
    deploys = await deps.deploy_history.recent_deploys()
    assert deploys[0].deployed_at == frozen_time

    # Verify FleetController uses frozen clock
    twins = await deps.fleet_controller.fork("inc_001", 1)
    assert twins[0].forked_from_snapshot_at == frozen_time

    # Verify DependencyGraph uses frozen clock
    snap = deps.dependency_graph.snapshot()
    assert snap.observed_at == frozen_time

    # Verify Orchestrator uses frozen clock
    orchestrator = FakeOrchestrator(deps=deps, clock=clock)
    rec = await orchestrator.run_incident(alert)
    assert rec.started_at == frozen_time

    # Verify Planner determinism with seed
    p1 = FakePlanner(seed=42)
    p2 = FakePlanner(seed=43)
    c1 = await p1.generate_candidates(rec.context)
    c2 = await p2.generate_candidates(rec.context)
    assert c1[1].params.replica_delta != c2[1].params.replica_delta

    # Verify Tournament determinism with seed
    t1 = FakeTournament(seed=42, clock=clock)
    t2 = FakeTournament(seed=43, clock=clock)
    _, res1 = await t1.observe_and_score(twins, c1[:1])
    _, res2 = await t2.observe_and_score(twins, c1[:1])
    assert res1.scores[0].composite != res2.scores[0].composite

    # Verify EvalHarness uses clock and seed
    harness = FakeEvalHarness(seed=45, clock=clock)
    eval_rec = await harness.run_scenario("scenario_test")
    assert eval_rec.started_at == frozen_time
