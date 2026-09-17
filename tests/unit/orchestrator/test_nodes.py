"""Unit tests for orchestrator nodes implementing architecture §2.2 and §2.9."""

from datetime import UTC, datetime

import pytest

from understudy.actuator.api import Actuator
from understudy.common.errors import ActuationError, OrchestratorError
from understudy.contracts.enums import (
    ActionType,
    KernelVerdictType,
    RunOutcome,
    TournamentOutcome,
)
from understudy.contracts.evidence import CandidateEvidence, CandidateScore, TournamentResult
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.kernel import KernelVerdict
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.contracts.twin import MirrorStats, TwinHandle
from understudy.fleet.fakes import FakeFleetController
from understudy.kernel.fakes import FakeSafetyKernel
from understudy.mirror.fakes import FakeMirrorRegistry
from understudy.notify.fakes import FakeNotifier
from understudy.orchestrator.api import Deps
from understudy.orchestrator.fakes import create_fake_deps
from understudy.orchestrator.nodes import (
    ALL_NODES,
    actuate,
    apply_candidates,
    escalate_pagerduty,
    fork_fleet,
    gather_context,
    handle_failure,
    ingest,
    notify_slack,
    observe,
    plan_candidates,
    record_run,
    register_mirrors,
    safety_kernel,
    teardown_fleet,
    tournament,
)
from understudy.orchestrator.nodes.actuate import node as actuate_node
from understudy.orchestrator.nodes.apply_candidates import node as apply_candidates_node
from understudy.orchestrator.nodes.escalate_pagerduty import node as escalate_pagerduty_node
from understudy.orchestrator.nodes.fork_fleet import node as fork_fleet_node
from understudy.orchestrator.nodes.gather_context import node as gather_context_node
from understudy.orchestrator.nodes.handle_failure import node as handle_failure_node
from understudy.orchestrator.nodes.ingest import node as ingest_node
from understudy.orchestrator.nodes.notify_slack import node as notify_slack_node
from understudy.orchestrator.nodes.observe import node as observe_node
from understudy.orchestrator.nodes.plan_candidates import node as plan_candidates_node
from understudy.orchestrator.nodes.record_run import node as record_run_node
from understudy.orchestrator.nodes.register_mirrors import node as register_mirrors_node
from understudy.orchestrator.nodes.safety_kernel import node as safety_kernel_node
from understudy.orchestrator.nodes.teardown_fleet import node as teardown_fleet_node
from understudy.orchestrator.nodes.tournament import node as tournament_node
from understudy.orchestrator.state import State
from understudy.playbook.fakes import FakePlaybookLibrary


def _sample_alert(alert_id: str = "alt_1", service: str = "data-service") -> Alert:
    return Alert(
        alert_id=alert_id,
        source="synthetic",
        title="High error rate",
        service=service,
        severity="critical",
        fired_at=datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC),
        raw={"key": "val"},
    )


def _sample_context(incident_id: str = "inc_123") -> IncidentContext:
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    alert = _sample_alert()
    return IncidentContext(
        incident_id=incident_id,
        alert=alert,
        signatures=[],
        metrics_window=MetricWindow(service=alert.service, start_time=now, end_time=now),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(nodes=[alert.service], edges=[], observed_at=now),
        gathered_at=now,
    )


def _sample_plan(
    plan_id: str = "plan_0",
    candidate_index: int = 0,
    has_inverse: bool = True,
    action: ActionType = ActionType.RESTART_WORKLOAD,
    origin: str = "planner",
    playbook_id: str | None = None,
    with_resources: bool = True,
) -> RemediationPlan:
    inv = None
    if has_inverse:
        inv = RemediationPlan(
            plan_id=f"{plan_id}_inv",
            candidate_index=candidate_index,
            action=action,
            params=ActionParams(workload="data-service"),
            target_resources=[],
            declared_blast_set=["data-service"],
            inverse=None,
            rationale="Inverse plan",
            origin="planner",
        )
    target_resources = (
        [ResourceRef(namespace="ust-twin", kind="Deployment", name="data-service")]
        if with_resources
        else []
    )
    return RemediationPlan(
        plan_id=plan_id,
        candidate_index=candidate_index,
        action=action,
        params=ActionParams(workload="data-service"),
        target_resources=target_resources,
        declared_blast_set=["data-service"],
        inverse=inv,
        rationale="Candidate test plan",
        origin=origin,  # type: ignore[arg-type]
        playbook_id=playbook_id,
    )


