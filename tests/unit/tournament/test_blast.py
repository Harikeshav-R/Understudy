"""Exhaustive unit tests for blast radius computation and pre-apply baselines."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from understudy.common.clock import FrozenClock
from understudy.common.errors import ObservabilityError
from understudy.contracts.enums import ActionType
from understudy.contracts.incident import MetricWindow
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.contracts.twin import TwinHandle
from understudy.graph.service_graph import ServiceDependencyGraph
from understudy.signals.fakes import FakeObservabilityAdapter
from understudy.tournament.api import BlastTracker
from understudy.tournament.blast import (
    BlastConfig,
    BlastCoordinator,
    compute_affected_services,
    compute_blast_radius,
    compute_downstream_error_delta,
    evaluate_blast,
)
from understudy.tournament.fakes import FakeBlastTracker


def test_blast_config_defaults_and_from_settings() -> None:
    """Verify BlastConfig defaults and loader from system settings."""
    cfg = BlastConfig()
    assert cfg.degradation_threshold == 0.10
    assert cfg.baseline_window_seconds == 30.0
    assert cfg.zero_baseline_latency_floor_ms == 1.0
    assert cfg.zero_baseline_error_rate_floor == 0.01
    assert cfg.downstream_error_ceiling == 0.10

    loaded = BlastConfig.from_settings()
    assert loaded.degradation_threshold == 0.10
    assert loaded.downstream_error_ceiling == 0.10


def test_compute_affected_services_relative_degradation() -> None:
    """Validate degradation detection logic (>10% increase in p99 or error rate)."""
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    baselines = {
        "svc-a": MetricWindow(
            service="svc-a",
            start_time=now - timedelta(seconds=30),
            end_time=now,
            p99_latency_ms=100.0,
            error_rate=0.01,
        ),
        "svc-b": MetricWindow(
            service="svc-b",
            start_time=now - timedelta(seconds=30),
            end_time=now,
            p99_latency_ms=50.0,
            error_rate=0.02,
        ),
        "svc-c": MetricWindow(
            service="svc-c",
            start_time=now - timedelta(seconds=30),
            end_time=now,
            p99_latency_ms=20.0,
            error_rate=0.01,
        ),
    }

    # svc-a: p99 increased 100 -> 115 (+15% > 10%) -> affected
    # svc-b: error_rate increased 0.02 -> 0.03 (+50% > 10%) -> affected
    # svc-c: p99 20 -> 21 (+5% <= 10%), error_rate 0.01 -> 0.01 -> not affected
    # svc-d: missing baseline -> skipped
    post_windows = {
        "svc-a": MetricWindow(
            service="svc-a",
            start_time=now,
            end_time=now + timedelta(seconds=30),
            p99_latency_ms=115.0,
            error_rate=0.01,
        ),
        "svc-b": MetricWindow(
            service="svc-b",
            start_time=now,
            end_time=now + timedelta(seconds=30),
            p99_latency_ms=50.0,
            error_rate=0.03,
        ),
        "svc-c": MetricWindow(
            service="svc-c",
            start_time=now,
            end_time=now + timedelta(seconds=30),
            p99_latency_ms=21.0,
            error_rate=0.01,
        ),
        "svc-d": MetricWindow(
            service="svc-d",
            start_time=now,
            end_time=now + timedelta(seconds=30),
            p99_latency_ms=50.0,
            error_rate=0.05,
        ),
    }

    affected = compute_affected_services(baselines, post_windows, threshold=0.10)
    assert affected == {"svc-a", "svc-b"}


def test_compute_affected_services_zero_baselines() -> None:
    """Verify handling when baseline p99 or error rates are zero or None."""
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    baselines = {
        "svc-lat-spike": MetricWindow(
            service="svc-lat-spike",
            start_time=now,
            end_time=now,
            p99_latency_ms=0.0,
            error_rate=0.0,
        ),
        "svc-lat-ok": MetricWindow(
            service="svc-lat-ok",
            start_time=now,
            end_time=now,
            p99_latency_ms=0.0,
            error_rate=0.0,
        ),
        "svc-err-spike": MetricWindow(
            service="svc-err-spike",
            start_time=now,
            end_time=now,
            p99_latency_ms=None,
            error_rate=0.0,
        ),
        "svc-err-ok": MetricWindow(
            service="svc-err-ok",
            start_time=now,
            end_time=now,
            p99_latency_ms=None,
            error_rate=None,
        ),
    }
    post_windows = {
        # > 1.0ms floor -> affected
        "svc-lat-spike": MetricWindow(
            service="svc-lat-spike",
            start_time=now,
            end_time=now,
            p99_latency_ms=5.0,
            error_rate=0.0,
        ),
        # <= 1.0ms floor -> not affected
        "svc-lat-ok": MetricWindow(
            service="svc-lat-ok",
            start_time=now,
            end_time=now,
            p99_latency_ms=0.5,
            error_rate=0.0,
        ),
        # > 0.01 floor -> affected
        "svc-err-spike": MetricWindow(
            service="svc-err-spike",
            start_time=now,
            end_time=now,
            p99_latency_ms=0.0,
            error_rate=0.03,
        ),
        # <= 0.01 floor -> not affected
        "svc-err-ok": MetricWindow(
            service="svc-err-ok",
            start_time=now,
            end_time=now,
            p99_latency_ms=0.0,
            error_rate=0.005,
        ),
    }

    affected = compute_affected_services(baselines, post_windows)
    assert affected == {"svc-lat-spike", "svc-err-spike"}


def test_compute_blast_radius() -> None:
    """Validate blast radius score computation over reachable graph dependents."""
    graph = ServiceDependencyGraph(
        nodes=["edge-gateway", "auth-service", "data-service", "worker"],
        edges=[
            ("edge-gateway", "auth-service"),
            ("edge-gateway", "data-service"),
            ("auth-service", "data-service"),
        ],
        request_shares={
            "edge-gateway": 0.40,
            "auth-service": 0.30,
            "data-service": 0.20,
            "worker": 0.10,
        },
    )

    # Empty target or "none" yields 0.0
    assert compute_blast_radius("", {"auth-service"}, graph) == 0.0
    assert compute_blast_radius("none", {"auth-service"}, graph) == 0.0

    # No affected services yields 0.0
    assert compute_blast_radius("data-service", set(), graph) == 0.0

    # Target with no dependents (edge-gateway has no callers) yields 0.0
    assert compute_blast_radius("edge-gateway", {"auth-service", "data-service"}, graph) == 0.0

    # Target data-service callers: auth-service (0.30) + edge-gateway (0.40)
    # Only auth affected
    assert compute_blast_radius("data-service", {"auth-service"}, graph) == pytest.approx(0.30)

    # Both auth and edge affected: 0.30 + 0.40 = 0.70
    assert compute_blast_radius(
        "data-service", {"auth-service", "edge-gateway"}, graph
    ) == pytest.approx(0.70)

    # Unrelated worker affected (not a dependent of data-service): only auth and edge count
    assert compute_blast_radius(
        "data-service", {"auth-service", "edge-gateway", "worker"}, graph
    ) == pytest.approx(0.70)


def test_compute_blast_radius_clamping() -> None:
    """Blast radius is clamped to [0.0, 1.0] even if shares exceed 1.0."""
    graph = ServiceDependencyGraph(
        nodes=["target", "caller1", "caller2"],
        edges=[("caller1", "target"), ("caller2", "target")],
        request_shares={"caller1": 0.8, "caller2": 0.7},
    )
    assert compute_blast_radius("target", {"caller1", "caller2"}, graph) == 1.0


def test_compute_downstream_error_delta_weighted_and_aggregations() -> None:
    """Validate downstream error delta calculation across reachable services."""
    graph = ServiceDependencyGraph(
        nodes=["edge-gateway", "auth-service", "data-service"],
        edges=[
            ("edge-gateway", "auth-service"),
            ("auth-service", "data-service"),
            ("edge-gateway", "data-service"),
        ],
        request_shares={
            "edge-gateway": 0.40,
            "auth-service": 0.20,
            "data-service": 0.40,
        },
    )
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

    # Edge cases
    assert compute_downstream_error_delta("", {}, {}, graph) == 0.0
    assert compute_downstream_error_delta("none", {}, {}, graph) == 0.0
    assert compute_downstream_error_delta("edge-gateway", {}, {}, graph) == 0.0  # no dependents

    baselines = {
        "edge-gateway": MetricWindow(
            service="edge-gateway",
            start_time=now,
            end_time=now,
            p99_latency_ms=10.0,
            error_rate=0.01,
        ),
        "auth-service": MetricWindow(
            service="auth-service",
            start_time=now,
            end_time=now,
            p99_latency_ms=20.0,
            error_rate=0.02,
        ),
    }

    # Reachable from data-service: edge-gateway (0.40 share) and auth-service (0.20 share)
    # Total reachable share: 0.40 + 0.20 = 0.60
    # edge-gateway delta: 0.05 - 0.01 = +0.04
    # auth-service delta: 0.08 - 0.02 = +0.06
    # Weighted delta: (0.40 * 0.04 + 0.20 * 0.06) / 0.60
    # = (0.016 + 0.012) / 0.60 = 0.028 / 0.60 = 0.0467
    post_windows = {
        "edge-gateway": MetricWindow(
            service="edge-gateway",
            start_time=now,
            end_time=now,
            p99_latency_ms=10.0,
            error_rate=0.05,
        ),
        "auth-service": MetricWindow(
            service="auth-service",
            start_time=now,
            end_time=now,
            p99_latency_ms=20.0,
            error_rate=0.08,
        ),
    }

    weighted = compute_downstream_error_delta("data-service", baselines, post_windows, graph)
    assert weighted == pytest.approx(0.0467, abs=1e-4)


def test_compute_downstream_error_delta_zero_shares_and_missing_windows() -> None:
    """Verify downstream error delta when shares are zero or windows are missing."""
    graph = ServiceDependencyGraph(
        nodes=["target", "caller"],
        edges=[("caller", "target")],
        request_shares={"caller": 0.0, "target": 0.0},
    )
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    baselines: dict[str, MetricWindow] = {}
    post_windows = {
        "caller": MetricWindow(
            service="caller",
            start_time=now,
            end_time=now,
            p99_latency_ms=10.0,
            error_rate=0.05,
        )
    }
    # Total weight <= 0 yields 0.0
    assert compute_downstream_error_delta("target", baselines, post_windows, graph) == 0.0


def test_compute_downstream_error_delta_negative_improvement() -> None:
    """Downstream error rate improvement yields a negative delta prior to scoring clamping."""
    graph = ServiceDependencyGraph(
        nodes=["target", "caller"],
        edges=[("caller", "target")],
        request_shares={"caller": 1.0, "target": 0.0},
    )
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    baselines = {
        "caller": MetricWindow(
            service="caller",
            start_time=now,
            end_time=now,
            p99_latency_ms=10.0,
            error_rate=0.08,
        )
    }
    post_windows = {
        "caller": MetricWindow(
            service="caller",
            start_time=now,
            end_time=now,
            p99_latency_ms=10.0,
            error_rate=0.02,
        )
    }
    delta = compute_downstream_error_delta("target", baselines, post_windows, graph)
    assert delta == pytest.approx(-0.06)


def test_evaluate_blast_full_and_no_action() -> None:
    """Verify evaluate_blast on active remediation plan and NO_ACTION candidate."""
    graph = ServiceDependencyGraph(
        nodes=["edge-gateway", "data-service"],
        edges=[("edge-gateway", "data-service")],
        request_shares={"edge-gateway": 0.60, "data-service": 0.40},
    )
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    baselines = {
        "edge-gateway": MetricWindow(
            service="edge-gateway",
            start_time=now,
            end_time=now,
            p99_latency_ms=50.0,
            error_rate=0.0,
        ),
        "data-service": MetricWindow(
            service="data-service",
            start_time=now,
            end_time=now,
            p99_latency_ms=100.0,
            error_rate=0.0,
        ),
    }
    post_windows = {
        # edge-gateway p99 degraded 50 -> 80 (+60% > 10%)
        "edge-gateway": MetricWindow(
            service="edge-gateway",
            start_time=now,
            end_time=now,
            p99_latency_ms=80.0,
            error_rate=0.05,
        ),
        "data-service": MetricWindow(
            service="data-service",
            start_time=now,
            end_time=now,
            p99_latency_ms=100.0,
            error_rate=0.0,
        ),
    }

    # Case 1: Active remediation plan
    active_plan = RemediationPlan(
        plan_id="plan_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service"),
        target_resources=[ResourceRef(namespace="test", kind="Deployment", name="data-service")],
        origin="planner",
        rationale="Rollback data-service",
    )
    eval_active = evaluate_blast(active_plan, baselines, post_windows, graph)
    assert eval_active.target_service == "data-service"
    assert eval_active.reachable_set == ["edge-gateway"]
    assert eval_active.affected_services == ["edge-gateway"]
    assert eval_active.observed_blast_set == ["edge-gateway"]
    assert eval_active.blast_radius == pytest.approx(0.60)
    assert eval_active.downstream_error_delta == pytest.approx(0.05)

    # Case 2: NO_ACTION candidate
    no_action_plan = RemediationPlan(
        plan_id="plan_no_act",
        candidate_index=1,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="data-service"),
        target_resources=[],
        origin="planner",
        rationale="Maintain state",
    )
    eval_no_act = evaluate_blast(no_action_plan, baselines, post_windows, graph)
    assert eval_no_act.blast_radius == 0.0
    assert eval_no_act.downstream_error_delta == 0.0
    assert eval_no_act.reachable_set == []
    assert eval_no_act.observed_blast_set == []

    # Case 3: Target service passed directly as string
    eval_str = evaluate_blast("data-service", baselines, post_windows, graph)
    assert eval_str.blast_radius == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_blast_coordinator_capture_baseline_and_fallback() -> None:
    """Verify BlastCoordinator captures baselines and falls back gracefully on error."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    graph = ServiceDependencyGraph(
        nodes=["edge-gateway", "data-service"],
        edges=[("edge-gateway", "data-service")],
    )

    # Mock adapter where data-service fails
    adapter = Mock()

    async def _mock_metric_window(
        service: str, since: datetime, namespace: str, until: datetime | None = None
    ) -> MetricWindow:
        _ = (namespace, until)
        if service == "data-service":
            raise ObservabilityError("Scrape timeout")
        return MetricWindow(
            service=service,
            start_time=since,
            end_time=since + timedelta(seconds=30),
            p99_latency_ms=100.0,
            error_rate=0.01,
        )

    adapter.metric_window = AsyncMock(side_effect=_mock_metric_window)

    coordinator = BlastCoordinator(
        observability=adapter,
        dependency_graph=graph,
        clock=clock,
    )

    baseline = await coordinator.capture_baseline(namespace="ust-twin-0")
    assert baseline.namespace == "ust-twin-0"
    assert "edge-gateway" in baseline.baselines
    assert baseline.baselines["edge-gateway"].p99_latency_ms == 100.0

    # Fallback recorded for data-service
    assert "data-service" in baseline.baselines
    assert baseline.baselines["data-service"].p99_latency_ms == 0.0
    assert baseline.baselines["data-service"].error_rate == 0.0


