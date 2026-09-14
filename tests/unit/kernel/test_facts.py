"""Unit tests for safety kernel fact extraction and serialization."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from understudy.common.clock import FrozenClock
from understudy.common.config import ClusterSettings, Settings
from understudy.common.errors import MissingFact
from understudy.contracts.enums import ActionType, KernelVerdictType, RunOutcome
from understudy.contracts.evidence import CandidateEvidence, ProbeSample
from understudy.contracts.incident import (
    Alert,
    DependencyEdge,
    DependencyGraphSnapshot,
    DeployRef,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.kernel import Fact
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.contracts.run import RunRecord
from understudy.contracts.twin import MirrorStats
from understudy.fleet.fakes import FakeWorkloadReader
from understudy.fleet.models import (
    ClusterWorkloadSnapshot,
    ContainerSnapshot,
    ResourceSpec,
    WorkloadSnapshot,
)
from understudy.graph.fakes import FakeDependencyGraph
from understudy.kernel.api import SafetyKernel
from understudy.kernel.dsl import KernelContext
from understudy.kernel.facts import (
    FactExtractor,
    K8sFactSource,
    WorkloadReaderFactAdapter,
    extract_facts,
    facts_from_dict,
    facts_to_dict,
    load_facts_json,
    load_slo_config,
    save_facts_json,
)
from understudy.kernel.fakes import FakeK8sFactSource, FakeSafetyKernel
from understudy.signals.fakes import FakeDeployHistory
from understudy.store.fakes import FakeRunStore


def _create_sample_plan(
    action: ActionType = ActionType.SCALE_WORKLOAD,
    target_service: str = "data-service",
    target_commit: str | None = None,
    with_inverse: bool = True,
) -> RemediationPlan:
    """Create a sample RemediationPlan."""
    inverse_plan = None
    if with_inverse and action != ActionType.NO_ACTION:
        inverse_plan = RemediationPlan(
            plan_id="plan_inv_1",
            candidate_index=1,
            action=action,
            params=ActionParams(workload=target_service, replica_delta=-1),
            target_resources=[
                ResourceRef(
                    kind="Deployment",
                    name=target_service,
                    namespace="ust-prod",
                )
            ],
            declared_blast_set=[target_service],
            rationale="Inverse plan",
            origin="planner",
        )

    return RemediationPlan(
        plan_id="plan_test_1",
        candidate_index=0,
        action=action,
        params=ActionParams(
            workload=target_service,
            replica_delta=1,
            target_commit=target_commit,
        ),
        target_resources=[
            ResourceRef(
                kind="Deployment",
                name=target_service,
                namespace="ust-prod",
            )
        ],
        declared_blast_set=[target_service, "auth-service"],
        inverse=inverse_plan,
        rationale="Scale data-service",
        origin="planner",
    )


def _create_sample_evidence(
    plan_id: str = "plan_test_1",
    applied_at: datetime | None = None,
    probes_count: int = 60,
    drop_ratio_dropped: int = 2,
    drop_ratio_delivered: int = 198,
) -> CandidateEvidence:
    """Create sample rehearsal CandidateEvidence."""
    now = applied_at or datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    probes = [
        ProbeSample(
            at=now + timedelta(seconds=i),
            healthy=True,
            p99_latency_ms=120.0,
            error_rate=0.001,
        )
        for i in range(probes_count)
    ]
    return CandidateEvidence(
        plan_id=plan_id,
        twin_id="twin_0",
        applied_at=now,
        probes=probes,
        recovered=True,
        recovery_seconds=12.5,
        observed_blast_set=["data-service"],
        downstream_error_delta=0.0,
        mirror_stats=MirrorStats(
            twin_id="twin_0",
            delivered=drop_ratio_delivered,
            dropped=drop_ratio_dropped,
        ),
        evidence_complete=True,
    )


# --- Tests for Protocols and Adapters ---


class UnimplementedK8sSource:
    """Dummy class failing to implement K8sFactSource."""

    pass


@pytest.mark.asyncio
async def test_k8s_fact_source_protocol() -> None:
    """Verify runtime checkability and default NotImplementedError."""
    fake_source = FakeK8sFactSource()
    assert isinstance(fake_source, K8sFactSource)
    assert not isinstance(UnimplementedK8sSource(), K8sFactSource)

    # Calling protocol method directly invokes the base NotImplementedError
    with pytest.raises(NotImplementedError):
        await K8sFactSource.get_workload_replicas(fake_source, "ns", "svc")
    with pytest.raises(NotImplementedError):
        await K8sFactSource.check_egress_policy_present(fake_source, "ns")


@pytest.mark.asyncio
async def test_safety_kernel_protocol_unimplemented() -> None:
    """Test NotImplementedError on SafetyKernel base protocol."""
    fake_kernel = FakeSafetyKernel()
    with pytest.raises(NotImplementedError):
        await SafetyKernel.verify(fake_kernel, _create_sample_plan(), [])


@pytest.mark.asyncio
async def test_fake_k8s_fact_source_queries() -> None:
    """Test queries against FakeK8sFactSource."""
    source = FakeK8sFactSource(
        replicas={"data-service": 3},
        healthy_replicas={"data-service": 2},
        egress_policy_present=True,
    )
    res = await source.get_workload_replicas("ust-prod", "data-service")
    assert res == (3, 2)

    missing = await source.get_workload_replicas("ust-prod", "non-existent")
    assert missing is None

    assert await source.check_egress_policy_present("ust-prod") is True


@pytest.mark.asyncio
async def test_workload_reader_fact_adapter() -> None:
    """Test WorkloadReaderFactAdapter with snapshot and with reader."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    snap = ClusterWorkloadSnapshot(
        namespace="ust-prod",
        workloads={
            "data-service": WorkloadSnapshot(
                name="data-service",
                namespace="ust-prod",
                replicas=3,
                containers=[
                    ContainerSnapshot(
                        name="data-service",
                        image_tag="good",
                        image_digest="sha256:abc",
                        pinned_image="localhost:5001/data-service@sha256:abc",
                        resources=ResourceSpec(),
                    )
                ],
            )
        },
        captured_at=now,
    )

    # 1. Adapter with direct snapshot
    adapter1 = WorkloadReaderFactAdapter(snapshot=snap, egress_policy_present=True)
    res1 = await adapter1.get_workload_replicas("ust-prod", "data-service")
    assert res1 == (3, 3)
    assert await adapter1.get_workload_replicas("ust-prod", "missing") is None
    assert await adapter1.check_egress_policy_present("ust-prod") is True

    # 2. Adapter with WorkloadReader
    reader = FakeWorkloadReader()
    adapter2 = WorkloadReaderFactAdapter(workload_reader=reader)
    res2 = await adapter2.get_workload_replicas("ust-prod", "data-service")
    assert res2 == (1, 1)

    # 3. Adapter with neither snapshot nor reader
    adapter3 = WorkloadReaderFactAdapter()
    assert await adapter3.get_workload_replicas("ust-prod", "data-service") is None