def _sample_twin(twin_id: str = "twin_0", index: int = 0, state: str = "ready") -> TwinHandle:
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    return TwinHandle(
        twin_id=twin_id,
        incident_id="inc_123",
        candidate_index=index,
        namespace=f"ust-twin-{twin_id}",
        database=f"twin_{twin_id}",
        forked_from_snapshot_at=now,
        ready_at=now,
        state=state,  # type: ignore[arg-type]
    )


def test_all_nodes_registry() -> None:
    assert len(ALL_NODES) == 15
    for name in [
        "ingest",
        "gather_context",
        "plan_candidates",
        "fork_fleet",
        "register_mirrors",
        "apply_candidates",
        "observe",
        "tournament",
        "safety_kernel",
        "actuate",
        "notify_slack",
        "escalate_pagerduty",
        "teardown_fleet",
        "record_run",
        "handle_failure",
    ]:
        assert name in ALL_NODES


@pytest.mark.asyncio
async def test_ingest_node() -> None:
    deps = create_fake_deps()

    # Case 1: Alert is None, receive from alert_source
    res1 = await ingest(State(), deps)
    assert res1["alert"] is not None
    assert res1["incident_id"] == f"inc_{res1['alert'].alert_id}"
    assert res1["started_at"] is not None

    # Case 2: State already has alert and incident_id and started_at
    fixed_time = datetime(2026, 9, 11, 10, 0, 0, tzinfo=UTC)
    alert = _sample_alert("custom_alt")
    state2 = State(incident_id="inc_custom", alert=alert, started_at=fixed_time)
    res2 = await ingest_node(state2, deps)
    assert res2["incident_id"] == "inc_custom"
    assert res2["alert"] == alert
    assert res2["started_at"] == fixed_time

    # Case 3: Alert without alert_id
    alert_no_id = Alert(
        alert_id="",
        source="synthetic",
        title="Title",
        service="data-service",
        severity="error",
        fired_at=fixed_time,
    )
    res3 = await ingest(State(alert=alert_no_id), deps)
    assert res3["incident_id"].startswith("inc_")


@pytest.mark.asyncio
async def test_gather_context_node() -> None:
    deps = create_fake_deps()

    # Case 1: Missing alert raises OrchestratorError
    with pytest.raises(OrchestratorError, match="Cannot gather context without an active alert"):
        await gather_context(State(), deps)

    # Case 2: Alert present with service
    alert = _sample_alert("alt_10", service="data-service")
    state = State(incident_id="inc_123", alert=alert)
    res = await gather_context_node(state, deps)
    assert "context" in res
    ctx = res["context"]
    assert ctx.incident_id == "inc_123"
    assert ctx.alert == alert
    assert ctx.metrics_window.service == "data-service"

    # Case 3: Alert with empty service string defaults to "default"
    alert_no_svc = Alert(
        alert_id="alt_11",
        source="synthetic",
        title="Title",
        service="",
        severity="error",
        fired_at=datetime.now(UTC),
    )
    res_no_svc = await gather_context(State(alert=alert_no_svc), deps)
    assert res_no_svc["context"].metrics_window.service == "default"