@pytest.mark.asyncio
async def test_blast_coordinator_declared_services_fallback() -> None:
    """Verify fallback declared services when snapshot raises an exception."""
    broken_graph = Mock()
    broken_graph.snapshot.side_effect = RuntimeError("Broken graph")
    adapter = FakeObservabilityAdapter()
    coordinator = BlastCoordinator(
        observability=adapter,
        dependency_graph=broken_graph,
    )
    with pytest.raises(RuntimeError, match="Broken graph"):
        coordinator._get_declared_services()

    empty_graph = Mock()
    empty_snapshot = Mock()
    empty_snapshot.nodes = []
    empty_graph.snapshot.return_value = empty_snapshot
    coordinator_empty = BlastCoordinator(
        observability=adapter,
        dependency_graph=empty_graph,
    )
    with pytest.raises(ValueError, match="contains no nodes"):
        coordinator_empty._get_declared_services()


@pytest.mark.asyncio
async def test_blast_coordinator_capture_baselines_twins() -> None:
    """Verify concurrent baseline capture across multiple twin environments."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    adapter = FakeObservabilityAdapter(clock=clock)
    graph = ServiceDependencyGraph(nodes=["edge-gateway"], edges=[])
    coordinator = BlastCoordinator(observability=adapter, dependency_graph=graph, clock=clock)

    # Empty twins returns empty dict
    assert await coordinator.capture_baselines(twins=[]) == {}

    twins = [
        TwinHandle(
            twin_id="twin_0",
            incident_id="inc_1",
            candidate_index=0,
            namespace="ust-twin-0",
            database="db0",
            forked_from_snapshot_at=clock.now(),
            state="ready",
        ),
        TwinHandle(
            twin_id="twin_1",
            incident_id="inc_1",
            candidate_index=1,
            namespace="ust-twin-1",
            database="db1",
            forked_from_snapshot_at=clock.now(),
            state="ready",
        ),
    ]

    baselines = await coordinator.capture_baselines(twins=twins)
    assert len(baselines) == 2
    assert "twin_0" in baselines
    assert "twin_1" in baselines
    assert baselines["twin_0"].namespace == "ust-twin-0"
    assert baselines["twin_1"].namespace == "ust-twin-1"


@pytest.mark.asyncio
async def test_blast_coordinator_capture_baseline_prod_fallback() -> None:
    """Verify capture_baseline falls back to prod_namespace when twin has 0 requests."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    graph = ServiceDependencyGraph(nodes=["svc-a", "svc-b", "svc-c", "svc-d"], edges=[])
    adapter = Mock()

    now = clock.now()

    async def _mock_metric_window(
        service: str, since: datetime, namespace: str, until: datetime | None = None
    ) -> MetricWindow:
        _ = (since, until)
        if namespace == "ust-twin-0":
            if service == "svc-d":
                return MetricWindow(
                    service=service,
                    start_time=now,
                    end_time=now,
                    p99_latency_ms=4.95,
                    error_rate=0.0,
                    request_count=4,
                )
            return MetricWindow(
                service=service,
                start_time=now,
                end_time=now,
                p99_latency_ms=0.0,
                error_rate=0.0,
                request_count=0,
            )
        if namespace == "ust-prod":
            if service == "svc-a":
                return MetricWindow(
                    service=service,
                    start_time=now,
                    end_time=now,
                    p99_latency_ms=250.0,
                    error_rate=0.05,
                    request_count=50,
                )
            if service == "svc-b":
                return MetricWindow(
                    service=service,
                    start_time=now,
                    end_time=now,
                    p99_latency_ms=0.0,
                    error_rate=0.0,
                    request_count=0,
                )
            if service == "svc-c":
                raise ObservabilityError("Prod scrape failed")
            if service == "svc-d":
                return MetricWindow(
                    service=service,
                    start_time=now,
                    end_time=now,
                    p99_latency_ms=50.0,
                    error_rate=0.0,
                    request_count=100,
                )
        return MetricWindow(
            service=service,
            start_time=now,
            end_time=now,
            p99_latency_ms=10.0,
            error_rate=0.0,
            request_count=10,
        )

    adapter.metric_window = AsyncMock(side_effect=_mock_metric_window)
    coordinator = BlastCoordinator(
        observability=adapter,
        dependency_graph=graph,
        clock=clock,
        prod_namespace="ust-prod",
    )

    baseline = await coordinator.capture_baseline(namespace="ust-twin-0")
    # svc-a: twin had 0, prod had 50 -> returns prod window
    assert baseline.baselines["svc-a"].p99_latency_ms == 250.0
    assert baseline.baselines["svc-a"].request_count == 50
    # svc-b: twin had 0, prod had 0 -> returns twin window
    assert baseline.baselines["svc-b"].request_count == 0
    # svc-c: twin had 0, prod threw error -> returns twin window
    assert baseline.baselines["svc-c"].request_count == 0
    # svc-d: twin had 4 (health probes), prod had 100 -> returns prod window
    assert baseline.baselines["svc-d"].p99_latency_ms == 50.0
    assert baseline.baselines["svc-d"].request_count == 100

    # Test prod_namespace itself (bypasses fallback branch 295->307)
    prod_baseline = await coordinator.capture_baseline(namespace="ust-prod")
    assert prod_baseline.baselines["svc-a"].request_count == 50


