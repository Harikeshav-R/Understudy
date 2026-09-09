"""Round-trip serialization and deserialization tests for all contract models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from understudy.contracts import (
    ActionParams,
    ActionType,
    Alert,
    CandidateEvidence,
    CandidateScore,
    DependencyEdge,
    DependencyGraphSnapshot,
    DeployRef,
    ErrorSignature,
    Fact,
    FailureClass,
    IncidentContext,
    InvariantResult,
    InvariantTier,
    KernelVerdict,
    KernelVerdictType,
    MetricPoint,
    MetricSeries,
    MetricWindow,
    MirrorStats,
    ProbeSample,
    RemediationPlan,
    ResourceRef,
    RunOutcome,
    RunRecord,
    TournamentOutcome,
    TournamentResult,
    TwinHandle,
)


def test_action_params_roundtrip() -> None:
    model = ActionParams(
        workload="data-service",
        target_commit="abc1234",
        replica_delta=2,
        flag_name="beta_feature",
        config_key="TIMEOUT_MS",
        config_value="5000",
    )
    serialized = model.model_dump_json()
    deserialized = ActionParams.model_validate_json(serialized)
    assert model == deserialized


def test_resource_ref_roundtrip() -> None:
    model = ResourceRef(namespace="ust-prod", kind="Deployment", name="data-service")
    serialized = model.model_dump_json()
    deserialized = ResourceRef.model_validate_json(serialized)
    assert model == deserialized


def test_remediation_plan_roundtrip() -> None:
    inverse_plan = RemediationPlan(
        plan_id="plan_inverse_001",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="data-service"),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="No-op reversal",
        origin="planner",
    )
    model = RemediationPlan(
        plan_id="plan_001",
        candidate_index=1,
        action=ActionType.SCALE_WORKLOAD,
        params=ActionParams(workload="data-service", replica_delta=1),
        target_resources=[
            ResourceRef(namespace="ust-twin-0", kind="Deployment", name="data-service")
        ],
        declared_blast_set=["data-service"],
        inverse=inverse_plan,
        rationale="Scale up to handle queue pressure",
        origin="planner",
        playbook_id="pb_123",
    )
    serialized = model.model_dump_json()
    deserialized = RemediationPlan.model_validate_json(serialized)
    assert model == deserialized
    assert deserialized.inverse is not None
    assert deserialized.inverse.plan_id == "plan_inverse_001"


def test_metric_models_roundtrip() -> None:
    now = datetime.now(UTC)
    point = MetricPoint(timestamp=now, value=42.5)
    point_deser = MetricPoint.model_validate_json(point.model_dump_json())
    assert point == point_deser

    series = MetricSeries(
        metric_name="http_requests_total",
        labels={"service": "edge-gateway", "code": "200"},
        points=[point],
    )
    series_deser = MetricSeries.model_validate_json(series.model_dump_json())
    assert series == series_deser

    window = MetricWindow(
        service="edge-gateway",
        start_time=now,
        end_time=now,
        series=[series],
        p99_latency_ms=120.5,
        error_rate=0.01,
        request_count=1500,
    )
    window_deser = MetricWindow.model_validate_json(window.model_dump_json())
    assert window == window_deser


def test_dependency_graph_models_roundtrip() -> None:
    now = datetime.now(UTC)
    edge = DependencyEdge(source="edge-gateway", target="auth-service")
    edge_deser = DependencyEdge.model_validate_json(edge.model_dump_json())
    assert edge == edge_deser

    snapshot = DependencyGraphSnapshot(
        nodes=["edge-gateway", "auth-service", "data-service"],
        edges=[edge, DependencyEdge(source="auth-service", target="data-service")],
        observed_at=now,
    )
    snapshot_deser = DependencyGraphSnapshot.model_validate_json(snapshot.model_dump_json())
    assert snapshot == snapshot_deser


def test_alert_and_signatures_roundtrip() -> None:
    now = datetime.now(UTC)
    alert = Alert(
        alert_id="alt_123",
        source="pagerduty",
        title="High latency on edge-gateway",
        service="edge-gateway",
        severity="critical",
        fired_at=now,
        raw={"details": "p99 > 400ms"},
    )
    alert_deser = Alert.model_validate_json(alert.model_dump_json())
    assert alert == alert_deser

    signature = ErrorSignature(
        fingerprint="fp_abcd",
        message="Connection pool exhausted",
        service="data-service",
        count=45,
        first_seen=now,
        last_seen=now,
    )
    sig_deser = ErrorSignature.model_validate_json(signature.model_dump_json())
    assert signature == sig_deser

    deploy = DeployRef(
        commit_sha="c0ffee1",
        image_digests={"data-service": "sha256:1234"},
        deployed_at=now,
        pr_number=42,
        contains_migration=True,
    )
    deploy_deser = DeployRef.model_validate_json(deploy.model_dump_json())
    assert deploy == deploy_deser


def test_incident_context_roundtrip() -> None:
    now = datetime.now(UTC)
    context = IncidentContext(
        incident_id="inc_001",
        alert=Alert(
            alert_id="alt_001",
            source="synthetic",
            title="Synthetic fault",
            service="data-service",
            severity="error",
            fired_at=now,
            raw={},
        ),
        signatures=[
            ErrorSignature(
                fingerprint="fp_1",
                message="Error",
                service="data-service",
                count=1,
                first_seen=now,
                last_seen=now,
            )
        ],
        metrics_window=MetricWindow(service="data-service", start_time=now, end_time=now),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(observed_at=now),
        inferred_failure_class=FailureClass.RESOURCE_EXHAUSTION,
        gathered_at=now,
    )
    context_deser = IncidentContext.model_validate_json(context.model_dump_json())
    assert context == context_deser


def test_twin_and_mirror_roundtrip() -> None:
    now = datetime.now(UTC)
    twin = TwinHandle(
        twin_id="twin_001",
        incident_id="inc_001",
        candidate_index=0,
        namespace="ust-twin-inc_001-0",
        database="twin_001_db",
        forked_from_snapshot_at=now,
        ready_at=now,
        state="ready",
    )
    twin_deser = TwinHandle.model_validate_json(twin.model_dump_json())
    assert twin == twin_deser

    stats_zero = MirrorStats(twin_id="twin_001", delivered=0, dropped=0)
    assert stats_zero.drop_ratio == 0.0

    stats = MirrorStats(twin_id="twin_001", delivered=90, dropped=10)
    assert stats.drop_ratio == pytest.approx(0.1)
    stats_deser = MirrorStats.model_validate_json(stats.model_dump_json())
    assert stats == stats_deser


def test_evidence_and_tournament_roundtrip() -> None:
    now = datetime.now(UTC)
    probe = ProbeSample(at=now, healthy=True, p99_latency_ms=85.2, error_rate=0.0)
    probe_deser = ProbeSample.model_validate_json(probe.model_dump_json())
    assert probe == probe_deser

    evidence = CandidateEvidence(
        plan_id="plan_001",
        twin_id="twin_001",
        applied_at=now,
        probes=[probe],
        recovered=True,
        recovery_seconds=14.2,
        observed_blast_set=["data-service"],
        downstream_error_delta=0.0,
        invariant_violations=[],
        mirror_stats=MirrorStats(twin_id="twin_001", delivered=100, dropped=0),
        evidence_complete=True,
    )
    evidence_deser = CandidateEvidence.model_validate_json(evidence.model_dump_json())
    assert evidence == evidence_deser

    score = CandidateScore(
        plan_id="plan_001",
        composite=0.15,
        components={"recovery": 0.1, "blast": 0.05},
        disqualified=False,
    )
    score_deser = CandidateScore.model_validate_json(score.model_dump_json())
    assert score == score_deser

    result = TournamentResult(
        incident_id="inc_001",
        outcome=TournamentOutcome.DECIDED,
        scores=[score],
        llm_ranking=["plan_001"],
        llm_agreement=True,
        winner_plan_id="plan_001",
        runner_up_plan_id=None,
        margin=0.25,
        decided_at=now,
    )
    result_deser = TournamentResult.model_validate_json(result.model_dump_json())
    assert result == result_deser


def test_kernel_roundtrip() -> None:
    now = datetime.now(UTC)
    fact = Fact(
        name="replicas[data-service]",
        value=3,
        source="k8s",
        observed_at=now,
    )
    fact_deser = Fact.model_validate_json(fact.model_dump_json())
    assert fact == fact_deser

    inv_res = InvariantResult(
        invariant_id="K1",
        tier=InvariantTier.PROOF,
        satisfied=True,
        unsat_core=None,
        reason="Discharged unsat by Z3",
    )
    inv_deser = InvariantResult.model_validate_json(inv_res.model_dump_json())
    assert inv_res == inv_deser

    verdict = KernelVerdict(
        incident_id="inc_001",
        plan_id="plan_001",
        verdict=KernelVerdictType.PASS,
        results=[inv_res],
        missing_facts=[],
        solver_ms=25.4,
        human_reason="All PROOF invariants discharged",
    )
    verdict_deser = KernelVerdict.model_validate_json(verdict.model_dump_json())
    assert verdict == verdict_deser


def test_run_record_roundtrip() -> None:
    now = datetime.now(UTC)
    context = IncidentContext(
        incident_id="inc_001",
        alert=Alert(
            alert_id="alt_001",
            source="synthetic",
            title="Synthetic fault",
            service="data-service",
            severity="error",
            fired_at=now,
            raw={},
        ),
        signatures=[],
        metrics_window=MetricWindow(service="data-service", start_time=now, end_time=now),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(observed_at=now),
        inferred_failure_class=None,
        gathered_at=now,
    )
    record = RunRecord(
        run_id="run_001",
        incident_id="inc_001",
        scenario_id="sc_01",
        started_at=now,
        finished_at=now,
        outcome=RunOutcome.EXECUTED,
        context=context,
        plans=[],
        evidence=[],
        tournament=None,
        verdict=None,
        prod_applied_plan_id="plan_001",
        prod_outcome="resolved",
        escalation_reason=None,
    )
    record_deser = RunRecord.model_validate_json(record.model_dump_json())
    assert record == record_deser


def test_frozen_immutability() -> None:
    ref = ResourceRef(namespace="ust-prod", kind="Deployment", name="data-service")
    with pytest.raises(ValidationError):
        # Trying to mutate a frozen model
        ref.name = "mutated"