@pytest.mark.asyncio
async def test_plan_candidates_node() -> None:
    deps = create_fake_deps()

    # Case 1: Missing context raises OrchestratorError
    with pytest.raises(OrchestratorError, match="Cannot plan candidates without incident context"):
        await plan_candidates(State(), deps)

    # Case 2: Standard candidate planning with playbook match
    ctx = _sample_context("inc_123")
    state = State(incident_id="inc_123", context=ctx)
    res = await plan_candidates_node(state, deps)
    plans = res["plans"]
    assert len(plans) == 5  # 4 from FakePlanner (3 active + NO_ACTION) + 1 from FakePlaybookLibrary
    for idx, p in enumerate(plans):
        assert p.candidate_index == idx

    # Case 3: Playbook candidate is None
    class EmptyPlaybookLibrary(FakePlaybookLibrary):
        async def retrieve_candidate(self, incident: IncidentContext) -> RemediationPlan | None:
            _ = incident
            return None

    deps_no_pb = Deps(
        **{
            **deps.__dict__,
            "playbook_library": EmptyPlaybookLibrary(),
        }
    )
    res_no_pb = await plan_candidates(state, deps_no_pb)
    assert len(res_no_pb["plans"]) == 4

    # Case 4: Playbook candidate plan_id already in planner plans
    p0 = _sample_plan("plan_cand_0", 0)

    class DuplicatePlaybookLibrary(FakePlaybookLibrary):
        async def retrieve_candidate(self, incident: IncidentContext) -> RemediationPlan | None:
            _ = incident
            return p0

    deps_dup_pb = Deps(
        **{
            **deps.__dict__,
            "playbook_library": DuplicatePlaybookLibrary(),
        }
    )
    res_dup = await plan_candidates(state, deps_dup_pb)
    assert len(res_dup["plans"]) == 4


@pytest.mark.asyncio
async def test_fork_fleet_node() -> None:
    deps = create_fake_deps()

    # Case 1: Missing incident_id
    with pytest.raises(OrchestratorError, match="Cannot fork fleet without incident_id"):
        await fork_fleet(State(plans=[_sample_plan()]), deps)

    # Case 2: Missing plans
    with pytest.raises(OrchestratorError, match="Cannot fork fleet without remediation plans"):
        await fork_fleet(State(incident_id="inc_123"), deps)

    # Case 3: Valid fork
    p0 = _sample_plan("plan_0", 0)
    p1 = _sample_plan("plan_1", 1)
    state = State(incident_id="inc_123", plans=[p0, p1])
    res = await fork_fleet_node(state, deps)
    assert len(res["twins"]) == 2
    assert res["twins"][0].state == "ready"


@pytest.mark.asyncio
async def test_register_mirrors_node() -> None:
    deps = create_fake_deps()
    t0 = _sample_twin("twin_0", 0)
    t1 = _sample_twin("twin_1", 1)
    state = State(incident_id="inc_123", twins=[t0, t1])

    res = await register_mirrors_node(state, deps)
    assert res == {}
    stats0 = await deps.mirror_registry.get_stats("twin_0")
    assert stats0.delivered == 100

    res2 = await register_mirrors(state, deps)
    assert res2 == {}


@pytest.mark.asyncio
async def test_apply_candidates_node() -> None:
    deps = create_fake_deps()
    p0 = _sample_plan("plan_0", 0)
    p1 = _sample_plan("plan_1", 1)
    t0 = _sample_twin("twin_0", 0)
    t1 = _sample_twin("twin_1", 1)

    # Case 1: A plan with no twin at its candidate_index
    state_missing = State(incident_id="inc_123", plans=[p0, p1], twins=[t0])
    with pytest.raises(OrchestratorError, match=r"No twin forked for candidate_index \[1\]"):
        await apply_candidates(state_missing, deps)

    # Case 2: Matching apply
    state = State(incident_id="inc_123", plans=[p0, p1], twins=[t0, t1])
    res = await apply_candidates_node(state, deps)
    assert len(res["twins"]) == 2
    assert res["twins"][0].state == "applied"
    assert res["twins"][1].state == "applied"

    # Case 3: Twins out of list order still pair by candidate_index, not position
    state_unordered = State(incident_id="inc_123", plans=[p0, p1], twins=[t1, t0])
    res_unordered = await apply_candidates_node(state_unordered, deps)
    assert [t.twin_id for t in res_unordered["twins"]] == ["twin_0", "twin_1"]
    assert [t.candidate_index for t in res_unordered["twins"]] == [0, 1]

    # Case 4: A stale twin left over from an earlier fork is superseded, not applied to
    stale = _sample_twin("twin_stale_1", 1)
    state_extra = State(incident_id="inc_123", plans=[p0, p1], twins=[stale, t0, t1])
    res_extra = await apply_candidates_node(state_extra, deps)
    assert [t.twin_id for t in res_extra["twins"]] == ["twin_0", "twin_1"]