# --- Tests for SLO Config Loader ---


def test_load_slo_config(tmp_path: Path) -> None:
    """Test loading slo.yaml configurations."""
    # Existing valid config
    valid_file = tmp_path / "slo.yaml"
    valid_file.write_text("mutation_budget: 5\nmin_replicas:\n  data-service: 2\n")
    cfg = load_slo_config(valid_file)
    assert cfg["mutation_budget"] == 5
    assert cfg["min_replicas"]["data-service"] == 2

    # Non-existent file
    assert load_slo_config(tmp_path / "missing.yaml") == {}

    # Invalid non-dict file
    invalid_file = tmp_path / "invalid.yaml"
    invalid_file.write_text("- item1\n- item2\n")
    assert load_slo_config(invalid_file) == {}

    # Corrupted YAML syntax
    corrupt_file = tmp_path / "corrupt.yaml"
    corrupt_file.write_text("mutation_budget: [unclosed")
    assert load_slo_config(corrupt_file) == {}


# --- Tests for FactExtractor Subsystems ---


@pytest.mark.asyncio
async def test_extract_k8s_facts() -> None:
    """Test K8s fact extraction for replicas, healthy counts, and egress policy."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    plan = _create_sample_plan()

    # None k8s source
    extractor_none = FactExtractor()
    assert await extractor_none.extract_k8s_facts(plan, now) == []

    # Active k8s source with target resources
    k8s_source = FakeK8sFactSource(
        replicas={"data-service": 3, "auth-service": 2},
        healthy_replicas={"data-service": 2, "auth-service": 2},
        egress_policy_present=True,
    )
    extractor = FactExtractor(k8s_source=k8s_source)
    facts = await extractor.extract_k8s_facts(plan, now)

    fact_map = {f.name: f.value for f in facts}
    assert fact_map["twin_egress_policy_present"] is True
    assert fact_map["replicas[data-service]"] == 3
    assert fact_map["healthy_replicas[data-service]"] == 2
    assert fact_map["replicas"] == {"data-service": 3}
    assert fact_map["healthy_replicas"] == {"data-service": 2}

    # All facts stamped with observed_at
    for f in facts:
        assert f.observed_at == now
        assert f.source == "k8s"

    # Plan with empty workload, non-deployment target, and missing service in k8s
    plan_non_deploy = RemediationPlan(
        plan_id="plan_cfg",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload=""),
        target_resources=[ResourceRef(kind="ConfigMap", name="app-config", namespace="ust-prod")],
        declared_blast_set=[],
        rationale="Revert config",
        origin="planner",
    )
    facts_empty_svc = await extractor.extract_k8s_facts(plan_non_deploy, now)
    assert len(facts_empty_svc) == 1
    assert facts_empty_svc[0].name == "twin_egress_policy_present"

    # Plan with workload that does NOT exist in k8s_source
    plan_missing = _create_sample_plan(target_service="unknown-svc")
    facts_missing = await extractor.extract_k8s_facts(plan_missing, now)
    missing_map = {f.name: f.value for f in facts_missing}
    assert "replicas" not in missing_map
    assert "healthy_replicas" not in missing_map


def test_extract_config_facts() -> None:
    """Test configuration facts extraction across config shapes."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    plan = _create_sample_plan()

    custom_settings = Settings(cluster=ClusterSettings(prod_namespace="custom-prod"))
    slo_cfg = {
        "mutation_budget": 4,
        "min_replicas": {
            "data-service": 2,
            "auth-service": 1,
            "invalid-count": "not-an-int",
        },
    }
    extractor = FactExtractor(settings=custom_settings, slo_config=slo_cfg)
    facts = extractor.extract_config_facts(plan, now)

    fact_map = {f.name: f.value for f in facts}
    assert fact_map["authorized_namespace"] == "custom-prod"
    assert fact_map["mutation_budget"] == 4
    assert fact_map["min_replicas[data-service]"] == 2
    assert fact_map["min_replicas[auth-service]"] == 1
    assert "min_replicas[invalid-count]" not in fact_map
    assert fact_map["min_replicas"] == {"data-service": 2, "auth-service": 1}

    # Config with min_replicas not being a Mapping
    extractor_non_map = FactExtractor(slo_config={"min_replicas": "invalid"})
    facts_non_map = extractor_non_map.extract_config_facts(plan, now)
    map_non = {f.name: f.value for f in facts_non_map}
    assert "min_replicas" not in map_non

    # Config with min_replicas being empty mapping
    extractor_empty_map = FactExtractor(slo_config={"min_replicas": {}})
    facts_empty_map = extractor_empty_map.extract_config_facts(plan, now)
    map_empty = {f.name: f.value for f in facts_empty_map}
    assert "min_replicas" not in map_empty

    # Default settings fallback
    extractor_default = FactExtractor()
    facts_def = extractor_default.extract_config_facts(plan, now)
    fact_map_def = {f.name: f.value for f in facts_def}
    assert fact_map_def["authorized_namespace"] == "ust-prod"
    assert fact_map_def["mutation_budget"] == 3


