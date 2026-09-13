"""Unit tests for ServiceBlastRadiusCalculator and affected service detection."""

from unittest.mock import Mock

import pytest

from understudy.common.errors import GraphError
from understudy.graph.calculator import ServiceBlastRadiusCalculator
from understudy.graph.service_graph import ServiceDependencyGraph


def test_blast_radius_calculation() -> None:
    """Validate normalized blast score according to transitive dependents and shares."""
    graph = ServiceDependencyGraph.from_yaml("deploy/prod/dependencies.yaml")
    calc = ServiceBlastRadiusCalculator(graph)

    # 1. No affected services yields 0.0
    assert calc.calculate("data-service", set()) == 0.0

    # 2. Target without dependents yields 0.0
    assert calc.calculate("edge-gateway", {"data-service", "auth-service"}) == 0.0

    # 3. Affected dependents: auth-service (0.30)
    assert calc.calculate("data-service", {"auth-service"}) == pytest.approx(0.30)

    # 4. Affected dependents: auth-service (0.30) + edge-gateway (0.40) = 0.70
    assert calc.calculate("data-service", {"auth-service", "edge-gateway"}) == pytest.approx(0.70)

    # 5. Affected dependents: auth (0.30) + edge (0.40) + worker (0.10) = 0.80
    assert calc.calculate(
        "data-service", {"auth-service", "edge-gateway", "worker"}
    ) == pytest.approx(0.80)

    # 6. Affected set with unrelated services only counts reachable
    assert calc.calculate("auth-service", {"edge-gateway", "worker"}) == pytest.approx(0.40)


def test_blast_radius_unknown_target() -> None:
    """Calculating blast radius for undeclared target raises GraphError."""
    graph = ServiceDependencyGraph.from_yaml("deploy/prod/dependencies.yaml")
    calc = ServiceBlastRadiusCalculator(graph)
    with pytest.raises(GraphError, match="Unknown service: 'nonexistent'"):
        calc.calculate("nonexistent", {"auth-service"})


def test_blast_radius_clamping() -> None:
    """Blast radius is clamped to [0.0, 1.0]."""
    graph = ServiceDependencyGraph(
        nodes=["target", "caller1", "caller2"],
        edges=[("caller1", "target"), ("caller2", "target")],
        request_shares={"caller1": 0.7, "caller2": 0.8},  # sum > 1.0
    )
    calc = ServiceBlastRadiusCalculator(graph)
    assert calc.calculate("target", {"caller1", "caller2"}) == 1.0


def test_compute_affected_services() -> None:
    """Validate degradation detection logic (>10% increase in p99 or error rate)."""
    # Baseline windows
    base_data = Mock(p99_latency_ms=100.0, error_rate=0.01)
    base_auth = Mock(p99_latency_ms=50.0, error_rate=0.0)
    base_edge = Mock(p99_latency_ms=20.0, error_rate=0.0)

    baselines = {
        "data-service": base_data,
        "auth-service": base_auth,
        "edge-gateway": base_edge,
    }

    # Post windows:
    # data-service: p99 increased 100 -> 120 (+20% > 10%) -> affected
    # auth-service: error_rate 0 -> 0.15 (> 0.10 threshold) -> affected
    # edge-gateway: p99 20 -> 21 (+5% <= 10%), error_rate 0 -> not affected
    # worker: no baseline -> skipped
    post_data = Mock(p99_latency_ms=120.0, error_rate=0.01)
    post_auth = Mock(p99_latency_ms=50.0, error_rate=0.15)
    post_edge = Mock(p99_latency_ms=21.0, error_rate=0.0)
    post_worker = Mock(p99_latency_ms=10.0, error_rate=0.0)

    post_windows = {
        "data-service": post_data,
        "auth-service": post_auth,
        "edge-gateway": post_edge,
        "worker": post_worker,
    }

    affected = ServiceBlastRadiusCalculator.compute_affected_services(baselines, post_windows)
    assert "data-service" in affected
    assert "auth-service" in affected
    assert "edge-gateway" not in affected
    assert "worker" not in affected


def test_compute_affected_services_zero_baselines() -> None:
    """Verify zero baselines handling when post values increase or remain zero."""
    baselines = {
        "svc-a": Mock(p99_latency_ms=0.0, error_rate=0.0),
        "svc-b": Mock(p99_latency_ms=0.0, error_rate=0.0),
        "svc-c": Mock(p99_latency_ms=10.0, error_rate=0.01),
    }
    post_windows = {
        "svc-a": Mock(p99_latency_ms=15.0, error_rate=0.0),
        "svc-b": Mock(p99_latency_ms=0.0, error_rate=0.0),
        "svc-c": Mock(p99_latency_ms=10.0, error_rate=0.05),  # 0.01 -> 0.05 (+400% > 10%)
    }
    affected = ServiceBlastRadiusCalculator.compute_affected_services(baselines, post_windows)
    assert "svc-a" in affected
    assert "svc-b" not in affected
    assert "svc-c" in affected


def test_compute_affected_services_zero_baseline_error_spike() -> None:
    """A fresh 0% -> 5% error rate from a zero baseline must be flagged as affected."""
    baselines = {"data-service": Mock(p99_latency_ms=0.0, error_rate=0.0)}
    post_windows = {"data-service": Mock(p99_latency_ms=0.0, error_rate=0.05)}
    affected = ServiceBlastRadiusCalculator.compute_affected_services(baselines, post_windows)
    assert "data-service" in affected