@pytest.mark.asyncio
async def test_observe_node() -> None:
    deps = create_fake_deps()
    p0 = _sample_plan("plan_0", 0)
    t0 = _sample_twin("twin_0", 0)

    # Case 1: Fresh observation
    state = State(incident_id="inc_123", plans=[p0], twins=[t0])
    res = await observe_node(state, deps)
    assert "evidence" in res
    assert "tournament" in res
    assert len(res["evidence"]) == 1

    # Case 2: Evidence inherited from a previous attempt is re-observed, not reused
    ev0 = CandidateEvidence(
        plan_id="plan_0",
        twin_id="twin_0",
        applied_at=datetime.now(UTC),
        probes=[],
        recovered=True,
        recovery_seconds=5.0,
        observed_blast_set=["data-service"],
        downstream_error_delta=0.0,
        invariant_violations=[],
        mirror_stats=MirrorStats(twin_id="twin_0", delivered=100, dropped=0),
        evidence_complete=True,
    )
    state_recorded = State(incident_id="inc_123", plans=[p0], twins=[t0], evidence=[ev0])
    res_recorded = await observe(state_recorded, deps)
    assert len(res_recorded["evidence"]) == 1
    assert res_recorded["evidence"][0] != ev0
    assert res_recorded["tournament"] is not None


@pytest.mark.asyncio
async def test_tournament_node() -> None:
    deps = create_fake_deps()
    p0 = _sample_plan("plan_0", 0)
    t0 = _sample_twin("twin_0", 0)

    # Case 1: Tournament already present and DECIDED
    now = datetime.now(UTC)
    tour_decided = TournamentResult(
        incident_id="inc_123",
        outcome=TournamentOutcome.DECIDED,
        scores=[CandidateScore(plan_id="plan_0", composite=0.1, components={}, disqualified=False)],
        winner_plan_id="plan_0",
        runner_up_plan_id=None,
        margin=0.5,
        decided_at=now,
    )
    state1 = State(incident_id="inc_123", tournament=tour_decided)
    res1 = await tournament_node(state1, deps)
    assert res1["tournament"] == tour_decided
    assert "outcome" not in res1

    # Case 2: Tournament not in state -> observe_and_score called
    state2 = State(incident_id="inc_123", plans=[p0], twins=[t0])
    res2 = await tournament(state2, deps)
    assert res2["tournament"].outcome == TournamentOutcome.DECIDED

    # Case 3: Tournament AMBIGUOUS -> escalates
    tour_ambig = TournamentResult(
        incident_id="inc_123",
        outcome=TournamentOutcome.AMBIGUOUS,
        scores=[],
        margin=0.02,
        decided_at=now,
    )
    state3 = State(incident_id="inc_123", tournament=tour_ambig)
    res3 = await tournament(state3, deps)
    assert res3["outcome"] == RunOutcome.ESCALATED
    assert "Tournament ambiguous" in res3["escalation_reason"]

    # Case 4: Tournament NO_VIABLE_CANDIDATE -> escalates
    tour_none = TournamentResult(
        incident_id="inc_123",
        outcome=TournamentOutcome.NO_VIABLE_CANDIDATE,
        scores=[],
        decided_at=now,
    )
    state4 = State(incident_id="inc_123", tournament=tour_none)
    res4 = await tournament(state4, deps)
    assert res4["outcome"] == RunOutcome.ESCALATED
    assert "no viable candidate" in res4["escalation_reason"]