def test_extract_plan_facts() -> None:
    """Test plan fact extraction across plan variants."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)

    # Standard plan with inverse
    plan = _create_sample_plan(with_inverse=True)
    extractor = FactExtractor()
    facts = extractor.extract_plan_facts(plan, now)

    fact_map = {f.name: f.value for f in facts}
    assert fact_map["plan_target_namespaces"] == {"ust-prod"}
    assert len(fact_map["plan_targets"]) == 1
    assert fact_map["declared_blast_set"] == {"data-service", "auth-service"}
    assert fact_map["plan_has_inverse"] is True
    assert len(fact_map["inverse_targets"]) == 1
    for f in facts:
        assert f.source == "plan"
        assert f.observed_at == now

    # Plan without inverse
    plan_no_inv = _create_sample_plan(with_inverse=False)
    facts_no_inv = extractor.extract_plan_facts(plan_no_inv, now)
    fact_map_ni = {f.name: f.value for f in facts_no_inv}
    assert fact_map_ni["plan_has_inverse"] is False
    assert fact_map_ni["inverse_targets"] == set()

    # NO_ACTION plan (has_inverse is True, inverse_targets empty)
    no_action_plan = RemediationPlan(
        plan_id="plan_no_action",
        candidate_index=2,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload=""),
        target_resources=[],
        declared_blast_set=[],
        rationale="Do nothing",
        origin="planner",
    )
    facts_no_action = extractor.extract_plan_facts(no_action_plan, now)
    fact_map_na = {f.name: f.value for f in facts_no_action}
    assert fact_map_na["plan_has_inverse"] is True
    assert fact_map_na["inverse_targets"] == set()
    assert fact_map_na["plan_target_namespaces"] == {"ust-prod"}


@pytest.mark.asyncio
async def test_extract_github_facts() -> None:
    """Test GitHub fact extraction for migration boundary K3."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    t_mig = now - timedelta(hours=2)
    t_target = now - timedelta(hours=1)

    deploys = [
        DeployRef(
            commit_sha="c0ffee_latest",
            image_digests={"data-service": "sha256:d1"},
            deployed_at=now,
            contains_migration=False,
        ),
        DeployRef(
            commit_sha="c0ffee_target",
            image_digests={"data-service": "sha256:d0"},
            deployed_at=t_target,
            contains_migration=False,
        ),
        DeployRef(
            commit_sha="c0ffee_migration",
            image_digests={"data-service": "sha256:dm"},
            deployed_at=t_mig,
            contains_migration=True,
        ),
    ]

    deploy_history = FakeDeployHistory(deploys=deploys)
    extractor = FactExtractor(deploy_history=deploy_history)

    # 1. Rollback plan targeting c0ffee_target
    plan_rollback = _create_sample_plan(
        action=ActionType.ROLLBACK_DEPLOY,
        target_commit="c0ffee_target",
    )
    facts = await extractor.extract_github_facts(plan_rollback, now)
    fact_map = {f.name: f.value for f in facts}

    assert fact_map["last_migration_commit_time"] == t_mig
    assert fact_map["rollback_target_commit_time"] == t_target
    assert fact_map["target_contains_migration"] is False

    # 1b. Rollback targeting prefix or longer SHA
    plan_prefix = _create_sample_plan(
        action=ActionType.ROLLBACK_DEPLOY,
        target_commit="c0ffee_target_longer_sha",
    )
    facts_pref = await extractor.extract_github_facts(
        plan_prefix,
        now,
        recent_deploys=[
            DeployRef(
                commit_sha="c0ffee_target",
                image_digests={},
                deployed_at=t_target,
                contains_migration=True,
            )
        ],
    )
    fact_map_pref = {f.name: f.value for f in facts_pref}
    assert fact_map_pref["target_contains_migration"] is True

    # 2. Rollback targeting commit that does NOT exist
    plan_unknown = _create_sample_plan(
        action=ActionType.ROLLBACK_DEPLOY,
        target_commit="nonexistent_sha",
    )
    facts_unknown = await extractor.extract_github_facts(plan_unknown, now)
    fact_map_un = {f.name: f.value for f in facts_unknown}
    assert "last_migration_commit_time" in fact_map_un
    assert "rollback_target_commit_time" not in fact_map_un
    assert "target_contains_migration" not in fact_map_un

    # 3. No migrations present in history (last_migration_commit_time omitted)
    no_mig_deploys = [
        DeployRef(
            commit_sha="c1",
            image_digests={},
            deployed_at=now,
            contains_migration=False,
        )
    ]
    facts_no_mig = await extractor.extract_github_facts(
        plan_rollback, now, recent_deploys=no_mig_deploys
    )
    fact_map_nm = {f.name: f.value for f in facts_no_mig}
    assert "last_migration_commit_time" not in fact_map_nm

    # 4. Rollback without target_commit set
    plan_no_sha = _create_sample_plan(
        action=ActionType.ROLLBACK_DEPLOY,
        target_commit=None,
    )
    facts_no_sha = await extractor.extract_github_facts(plan_no_sha, now, recent_deploys=deploys)
    fact_map_ns = {f.name: f.value for f in facts_no_sha}
    assert "rollback_target_commit_time" not in fact_map_ns

    # 5. No deploy history available
    extractor_empty = FactExtractor()
    assert await extractor_empty.extract_github_facts(plan_rollback, now) == []


