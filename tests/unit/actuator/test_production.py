"""Unit tests for production actuation engine (actuator/production.py).

Enforces 100% line and branch test coverage.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from understudy.actuator.apply import K8sPlanApplier
from understudy.actuator.production import (
    DEFAULT_TARGET_SERVICE,
    ProductionActuator,
    apply_to_production,
    assert_caller_authorised,
    assert_k10_authorisation,
    build_pre_actuation_run_record,
    determine_prod_outcome,
)
from understudy.common.clock import FrozenClock
from understudy.common.config import Settings
from understudy.common.errors import ActuationError
from understudy.contracts.enums import ActionType, InvariantTier, KernelVerdictType, RunOutcome
from understudy.contracts.evidence import ProbeSample
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.kernel import InvariantResult, KernelVerdict
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.store.fakes import FakeRunStore
from understudy.tournament.api import EnvironmentProbe, ProbeResult

FIXED_NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


def _make_plan(
    plan_id: str = "plan_001",
    workload: str = "data-service",
    action: ActionType = ActionType.ROLLBACK_DEPLOY,
) -> RemediationPlan:
    return RemediationPlan(
        plan_id=plan_id,
        candidate_index=0,
        action=action,
        params=ActionParams(workload=workload, target_commit="abc1234"),
        target_resources=[ResourceRef(namespace="ust-prod", kind="Deployment", name=workload)],
        declared_blast_set=[workload],
        rationale="Roll back faulty deploy",
        origin="planner",
    )


def _make_verdict(
    plan_id: str = "plan_001",
    incident_id: str = "inc_001",
    verdict: KernelVerdictType = KernelVerdictType.PASS,
    evaluated_at: datetime | None = FIXED_NOW,
    human_reason: str = "PASS",
) -> KernelVerdict:
    return KernelVerdict(
        incident_id=incident_id,
        plan_id=plan_id,
        verdict=verdict,
        results=[
            InvariantResult(
                invariant_id="K1",
                tier=InvariantTier.PROOF,
                satisfied=True,
                reason="Safe",
            )
        ],
        missing_facts=[],
        solver_ms=10.0,
        human_reason=human_reason,
        evaluated_at=evaluated_at,
    )


def _make_context(incident_id: str = "inc_001", service: str = "data-service") -> IncidentContext:
    now = FIXED_NOW
    return IncidentContext(
        incident_id=incident_id,
        alert=Alert(
            alert_id=f"alt_{incident_id}",
            source="pagerduty",
            title=f"Incident {incident_id}",
            service=service,
            severity="critical",
            fired_at=now,
        ),
        metrics_window=MetricWindow(
            service=service,
            start_time=now - timedelta(minutes=5),
            end_time=now,
            p99_latency_ms=450.0,
            error_rate=0.08,
            request_count=1000,
        ),
        dependency_graph=DependencyGraphSnapshot(observed_at=now),
        gathered_at=now,
    )


# ---------------------------------------------------------------------------
# Unit tests for caller authorisation guard (AGENTS.md §2.4)
# ---------------------------------------------------------------------------


def test_assert_caller_authorised_from_test_runner() -> None:
    """Calling from test runner passes the authorisation guard."""
    assert_caller_authorised()


def test_assert_caller_authorised_from_actuate_node() -> None:
    """Mocking an actuate caller frame passes authorisation guard."""
    fake_frame = MagicMock()
    fake_frame_info = MagicMock()
    fake_frame_info.frame = fake_frame
    fake_frame_info.function = "actuate"
    fake_frame_info.filename = "/path/to/actuate.py"

    fake_module = MagicMock()
    fake_module.__name__ = "understudy.orchestrator.nodes.actuate"

    with (
        patch("inspect.stack", return_value=[MagicMock(), MagicMock(), fake_frame_info]),
        patch("inspect.getmodule", return_value=fake_module),
    ):
        assert_caller_authorised()


def test_assert_caller_authorised_unauthorised_raises() -> None:
    """Calling from unauthorised modules (e.g. shadow or fleet) raises ActuationError."""
    fake_frame = MagicMock()
    fake_frame_info = MagicMock()
    fake_frame_info.frame = fake_frame
    fake_frame_info.function = "speculative_run"
    fake_frame_info.filename = "/path/to/shadow/loop.py"

    fake_module = MagicMock()
    fake_module.__name__ = "understudy.shadow.loop"

    with (
        patch("inspect.stack", return_value=[MagicMock(), MagicMock(), fake_frame_info]),
        patch("inspect.getmodule", return_value=fake_module),
        pytest.raises(ActuationError) as exc,
    ):
        assert_caller_authorised()
    assert "AGENTS.md §2.4 violation" in str(exc.value)


# ---------------------------------------------------------------------------
# Unit tests for assert_k10_authorisation
# ---------------------------------------------------------------------------


def test_assert_k10_authorisation_success() -> None:
    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1", evaluated_at=FIXED_NOW)
    assert_k10_authorisation(plan, verdict, now=FIXED_NOW)


def test_assert_k10_authorisation_none_timestamp_success() -> None:
    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1", evaluated_at=None)
    assert_k10_authorisation(plan, verdict, now=FIXED_NOW)


def test_assert_k10_authorisation_veto_raises() -> None:
    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1", verdict=KernelVerdictType.VETO)
    with pytest.raises(ActuationError, match="without PASS verdict"):
        assert_k10_authorisation(plan, verdict, now=FIXED_NOW)


def test_assert_k10_authorisation_plan_mismatch_raises() -> None:
    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_2", evaluated_at=FIXED_NOW)
    with pytest.raises(ActuationError, match="plan_id mismatch"):
        assert_k10_authorisation(plan, verdict, now=FIXED_NOW)


def test_assert_k10_authorisation_stale_raises() -> None:
    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1", evaluated_at=FIXED_NOW - timedelta(seconds=65))
    with pytest.raises(ActuationError, match="stale"):
        assert_k10_authorisation(plan, verdict, now=FIXED_NOW)


def test_assert_k10_authorisation_future_skew_raises() -> None:
    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1", evaluated_at=FIXED_NOW + timedelta(seconds=10))
    with pytest.raises(ActuationError, match="in the future"):
        assert_k10_authorisation(plan, verdict, now=FIXED_NOW)


# ---------------------------------------------------------------------------
# Unit tests for build_pre_actuation_run_record
# ---------------------------------------------------------------------------


def test_build_pre_actuation_run_record_with_context() -> None:
    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1")
    ctx = _make_context()
    record = build_pre_actuation_run_record(plan, verdict, now=FIXED_NOW, context=ctx)
    assert record.run_id == "run_inc_001_pre_actuation"
    assert record.incident_id == "inc_001"
    assert record.prod_applied_plan_id == "plan_1"
    assert record.context == ctx
    assert record.finished_at is None
    assert record.outcome == RunOutcome.EXECUTED


def test_build_pre_actuation_run_record_synthetic_context() -> None:
    plan = _make_plan("plan_1", workload="")
    verdict = _make_verdict("plan_1", incident_id="inc_synthetic")
    record = build_pre_actuation_run_record(plan, verdict, now=FIXED_NOW, context=None)
    assert record.run_id == "run_inc_synthetic_pre_actuation"
    assert record.context.incident_id == "inc_synthetic"
    assert record.context.alert.service == DEFAULT_TARGET_SERVICE


# ---------------------------------------------------------------------------
# Unit tests for determine_prod_outcome
# ---------------------------------------------------------------------------


def test_determine_prod_outcome_not_applied() -> None:
    outcome = determine_prod_outcome(probe_result=None, applied_successfully=False)
    assert outcome == "not_resolved"


def test_determine_prod_outcome_none_probe_result() -> None:
    outcome = determine_prod_outcome(probe_result=None, applied_successfully=True)
    assert outcome == "not_resolved"


def test_determine_prod_outcome_recovered() -> None:
    res = ProbeResult(
        namespace="ust-prod",
        target_service="data-service",
        probes=[],
        recovered=True,
        recovery_seconds=15.0,
    )
    assert determine_prod_outcome(res) == "resolved"


def test_determine_prod_outcome_not_resolved_without_baseline() -> None:
    res = ProbeResult(
        namespace="ust-prod",
        target_service="data-service",
        probes=[],
        recovered=False,
    )
    assert determine_prod_outcome(res) == "not_resolved"


def test_determine_prod_outcome_worsened_by_error_rate() -> None:
    baseline = ProbeSample(
        at=FIXED_NOW,
        healthy=False,
        p99_latency_ms=100.0,
        error_rate=0.01,
    )
    post_probes = [
        ProbeSample(at=FIXED_NOW, healthy=False, p99_latency_ms=100.0, error_rate=0.05),
        ProbeSample(at=FIXED_NOW, healthy=False, p99_latency_ms=100.0, error_rate=0.06),
    ]
    res = ProbeResult(
        namespace="ust-prod",
        target_service="data-service",
        probes=post_probes,
        recovered=False,
    )
    assert determine_prod_outcome(res, baseline_sample=baseline) == "worsened"


def test_determine_prod_outcome_worsened_by_latency() -> None:
    baseline = ProbeSample(
        at=FIXED_NOW,
        healthy=False,
        p99_latency_ms=100.0,
        error_rate=0.01,
    )
    post_probes = [
        ProbeSample(at=FIXED_NOW, healthy=False, p99_latency_ms=250.0, error_rate=0.01),
        ProbeSample(at=FIXED_NOW, healthy=False, p99_latency_ms=300.0, error_rate=0.01),
    ]
    res = ProbeResult(
        namespace="ust-prod",
        target_service="data-service",
        probes=post_probes,
        recovered=False,
    )
    assert determine_prod_outcome(res, baseline_sample=baseline) == "worsened"


def test_determine_prod_outcome_not_resolved_stable_unrecovered() -> None:
    baseline = ProbeSample(
        at=FIXED_NOW,
        healthy=False,
        p99_latency_ms=200.0,
        error_rate=0.05,
    )
    post_probes = [
        ProbeSample(at=FIXED_NOW, healthy=False, p99_latency_ms=200.0, error_rate=0.05),
        ProbeSample(at=FIXED_NOW, healthy=False, p99_latency_ms=200.0, error_rate=0.05),
    ]
    res = ProbeResult(
        namespace="ust-prod",
        target_service="data-service",
        probes=post_probes,
        recovered=False,
    )
    assert determine_prod_outcome(res, baseline_sample=baseline) == "not_resolved"


def test_determine_prod_outcome_empty_probe_metrics() -> None:
    baseline = ProbeSample(
        at=FIXED_NOW,
        healthy=False,
        p99_latency_ms=0.0,
        error_rate=0.0,
    )
    res = ProbeResult(
        namespace="ust-prod",
        target_service="data-service",
        probes=[],
        recovered=False,
    )
    assert determine_prod_outcome(res, baseline_sample=baseline) == "not_resolved"


# ---------------------------------------------------------------------------
# Unit tests for ProductionActuator class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_production_actuator_apply_and_revert() -> None:
    mock_applier = AsyncMock(spec=K8sPlanApplier)
    mock_applier.apply.return_value = True
    mock_applier.revert.return_value = True

    actuator = ProductionActuator(
        applier=mock_applier,
        enforce_caller_guard=False,
    )

    plan = _make_plan("plan_1")
    assert await actuator.apply(plan, "ust-twin-1") is True
    mock_applier.apply.assert_called_with(plan=plan, namespace="ust-twin-1")

    assert await actuator.revert(plan, "ust-twin-1") is True
    mock_applier.revert.assert_called_with(plan=plan, namespace="ust-twin-1")


@pytest.mark.asyncio
async def test_production_actuator_kill_switch_disabled() -> None:
    settings = Settings(actuation_enabled=False)
    clock = FrozenClock(initial_time=FIXED_NOW)
    mock_applier = AsyncMock(spec=K8sPlanApplier)

    actuator = ProductionActuator(
        applier=mock_applier,
        clock=clock,
        settings=settings,
        enforce_caller_guard=False,
    )

    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1")

    with pytest.raises(ActuationError) as exc_info:
        await actuator.apply_to_production(plan, verdict)
    assert "kill switch" in str(exc_info.value)
    assert mock_applier.apply.call_count == 0


@pytest.mark.asyncio
async def test_production_actuator_pre_record_and_probe_resolved() -> None:
    clock = FrozenClock(initial_time=FIXED_NOW)
    run_store = FakeRunStore()

    mock_applier = AsyncMock(spec=K8sPlanApplier)
    mock_applier.apply.return_value = True

    mock_probe = AsyncMock(spec=EnvironmentProbe)
    baseline_sample = ProbeSample(
        at=FIXED_NOW,
        healthy=False,
        p99_latency_ms=250.0,
        error_rate=0.05,
    )
    mock_probe.sample_once.return_value = baseline_sample
    mock_probe.probe_environment.return_value = ProbeResult(
        namespace="ust-prod",
        target_service="data-service",
        probes=[ProbeSample(at=FIXED_NOW, healthy=True, p99_latency_ms=80.0, error_rate=0.0)],
        recovered=True,
        recovery_seconds=10.0,
    )

    actuator = ProductionActuator(
        applier=mock_applier,
        probe=mock_probe,
        run_store=run_store,
        clock=clock,
        enforce_caller_guard=False,
    )

    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1")
    ctx = _make_context()

    success = await actuator.apply_to_production(plan, verdict, context=ctx)
    assert success is True
    assert actuator.last_prod_outcome == "resolved"

    # Verify pre-actuation record was persisted
    pre_run = await run_store.get_run(f"run_{verdict.incident_id}_pre_actuation")
    assert pre_run is not None
    assert pre_run.prod_applied_plan_id == "plan_1"
    assert pre_run.context == ctx


@pytest.mark.asyncio
async def test_production_actuator_baseline_probe_failure_continues() -> None:
    clock = FrozenClock(initial_time=FIXED_NOW)
    mock_applier = AsyncMock(spec=K8sPlanApplier)
    mock_applier.apply.return_value = True

    mock_probe = AsyncMock(spec=EnvironmentProbe)
    mock_probe.sample_once.side_effect = RuntimeError("Prometheus scrape timeout")
    mock_probe.probe_environment.return_value = ProbeResult(
        namespace="ust-prod",
        target_service="data-service",
        probes=[],
        recovered=True,
    )

    actuator = ProductionActuator(
        applier=mock_applier,
        probe=mock_probe,
        clock=clock,
        enforce_caller_guard=False,
    )

    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1")

    success = await actuator.apply_to_production(plan, verdict)
    assert success is True
    assert actuator.last_prod_outcome == "resolved"


@pytest.mark.asyncio
async def test_production_actuator_applier_returns_false() -> None:
    clock = FrozenClock(initial_time=FIXED_NOW)
    mock_applier = AsyncMock(spec=K8sPlanApplier)
    mock_applier.apply.return_value = False

    mock_probe = AsyncMock(spec=EnvironmentProbe)

    actuator = ProductionActuator(
        applier=mock_applier,
        probe=mock_probe,
        clock=clock,
        enforce_caller_guard=False,
    )

    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1")

    success = await actuator.apply_to_production(plan, verdict)
    assert success is False
    assert actuator.last_prod_outcome == "not_resolved"
    assert mock_probe.probe_environment.call_count == 0


@pytest.mark.asyncio
async def test_apply_to_production_standalone_helper() -> None:
    clock = FrozenClock(initial_time=FIXED_NOW)
    mock_applier = AsyncMock(spec=K8sPlanApplier)
    mock_applier.apply.return_value = True

    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1")

    success = await apply_to_production(
        plan=plan,
        verdict=verdict,
        applier=mock_applier,
        clock=clock,
        enforce_caller_guard=False,
    )
    assert success is True


@pytest.mark.asyncio
async def test_apply_to_production_caller_guard_enabled_success() -> None:
    """Caller guard enabled succeeds when invoked from a test runner."""
    clock = FrozenClock(initial_time=FIXED_NOW)
    mock_applier = AsyncMock(spec=K8sPlanApplier)
    mock_applier.apply.return_value = True

    actuator = ProductionActuator(
        applier=mock_applier,
        clock=clock,
        enforce_caller_guard=True,
    )
    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1")
    assert await actuator.apply_to_production(plan, verdict) is True


@pytest.mark.asyncio
async def test_apply_to_production_caller_guard_enabled_unauthorised() -> None:
    """Caller guard enabled raises ActuationError when invoked from unauthorised frame."""
    clock = FrozenClock(initial_time=FIXED_NOW)
    mock_applier = AsyncMock(spec=K8sPlanApplier)

    actuator = ProductionActuator(
        applier=mock_applier,
        clock=clock,
        enforce_caller_guard=True,
    )
    plan = _make_plan("plan_1")
    verdict = _make_verdict("plan_1")

    fake_frame = MagicMock()
    fake_frame_info = MagicMock()
    fake_frame_info.frame = fake_frame
    fake_frame_info.function = "illegal_actuate"
    fake_frame_info.filename = "/path/to/shadow/runner.py"

    fake_module = MagicMock()
    fake_module.__name__ = "understudy.shadow.runner"

    with (
        patch("inspect.stack", return_value=[MagicMock(), MagicMock(), fake_frame_info]),
        patch("inspect.getmodule", return_value=fake_module),
        pytest.raises(ActuationError, match=r"AGENTS\.md §2\.4 violation"),
    ):
        await actuator.apply_to_production(plan, verdict)