@pytest.mark.asyncio
async def test_safety_kernel_node() -> None:
    deps = create_fake_deps()
    p0 = _sample_plan("plan_0", 0, has_inverse=True, with_resources=True)
    p_no_inv = _sample_plan(
        "plan_no_inv",
        1,
        has_inverse=False,
        action=ActionType.SCALE_WORKLOAD,
        with_resources=False,
    )
    p_no_act = _sample_plan(
        "plan_no_act", 2, has_inverse=False, action=ActionType.NO_ACTION, with_resources=False
    )

    now = datetime.now(UTC)
    tour = TournamentResult(
        incident_id="inc_123",
        outcome=TournamentOutcome.DECIDED,
        scores=[],
        winner_plan_id="plan_0",
        decided_at=now,
    )

    # Case 1: No winner plan in state
    state_no_winner = State(incident_id="inc_123")
    res_no_winner = await safety_kernel_node(state_no_winner, deps)
    assert res_no_winner["outcome"] == RunOutcome.ESCALATED
    assert "No winning plan available" in res_no_winner["escalation_reason"]

    # Case 2: Standard PASS verdict with resources and inverse
    state_pass = State(incident_id="inc_123", plans=[p0], tournament=tour)
    res_pass = await safety_kernel(state_pass, deps)
    assert res_pass["verdict"].verdict == KernelVerdictType.PASS
    assert "outcome" not in res_pass

    # Case 3: VETO verdict triggers escalation
    deps_veto = Deps(
        **{
            **deps.__dict__,
            "safety_kernel": FakeSafetyKernel(force_verdict=KernelVerdictType.VETO),
        }
    )
    res_veto = await safety_kernel(state_pass, deps_veto)
    assert res_veto["verdict"].verdict == KernelVerdictType.VETO
    assert res_veto["outcome"] == RunOutcome.ESCALATED
    assert "VETO: Invariant K3" in res_veto["escalation_reason"]

    # Case 4: Winner plan without inverse and not NO_ACTION
    tour_no_inv = tour.model_copy(update={"winner_plan_id": "plan_no_inv"})
    state_no_inv = State(incident_id="inc_123", plans=[p_no_inv], tournament=tour_no_inv)
    res_no_inv = await safety_kernel(state_no_inv, deps)
    assert res_no_inv["verdict"].verdict == KernelVerdictType.PASS

    # Case 5: Winner plan with NO_ACTION (treated as having inverse)
    tour_no_act = tour.model_copy(update={"winner_plan_id": "plan_no_act"})
    state_no_act = State(incident_id="inc_123", plans=[p_no_act], tournament=tour_no_act)
    res_no_act = await safety_kernel(state_no_act, deps)
    assert res_no_act["verdict"].verdict == KernelVerdictType.PASS