def test_extract_graph_facts() -> None:
    """Test dependency graph fact extraction."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    plan = _create_sample_plan(target_service="data-service")

    # 1. From live DependencyGraph
    dep_graph = FakeDependencyGraph()
    extractor1 = FactExtractor(dependency_graph=dep_graph)
    facts1 = extractor1.extract_graph_facts(plan, now)
    fact_map1 = {f.name: f.value for f in facts1}
    assert fact_map1["dependents[data-service]"] == {"edge-gateway", "auth-service", "worker"}
    assert "data-service" in fact_map1["dependents"]

    # 2. From DependencyGraphSnapshot
    snap = DependencyGraphSnapshot(
        nodes=["data-service", "auth-service"],
        edges=[DependencyEdge(source="auth-service", target="data-service")],
        observed_at=now,
    )
    extractor2 = FactExtractor()
    facts2 = extractor2.extract_graph_facts(plan, now, graph_snapshot=snap)
    fact_map2 = {f.name: f.value for f in facts2}
    assert fact_map2["dependents[data-service]"] == {"auth-service"}

    # 3. Snapshot with no services queried or empty dependents
    plan_no_workload = RemediationPlan(
        plan_id="p_empty",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload=""),
        target_resources=[],
        declared_blast_set=[],
        rationale="",
        origin="planner",
    )
    facts_empty = extractor2.extract_graph_facts(plan_no_workload, now, graph_snapshot=snap)
    assert facts_empty == []

    # 3b. Plan with non-Deployment target resource (e.g. ConfigMap)
    plan_cm = RemediationPlan(
        plan_id="p_cm",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload=""),
        target_resources=[ResourceRef(kind="ConfigMap", name="app-cfg", namespace="ust-prod")],
        declared_blast_set=[],
        rationale="",
        origin="planner",
    )
    assert extractor2.extract_graph_facts(plan_cm, now) == []

    # 4. Neither graph nor snapshot
    facts4 = extractor2.extract_graph_facts(plan, now)
    assert facts4 == []


@pytest.mark.asyncio
async def test_extract_store_facts() -> None:
    """Test store fact extraction for in-flight plan targets and mutation budget window."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    plan = _create_sample_plan()

    # None store
    extractor_none = FactExtractor()
    assert await extractor_none.extract_store_facts(plan, now) == []

    store = FakeRunStore()

    # 1. Add an active in-flight run targeting auth-service
    active_plan = RemediationPlan(
        plan_id="plan_active",
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload="auth-service"),
        target_resources=[
            ResourceRef(
                kind="Deployment",
                name="auth-service",
                namespace="ust-prod",
            )
        ],
        declared_blast_set=["auth-service"],
        rationale="Active run",
        origin="planner",
    )
    active_run = RunRecord(
        run_id="run_active_1",
        incident_id="inc_active_1",
        started_at=now - timedelta(minutes=5),
        finished_at=None,
        outcome=RunOutcome.EXECUTED,
        context=IncidentContext(
            incident_id="inc_active_1",
            alert=Alert(
                alert_id="a1",
                source="synthetic",
                title="Active alert",
                service="auth-service",
                severity="critical",
                fired_at=now,
            ),
            metrics_window=MetricWindow(
                service="auth-service",
                start_time=now,
                end_time=now,
            ),
            dependency_graph=DependencyGraphSnapshot(observed_at=now),
            gathered_at=now,
        ),
        plans=[active_plan],
    )
    await store.record_run(active_run)

    # 2. Add a finished run within the 15-minute window that actuated production
    recent_applied_run = RunRecord(
        run_id="run_past_1",
        incident_id="inc_past_1",
        started_at=now - timedelta(minutes=10),
        finished_at=now - timedelta(minutes=8),
        outcome=RunOutcome.EXECUTED,
        context=active_run.context,
        prod_applied_plan_id="plan_past_1",
    )
    await store.record_run(recent_applied_run)

    # 3. Add an older finished run outside the 15-minute window
    old_applied_run = RunRecord(
        run_id="run_past_old",
        incident_id="inc_past_old",
        started_at=now - timedelta(minutes=30),
        finished_at=now - timedelta(minutes=25),
        outcome=RunOutcome.EXECUTED,
        context=active_run.context,
        prod_applied_plan_id="plan_old_1",
    )
    await store.record_run(old_applied_run)

    extractor = FactExtractor(
        run_store=store,
        slo_config={"mutation_window_minutes": 15},
    )
    facts = await extractor.extract_store_facts(plan, now)
    fact_map = {f.name: f.value for f in facts}

    assert (
        ResourceRef(kind="Deployment", name="auth-service", namespace="ust-prod")
        in fact_map["in_flight_plan_targets"]
    )
    # Only recent_applied_run is within 15 min window with prod_applied_plan_id
    assert fact_map["prod_mutations_in_window"] == 1