def test_evaluate_blast_observed_blast_set_contained_by_reachable() -> None:
    """Verify observed_blast_set contains only reachable dependents even if others degraded."""
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    graph = ServiceDependencyGraph(
        nodes=["data-service", "edge-gateway", "unrelated-worker"],
        edges=[("edge-gateway", "data-service")],
    )

    baselines = {
        "data-service": MetricWindow(
            service="data-service",
            start_time=now,
            end_time=now,
            p99_latency_ms=10.0,
            error_rate=0.0,
        ),
        "edge-gateway": MetricWindow(
            service="edge-gateway",
            start_time=now,
            end_time=now,
            p99_latency_ms=20.0,
            error_rate=0.0,
        ),
        "unrelated-worker": MetricWindow(
            service="unrelated-worker",
            start_time=now,
            end_time=now,
            p99_latency_ms=5.0,
            error_rate=0.0,
        ),
    }
    # Both edge-gateway and unrelated-worker degrade by >10%
    post_windows = {
        "data-service": MetricWindow(
            service="data-service",
            start_time=now,
            end_time=now,
            p99_latency_ms=10.0,
            error_rate=0.0,
        ),
        "edge-gateway": MetricWindow(
            service="edge-gateway",
            start_time=now,
            end_time=now,
            p99_latency_ms=50.0,
            error_rate=0.0,
        ),
        "unrelated-worker": MetricWindow(
            service="unrelated-worker",
            start_time=now,
            end_time=now,
            p99_latency_ms=50.0,
            error_rate=0.0,
        ),
    }

    plan = RemediationPlan(
        plan_id="plan_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service"),
        target_resources=[
            ResourceRef(namespace="ust-prod", kind="Deployment", name="data-service")
        ],
        origin="planner",
        rationale="Rollback",
    )

    res = evaluate_blast(plan, baselines, post_windows, graph)
    # affected_services includes unrelated-worker
    assert "unrelated-worker" in res.affected_services
    assert "edge-gateway" in res.affected_services
    # observed_blast_set is strictly intersected with reachable_set
    assert res.observed_blast_set == ["edge-gateway"]
    assert "unrelated-worker" not in res.observed_blast_set