@pytest.mark.asyncio
async def test_actuate_node() -> None:
    deps = create_fake_deps()
    p0 = _sample_plan("plan_0", 0)
    now = datetime.now(UTC)

    # Invariant K10 enforcement
    # Case 1: Verdict is None -> raises ActuationError
    state_no_verdict = State(incident_id="inc_123", plans=[p0])
    with pytest.raises(ActuationError, match="Cannot actuate on production without PASS verdict"):
        await actuate_node(state_no_verdict, deps)

    # Case 2: Verdict is VETO -> raises ActuationError
    veto_verdict = KernelVerdict(
        incident_id="inc_123",
        plan_id="plan_0",
        verdict=KernelVerdictType.VETO,
        results=[],
        missing_facts=[],
        solver_ms=1.0,
        human_reason="VETO",
    )
    state_veto = State(incident_id="inc_123", plans=[p0], verdict=veto_verdict)
    with pytest.raises(ActuationError, match="Cannot actuate on production without PASS verdict"):
        await actuate(state_veto, deps)

    # Case 3: PASS verdict but winning plan not found in state -> raises OrchestratorError
    pass_verdict = KernelVerdict(
        incident_id="inc_123",
        plan_id="plan_0",
        verdict=KernelVerdictType.PASS,
        results=[],
        missing_facts=[],
        solver_ms=1.0,
        human_reason="PASS",
    )
    state_no_plan = State(
        incident_id="inc_123",
        plans=[p0],
        tournament=TournamentResult(
            incident_id="inc_123",
            outcome=TournamentOutcome.DECIDED,
            scores=[],
            winner_plan_id="plan_missing",
            decided_at=now,
        ),
        verdict=pass_verdict,
    )
    with pytest.raises(OrchestratorError, match="winning plan not found in state"):
        await actuate(state_no_plan, deps)

    # Case 4: Successful actuation
    tour_ok = TournamentResult(
        incident_id="inc_123",
        outcome=TournamentOutcome.DECIDED,
        scores=[],
        winner_plan_id="plan_0",
        decided_at=now,
    )
    state_ok = State(incident_id="inc_123", plans=[p0], tournament=tour_ok, verdict=pass_verdict)
    res_ok = await actuate(state_ok, deps)
    assert res_ok["prod_applied_plan_id"] == "plan_0"
    assert res_ok["prod_outcome"] == "resolved"
    assert res_ok["outcome"] == RunOutcome.EXECUTED

    # Case 5: Actuator returns False (not resolved)
    class FailingActuator(Actuator):
        async def apply(self, plan: RemediationPlan, namespace: str) -> bool:
            _ = (plan, namespace)
            return False

        async def apply_to_production(self, plan: RemediationPlan, verdict: KernelVerdict) -> bool:
            _ = (plan, verdict)
            return False

        async def revert(self, plan: RemediationPlan, namespace: str) -> bool:
            _ = (plan, namespace)
            return False

    deps_fail = Deps(
        **{
            **deps.__dict__,
            "actuator": FailingActuator(),
        }
    )
    res_fail = await actuate(state_ok, deps_fail)
    assert res_fail["prod_outcome"] == "not_resolved"
    assert res_fail["prod_applied_plan_id"] == "plan_0"
    # Outcome is left to escalate_pagerduty: production is still broken, so the run
    # must reach a human rather than terminate here.
    assert "outcome" not in res_fail
    assert "did not resolve incident" in res_fail["escalation_reason"]

    # Case 6: Actuator returns False with worsened outcome
    class WorseningActuator(FailingActuator):
        def __init__(self) -> None:
            self.last_prod_outcome = "worsened"

    deps_worse = Deps(
        **{
            **deps.__dict__,
            "actuator": WorseningActuator(),
        }
    )
    res_worse = await actuate(state_ok, deps_worse)
    assert res_worse["prod_outcome"] == "worsened"
    assert res_worse["prod_applied_plan_id"] == "plan_0"
    assert "outcome" not in res_worse
    assert "did not resolve incident" in res_worse["escalation_reason"]


@pytest.mark.asyncio
async def test_notify_slack_node() -> None:
    deps = create_fake_deps()
    p0 = _sample_plan("plan_0", 0)

    # Case 1: With applied plan and prod_outcome
    state1 = State(
        incident_id="inc_123",
        plans=[p0],
        prod_applied_plan_id="plan_0",
        prod_outcome="resolved",
    )
    res1 = await notify_slack_node(state1, deps)
    assert res1 == {}
    assert len(deps.notifier.slack_posts) == 1  # type: ignore[attr-defined]
    post1 = deps.notifier.slack_posts[0]  # type: ignore[attr-defined]
    assert post1["incident_id"] == "inc_123"
    assert post1["prod_outcome"] == "resolved"
    assert post1["run_id"] == "run_inc_123"

    # Case 2: Without applied plan and prod_outcome is None
    state2 = State(incident_id="inc_123")
    res2 = await notify_slack(state2, deps)
    assert res2 == {}
    assert len(deps.notifier.slack_posts) == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_escalate_pagerduty_node() -> None:
    deps = create_fake_deps()

    # Case 1: escalation_reason present in state
    state1 = State(incident_id="inc_123", escalation_reason="Custom reason")
    res1 = await escalate_pagerduty_node(state1, deps)
    assert res1["outcome"] == RunOutcome.ESCALATED
    assert res1["escalation_reason"] == "Custom reason"

    # Case 2: escalation_reason from verdict.human_reason
    verdict = KernelVerdict(
        incident_id="inc_123",
        plan_id="plan_0",
        verdict=KernelVerdictType.VETO,
        results=[],
        missing_facts=[],
        solver_ms=1.0,
        human_reason="VETO reason",
    )
    state2 = State(incident_id="inc_123", verdict=verdict)
    res2 = await escalate_pagerduty(state2, deps)
    assert res2["escalation_reason"] == "VETO reason"

    # Case 3: escalation_reason from errors
    state3 = State(incident_id="inc_123", errors=["Error A", "Error B"])
    res3 = await escalate_pagerduty(state3, deps)
    assert res3["escalation_reason"] == "Error A; Error B"

    # Case 4: default fallback reason
    state4 = State(incident_id="inc_123")
    res4 = await escalate_pagerduty(state4, deps)
    assert res4["escalation_reason"] == "Incident escalated to human operator"

    # Verify FakeNotifier recorded all 4 escalations with parameters
    assert isinstance(deps.notifier, FakeNotifier)
    assert len(deps.notifier.pagerduty_escalations) == 4
    assert deps.notifier.pagerduty_escalations[1]["verdict"] == verdict
    assert deps.notifier.pagerduty_escalations[1]["urgency"] == "high"