def test_extract_evidence_facts() -> None:
    """Test tournament evidence fact extraction."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    plan = _create_sample_plan()

    extractor = FactExtractor()

    # None evidence
    assert extractor.extract_evidence_facts(plan, None, now) == []

    # Valid evidence
    applied_time = now - timedelta(seconds=45)
    evidence = _create_sample_evidence(
        applied_at=applied_time,
        probes_count=75,
        drop_ratio_delivered=95,
        drop_ratio_dropped=5,
    )
    facts = extractor.extract_evidence_facts(plan, evidence, now)
    fact_map = {f.name: f.value for f in facts}

    assert fact_map["observed_blast_set"] == {"data-service"}
    assert fact_map["evidence_age_seconds"] == 45.0
    assert fact_map["max_drop_ratio"] == 0.05
    assert fact_map["probe_sample_count"] == 75

    # Evidence with future applied_at (clamped to 0.0)
    future_evidence = _create_sample_evidence(applied_at=now + timedelta(seconds=10))
    facts_fut = extractor.extract_evidence_facts(plan, future_evidence, now)
    fut_map = {f.name: f.value for f in facts_fut}
    assert fut_map["evidence_age_seconds"] == 0.0


# --- Tests for Combined Extraction & Convenience Function ---


@pytest.mark.asyncio
async def test_extract_all_and_convenience_function() -> None:
    """Test complete fact extraction via extract_all and extract_facts helper."""
    frozen_now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    clock = FrozenClock(initial_time=frozen_now)
    plan = _create_sample_plan()
    evidence = _create_sample_evidence(applied_at=frozen_now - timedelta(seconds=30))

    k8s = FakeK8sFactSource()
    deploys = FakeDeployHistory()
    store = FakeRunStore()
    graph = FakeDependencyGraph()

    # 1. extract_facts helper
    facts = await extract_facts(
        plan=plan,
        evidence=evidence,
        k8s_source=k8s,
        deploy_history=deploys,
        run_store=store,
        dependency_graph=graph,
        clock=clock,
        slo_config={"mutation_budget": 3, "min_replicas": {"data-service": 1}},
    )

    fact_map = {f.name: f.value for f in facts}
    assert fact_map["authorized_namespace"] == "ust-prod"
    assert fact_map["mutation_budget"] == 3
    assert fact_map["min_replicas[data-service]"] == 1
    assert fact_map["replicas[data-service]"] == 2
    assert fact_map["plan_has_inverse"] is True
    assert fact_map["probe_sample_count"] == 60
    assert fact_map["evidence_age_seconds"] == 30.0

    # 2. Test clock property
    extractor = FactExtractor(clock=clock)
    assert extractor.clock == clock

    # 3. extract_all with IncidentContext
    ctx = IncidentContext(
        incident_id="inc_full",
        alert=Alert(
            alert_id="alt1",
            source="synthetic",
            title="Alert",
            service="data-service",
            severity="critical",
            fired_at=frozen_now,
        ),
        metrics_window=MetricWindow(
            service="data-service",
            start_time=frozen_now,
            end_time=frozen_now,
        ),
        recent_deploys=await deploys.recent_deploys(limit=5),
        dependency_graph=graph.snapshot(),
        gathered_at=frozen_now,
    )
    facts_with_ctx = await extractor.extract_all(plan, evidence=evidence, context=ctx)
    assert len(facts_with_ctx) > 0


# --- Tests for Serialization Roundtrip ---


def test_fact_serialization_roundtrip(tmp_path: Path) -> None:
    """Test facts_to_dict, facts_from_dict, save_facts_json, and load_facts_json."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    res_ref1 = ResourceRef(kind="Deployment", name="data-service", namespace="ust-prod")
    res_ref2 = ResourceRef(kind="Deployment", name="auth-service", namespace="ust-prod")

    original_facts = [
        Fact(name="authorized_namespace", value="ust-prod", source="config", observed_at=now),
        Fact(name="mutation_budget", value=3, source="config", observed_at=now),
        Fact(name="last_migration_commit_time", value=now, source="github", observed_at=now),
        Fact(
            name="plan_target_namespaces",
            value={"ust-prod"},
            source="plan",
            observed_at=now,
        ),
        Fact(name="plan_targets", value={res_ref1, res_ref2}, source="plan", observed_at=now),
        Fact(
            name="dependents",
            value={"data-service": {"edge-gateway", "auth-service"}},
            source="graph",
            observed_at=now,
        ),
        Fact(
            name="observed_blast_set",
            value=["data-service"],
            source="tournament",
            observed_at=now,
        ),
    ]

    # Serialization to dict
    as_dicts = facts_to_dict(original_facts)
    assert isinstance(as_dicts, list)
    assert len(as_dicts) == len(original_facts)

    # Deserialization from dict
    restored = facts_from_dict(as_dicts)
    assert len(restored) == len(original_facts)

    restored_map = {f.name: f.value for f in restored}
    assert restored_map["authorized_namespace"] == "ust-prod"
    assert restored_map["mutation_budget"] == 3
    assert restored_map["last_migration_commit_time"] == now
    assert restored_map["plan_target_namespaces"] == {"ust-prod"}
    assert restored_map["plan_targets"] == {res_ref1, res_ref2}
    assert restored_map["dependents"] == {"data-service": {"edge-gateway", "auth-service"}}

    # Deserialization when item is already a ResourceRef
    direct_dict = [
        {
            "name": "in_flight_plan_targets",
            "value": [res_ref1],
            "source": "store",
            "observed_at": now.isoformat(),
        }
    ]
    restored_direct = facts_from_dict(direct_dict)
    assert restored_direct[0].value == {res_ref1}

    # Deserialization when item is neither dict nor ResourceRef (ignored)
    mixed_dict = [
        {
            "name": "in_flight_plan_targets",
            "value": [res_ref1, "not_a_resource_ref"],
            "source": "store",
            "observed_at": now.isoformat(),
        }
    ]
    restored_mixed = facts_from_dict(mixed_dict)
    assert restored_mixed[0].value == {res_ref1}

    # Deserialization when dependents has a non-list set value
    dep_dict = [
        {
            "name": "dependents",
            "value": {"data-service": {"auth-service"}},
            "source": "graph",
            "observed_at": now.isoformat(),
        }
    ]
    restored_dep = facts_from_dict(dep_dict)
    assert restored_dep[0].value == {"data-service": {"auth-service"}}

    # File save and load
    file_path = tmp_path / "facts.json"
    save_facts_json(original_facts, file_path)
    loaded_facts = load_facts_json(file_path)
    assert len(loaded_facts) == len(original_facts)

    # Error handling for invalid inputs
    bad_string_input: Any = "invalid string"
    with pytest.raises(TypeError, match="Expected sequence of fact dictionaries"):
        facts_from_dict(cast("list[dict[str, Any]]", bad_string_input))

    bad_item_input: Any = ["not a dict"]
    with pytest.raises(TypeError, match="Expected mapping for fact item"):
        facts_from_dict(cast("list[dict[str, Any]]", bad_item_input))