@pytest.mark.asyncio
async def test_blast_coordinator_capture_post_apply_and_error_handling() -> None:
    """Verify capture_post_apply fetches windows and handles exceptions gracefully."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    graph = ServiceDependencyGraph(nodes=["svc-ok", "svc-err"], edges=[])

    adapter = Mock()

    async def _mock_metric(
        service: str, since: datetime, namespace: str, until: datetime | None = None
    ) -> MetricWindow:
        _ = (namespace, until)
        if service == "svc-err":
            raise ObservabilityError("Failed to scrape")
        return MetricWindow(
            service=service,
            start_time=since,
            end_time=since + timedelta(seconds=10),
            p99_latency_ms=120.0,
            error_rate=0.02,
        )

    adapter.metric_window = AsyncMock(side_effect=_mock_metric)
    coordinator = BlastCoordinator(observability=adapter, dependency_graph=graph, clock=clock)

    windows = await coordinator.capture_post_apply(
        namespace="ust-twin-0",
        applied_at=clock.now() - timedelta(seconds=20),
    )
    assert "svc-ok" in windows
    assert windows["svc-ok"].p99_latency_ms == 120.0
    assert "svc-err" in windows
    assert windows["svc-err"].p99_latency_ms == 0.0


@pytest.mark.asyncio
async def test_blast_coordinator_evaluate_twins_concurrent() -> None:
    """Verify evaluate_twins evaluates candidate twins concurrently."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    graph = ServiceDependencyGraph(
        nodes=["edge-gateway", "data-service"],
        edges=[("edge-gateway", "data-service")],
        request_shares={"edge-gateway": 0.60, "data-service": 0.40},
    )
    adapter = FakeObservabilityAdapter(clock=clock)
    coordinator = BlastCoordinator(observability=adapter, dependency_graph=graph, clock=clock)

    now = clock.now()
    twins = [
        TwinHandle(
            twin_id="twin_0",
            incident_id="inc_1",
            candidate_index=0,
            namespace="ust-twin-0",
            database="db0",
            forked_from_snapshot_at=now,
            state="ready",
        ),
        TwinHandle(
            twin_id="twin_1",
            incident_id="inc_1",
            candidate_index=1,
            namespace="ust-twin-1",
            database="db1",
            forked_from_snapshot_at=now,
            state="ready",
        ),
    ]
    plans = [
        RemediationPlan(
            plan_id="plan_0",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=ActionParams(workload="data-service"),
            target_resources=[
                ResourceRef(namespace="ust-twin-0", kind="Deployment", name="data-service")
            ],
            origin="planner",
            rationale="Rollback",
        ),
        # Plan without matching twin
        RemediationPlan(
            plan_id="plan_unmatched",
            candidate_index=99,
            action=ActionType.NO_ACTION,
            params=ActionParams(workload="data-service"),
            origin="planner",
            rationale="No twin",
        ),
    ]

    baselines = await coordinator.capture_baselines(twins=twins)
    evaluations = await coordinator.evaluate_twins(
        plans=plans,
        twins=twins,
        baselines=baselines,
        applied_at=now,
    )
    assert len(evaluations) == 1
    assert "plan_0" in evaluations
    assert evaluations["plan_0"].target_service == "data-service"

    # Test baseline keyed by twin.namespace instead of twin.twin_id
    baselines_by_ns = {
        twins[0].namespace: baselines["twin_0"],
        twins[1].namespace: baselines["twin_1"],
    }
    evaluations_ns = await coordinator.evaluate_twins(
        plans=plans,
        twins=twins,
        baselines=baselines_by_ns,
        applied_at=now,
    )
    assert len(evaluations_ns) == 1
    assert "plan_0" in evaluations_ns

    # Test missing baseline for all twins
    evaluations_empty_base = await coordinator.evaluate_twins(
        plans=plans,
        twins=twins,
        baselines={},
        applied_at=now,
    )
    assert evaluations_empty_base == {}

    # Empty plans yields empty dict
    assert await coordinator.evaluate_twins([], twins, baselines, now) == {}


@pytest.mark.asyncio
async def test_fake_blast_tracker_conformance() -> None:
    """Verify FakeBlastTracker conforms to BlastTracker Protocol and behaves deterministically."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    fake = FakeBlastTracker(
        affected_services={"edge-gateway"},
        blast_radius=0.40,
        downstream_error_delta=0.03,
        clock=clock,
    )
    assert isinstance(fake, BlastTracker)

    baseline = await fake.capture_baseline(namespace="ust-twin-0")
    assert baseline.namespace == "ust-twin-0"
    assert "edge-gateway" in baseline.baselines

    plan = RemediationPlan(
        plan_id="plan_test",
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload="data-service"),
        target_resources=[ResourceRef(namespace="test", kind="Deployment", name="data-service")],
        origin="planner",
        rationale="Restart",
    )
    res = await fake.evaluate_environment(
        plan=plan,
        namespace="ust-twin-0",
        baseline=baseline,
        applied_at=clock.now(),
    )
    assert res.target_service == "data-service"
    assert res.affected_services == ["edge-gateway"]
    assert res.blast_radius == 0.40
    assert res.downstream_error_delta == 0.03