@pytest.mark.asyncio
async def test_teardown_fleet_node() -> None:
    deps = create_fake_deps()
    t0 = _sample_twin("twin_0", 0)
    t1 = _sample_twin("twin_1", 1)

    # Case 1: incident_id present
    state = State(incident_id="inc_123", twins=[t0, t1])
    res = await teardown_fleet_node(state, deps)
    assert len(res["twins"]) == 2
    assert res["twins"][0].state == "torn_down"
    assert res["twins"][1].state == "torn_down"

    # Case 2: incident_id empty
    state_empty_inc = State(incident_id="", twins=[t0])
    res_empty = await teardown_fleet(state_empty_inc, deps)
    assert len(res_empty["twins"]) == 1
    assert res_empty["twins"][0].state == "torn_down"


@pytest.mark.asyncio
async def test_record_run_node() -> None:
    deps = create_fake_deps()
    ctx = _sample_context("inc_123")
    p_pb = _sample_plan(
        "plan_pb",
        0,
        origin="playbook",
        playbook_id="pb_001",
    )
    p_planner = _sample_plan("plan_pl", 1, origin="planner")

    # Case 1: Plan originated from playbook and was resolved
    state_pb = State(
        incident_id="inc_123",
        context=ctx,
        plans=[p_pb, p_planner],
        prod_applied_plan_id="plan_pb",
        prod_outcome="resolved",
    )
    res_pb = await record_run_node(state_pb, deps)
    assert res_pb["outcome"] == RunOutcome.EXECUTED
    assert res_pb["finished_at"] is not None

    # Verify playbook library recorded written playbooks on resolved run
    fake_pb = deps.playbook_library
    assert isinstance(fake_pb, FakePlaybookLibrary)
    assert len(fake_pb.written_playbooks) == 1
    assert fake_pb.written_playbooks[0]["run_id"] == state_pb.to_run_record().run_id

    # Case 2: Plan originated from planner (not playbook) and was resolved
    state_planner = State(
        incident_id="inc_123",
        context=ctx,
        plans=[p_pb, p_planner],
        prod_applied_plan_id="plan_pl",
        prod_outcome="resolved",
    )
    res_pl = await record_run(state_planner, deps)
    assert res_pl["outcome"] == RunOutcome.EXECUTED
    assert len(fake_pb.written_playbooks) == 2

    # Case 3: Plan originated from playbook and was not resolved on prod
    state_failed = State(
        incident_id="inc_123",
        context=ctx,
        plans=[p_pb, p_planner],
        prod_applied_plan_id="plan_pb",
        prod_outcome="not_resolved",
    )
    res_failed = await record_run(state_failed, deps)
    assert res_failed["outcome"] == RunOutcome.EXECUTED
    assert len(fake_pb.recorded_outcomes) == 1
    assert fake_pb.recorded_outcomes[0]["success"] is False
    assert fake_pb.recorded_outcomes[0]["playbook_id"] == "pb_001"