def test_fixture_facts_ok_loading_and_kernel_context() -> None:
    """Test loading fixtures/facts_ok.json and verifying KernelContext consumes it."""
    fixture_path = Path("fixtures/facts_ok.json")
    assert fixture_path.is_file()

    facts = load_facts_json(fixture_path)
    assert len(facts) > 0

    plan = _create_sample_plan(action=ActionType.ROLLBACK_DEPLOY, target_commit="deadbeef")
    ctx = KernelContext(plan, facts)

    # Verify core facts are resolved with typed accessors
    assert ctx.get_str("authorized_namespace") == "ust-prod"
    assert ctx.get_int("mutation_budget") == 3
    assert ctx.get_int("replicas[data-service]") == 2
    assert ctx.get_int("min_replicas[data-service]") == 1
    assert ctx.get_datetime("last_migration_commit_time") == datetime(
        2026, 9, 13, 10, 0, 0, tzinfo=UTC
    )
    assert ctx.get_bool("twin_egress_policy_present") is True
    assert ctx.get_set("plan_target_namespaces") == {"ust-prod"}
    assert ctx.get_float("max_drop_ratio") == 0.01


def test_fixture_facts_missing_migration_omits_migration_fact() -> None:
    """Test that fixtures/facts_missing_migration.json omits last_migration_commit_time."""
    fixture_path = Path("fixtures/facts_missing_migration.json")
    assert fixture_path.is_file()

    facts = load_facts_json(fixture_path)
    plan = _create_sample_plan(action=ActionType.ROLLBACK_DEPLOY, target_commit="deadbeef")
    ctx = KernelContext(plan, facts)

    assert not ctx.has_fact("last_migration_commit_time")
    with pytest.raises(MissingFact):
        ctx.get_datetime("last_migration_commit_time")
    assert "last_migration_commit_time" in ctx.missing_facts


@pytest.mark.asyncio
async def test_fake_safety_kernel_all_verdicts() -> None:
    """Test FakeSafetyKernel generating PASS, VETO, and UNCERTAIN verdicts."""
    plan = _create_sample_plan()

    # 1. PASS verdict
    k_pass = FakeSafetyKernel(force_verdict=KernelVerdictType.PASS)
    v_pass = await k_pass.verify(plan, [])
    assert v_pass.verdict == KernelVerdictType.PASS
    assert len(v_pass.results) == 2
    assert v_pass.missing_facts == []

    # 2. VETO verdict
    k_veto = FakeSafetyKernel(force_verdict=KernelVerdictType.VETO)
    v_veto = await k_veto.verify(plan, [])
    assert v_veto.verdict == KernelVerdictType.VETO
    assert len(v_veto.results) == 1
    assert v_veto.results[0].unsat_core is not None

    # 3. UNCERTAIN verdict
    k_unc = FakeSafetyKernel(force_verdict=KernelVerdictType.UNCERTAIN)
    v_unc = await k_unc.verify(plan, [])
    assert v_unc.verdict == KernelVerdictType.UNCERTAIN
    assert "replicas[data-service]" in v_unc.missing_facts