@pytest.mark.asyncio
async def test_handle_failure_node() -> None:
    deps = create_fake_deps()
    ctx = _sample_context("inc_123")

    # Case 1: Failure with incident_id, errors, and context present
    state1 = State(
        incident_id="inc_123",
        context=ctx,
        errors=["Database connection timeout", "Fleet fork failed"],
    )
    res1 = await handle_failure_node(state1, deps)
    assert res1["outcome"] == RunOutcome.FAILED
    assert res1["finished_at"] is not None
    # Verify run recorded in store
    run_rec = await deps.run_store.get_run("run_inc_123")
    assert run_rec is not None
    assert run_rec.outcome == RunOutcome.FAILED

    # Case 2: Failure with empty incident_id, no errors, and no context
    state2 = State(incident_id="", errors=[])
    res2 = await handle_failure(state2, deps)
    assert res2["outcome"] == RunOutcome.FAILED


@pytest.mark.asyncio
async def test_handle_failure_unregisters_mirrors() -> None:
    """Mirror registrations must not outlive the twins the failure path destroys."""
    deps = create_fake_deps()
    twin = _sample_twin("twin_0", 0)
    registry = deps.mirror_registry
    assert isinstance(registry, FakeMirrorRegistry)
    await registry.register_twin(twin)

    state = State(
        incident_id="inc_123",
        context=_sample_context("inc_123"),
        twins=[twin],
        errors=["apply_candidates blew up"],
    )
    await handle_failure_node(state, deps)

    stats = await registry.get_stats(twin.twin_id)
    assert stats.delivered == 0  # unregistered: no traffic is mirrored to a dead twin


@pytest.mark.asyncio
async def test_handle_failure_after_escalation_does_not_page_twice() -> None:
    """A teardown crash after a successful escalation keeps ESCALATED and pages once."""
    deps = create_fake_deps()
    notifier = deps.notifier
    assert isinstance(notifier, FakeNotifier)

    state = State(
        incident_id="inc_123",
        context=_sample_context("inc_123"),
        outcome=RunOutcome.ESCALATED,
        escalation_reason="K3 blast radius exceeded",
        escalated=True,
        errors=["RuntimeError: teardown_fleet exploded"],
    )
    res = await handle_failure_node(state, deps)

    assert notifier.pagerduty_escalations == []
    assert res["outcome"] == RunOutcome.ESCALATED
    assert res["escalation_reason"].startswith("K3 blast radius exceeded | ")
    assert "teardown_fleet exploded" in res["escalation_reason"]

    run_rec = await deps.run_store.get_run("run_inc_123")
    assert run_rec is not None
    assert run_rec.outcome == RunOutcome.ESCALATED
    assert run_rec.escalation_reason == res["escalation_reason"]


@pytest.mark.asyncio
async def test_handle_failure_records_run_when_cleanup_raises() -> None:
    """Cleanup failures are collected into the reason, never allowed to skip the record."""
    deps = create_fake_deps()
    twin = _sample_twin("twin_0", 0)

    class ExplodingRegistry(FakeMirrorRegistry):
        async def unregister_twin(self, twin_id: str) -> None:
            raise RuntimeError(f"gateway unreachable for {twin_id}")

    class ExplodingFleet(FakeFleetController):
        async def teardown_all(self, incident_id: str) -> None:
            raise RuntimeError(f"k8s api down for {incident_id}")

    deps_broken = Deps(
        **{
            **deps.__dict__,
            "mirror_registry": ExplodingRegistry(),
            "fleet_controller": ExplodingFleet(clock=deps.clock),
        }
    )
    state = State(
        incident_id="inc_123",
        context=_sample_context("inc_123"),
        twins=[twin],
        errors=["original failure"],
    )
    res = await handle_failure_node(state, deps_broken)

    assert res["outcome"] == RunOutcome.FAILED
    assert "gateway unreachable for twin_0" in res["escalation_reason"]
    assert "k8s api down for inc_123" in res["escalation_reason"]

    run_rec = await deps_broken.run_store.get_run("run_inc_123")
    assert run_rec is not None
    assert run_rec.outcome == RunOutcome.FAILED
