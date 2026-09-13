"""Unit tests for ServiceDependencyGraph loading, cycle detection, and cross-checking."""

from pathlib import Path

import pytest

from understudy.common.clock import FrozenClock
from understudy.common.errors import GraphDiscrepancyError, GraphError
from understudy.graph.models import ObservedTraffic
from understudy.graph.service_graph import ServiceDependencyGraph


def test_from_yaml_valid_prod_manifest() -> None:
    """Validate loading deploy/prod/dependencies.yaml."""
    clock = FrozenClock()
    graph = ServiceDependencyGraph.from_yaml("deploy/prod/dependencies.yaml", clock=clock)

    assert graph.snapshot().nodes == ["auth-service", "data-service", "edge-gateway", "worker"]
    assert len(graph.snapshot().edges) == 4
    assert graph.snapshot().observed_at == clock.now()

    # Dependents: who calls the service
    assert graph.dependents("auth-service") == {"edge-gateway"}
    assert graph.dependents("data-service") == {"auth-service", "edge-gateway", "worker"}
    assert graph.dependents("edge-gateway") == set()
    assert graph.dependents("worker") == set()

    # Reachable set: transitive callers
    assert graph.reachable_set("data-service") == {"auth-service", "edge-gateway", "worker"}
    assert graph.reachable_set("auth-service") == {"edge-gateway"}
    assert graph.reachable_set("edge-gateway") == set()
    assert graph.reachable_set("worker") == set()

    # Request share
    assert graph.request_share("edge-gateway") == 0.40
    assert graph.request_share("auth-service") == 0.30
    assert graph.request_share("data-service") == 0.20
    assert graph.request_share("worker") == 0.10
    assert graph.request_share("unknown") == 0.0

    # Chain representation
    assert (
        graph.format_chains()
        == "edge-gateway -> auth-service -> data-service, worker -> data-service"
    )


def test_from_yaml_missing_file(tmp_path: Path) -> None:
    """Loading nonexistent YAML raises GraphError."""
    with pytest.raises(GraphError, match="Dependency manifest file not found"):
        ServiceDependencyGraph.from_yaml(tmp_path / "nonexistent.yaml")


def test_from_yaml_invalid_yaml(tmp_path: Path) -> None:
    """Malformed YAML content raises GraphError."""
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("version: 1\nservices: [unclosed")
    with pytest.raises(GraphError, match="Failed to parse YAML manifest"):
        ServiceDependencyGraph.from_yaml(bad_yaml)


def test_from_yaml_not_a_dict(tmp_path: Path) -> None:
    """YAML that parses to a list raises GraphError."""
    bad_yaml = tmp_path / "list.yaml"
    bad_yaml.write_text("- item1\n- item2\n")
    with pytest.raises(GraphError, match="expected dictionary"):
        ServiceDependencyGraph.from_yaml(bad_yaml)


def test_from_yaml_validation_error(tmp_path: Path) -> None:
    """Schema missing required fields raises GraphError."""
    bad_yaml = tmp_path / "schema.yaml"
    bad_yaml.write_text("version: 1\nservices:\n  - description: missing name\n")
    with pytest.raises(GraphError, match="Validation failed for dependency manifest schema"):
        ServiceDependencyGraph.from_yaml(bad_yaml)


def test_from_yaml_unknown_edge_nodes(tmp_path: Path) -> None:
    """Edges referencing undeclared services raise GraphError."""
    # 1. Unknown source
    yaml1 = tmp_path / "unknown_src.yaml"
    yaml1.write_text(
        """
version: "1"
services:
  - name: svc-a
edges:
  - source: ghost-svc
    target: svc-a
"""
    )
    with pytest.raises(GraphError, match="Edge source 'ghost-svc' is not declared"):
        ServiceDependencyGraph.from_yaml(yaml1)

    # 2. Unknown target
    yaml2 = tmp_path / "unknown_dst.yaml"
    yaml2.write_text(
        """
version: "1"
services:
  - name: svc-a
edges:
  - source: svc-a
    target: ghost-target
"""
    )
    with pytest.raises(GraphError, match="Edge target 'ghost-target' is not declared"):
        ServiceDependencyGraph.from_yaml(yaml2)


def test_from_yaml_cycle_detection(tmp_path: Path) -> None:
    """Cyclic dependencies raise GraphError."""
    cycle_yaml = tmp_path / "cycle.yaml"
    cycle_yaml.write_text(
        """
version: "1"
services:
  - name: svc-a
  - name: svc-b
  - name: svc-c
edges:
  - source: svc-a
    target: svc-b
  - source: svc-b
    target: svc-c
  - source: svc-c
    target: svc-a
"""
    )
    with pytest.raises(GraphError, match="Cyclic dependency detected"):
        ServiceDependencyGraph.from_yaml(cycle_yaml)


def test_unknown_service_queries() -> None:
    """Querying unknown services for dependents or reachable_set raises GraphError."""
    graph = ServiceDependencyGraph(nodes=["a", "b"], edges=[("a", "b")])
    with pytest.raises(GraphError, match="Unknown service: 'ghost'"):
        graph.dependents("ghost")

    with pytest.raises(GraphError, match="Unknown service: 'ghost'"):
        graph.reachable_set("ghost")


def test_request_shares_custom_and_fallback() -> None:
    """Verify request share calculation with custom nodes, fallback, and overrides."""
    # 1. Unknown services without demo defaults get equal share 1/N
    graph = ServiceDependencyGraph(nodes=["custom-a", "custom-b"], edges=[])
    assert graph.request_share("custom-a") == 0.5
    assert graph.request_share("custom-b") == 0.5
    assert graph.request_share("unknown") == 0.0

    # 2. Empty graph
    empty_graph = ServiceDependencyGraph(nodes=[], edges=[])
    assert empty_graph.request_share("anything") == 0.0

    # 3. Explicit override
    graph.set_request_shares({"custom-a": 0.8, "custom-b": 0.2})
    assert graph.request_share("custom-a") == 0.8
    assert graph.request_share("custom-b") == 0.2


def test_cross_check_clean() -> None:
    """Cross-check passes when observed traffic matches declared topology."""
    graph = ServiceDependencyGraph.from_yaml("deploy/prod/dependencies.yaml")
    traffic = ObservedTraffic(
        services={"edge-gateway", "auth-service", "data-service", "worker"},
        edges={("edge-gateway", "auth-service"), ("auth-service", "data-service")},
        request_counts={"edge-gateway": 50, "auth-service": 30, "data-service": 20, "worker": 0},
        total_requests=100,
    )
    report = graph.cross_check(traffic, raise_on_mismatch=True)
    assert report.status == "OK"
    assert "declared graph matches observed traffic: OK" in report.message
    # Shares calibrated from observed counts
    assert graph.request_share("edge-gateway") == 0.50
    assert graph.request_share("auth-service") == 0.30
    assert graph.request_share("data-service") == 0.20


def test_cross_check_mismatches() -> None:
    """Cross-check flags undeclared services and undeclared edges."""
    graph = ServiceDependencyGraph.from_yaml("deploy/prod/dependencies.yaml")
    traffic = ObservedTraffic(
        services={"edge-gateway", "auth-service", "rogue-service"},
        edges={("rogue-service", "data-service")},
        request_counts={"rogue-service": 10},
        total_requests=10,
    )

    # 1. With raise_on_mismatch=True
    with pytest.raises(GraphDiscrepancyError, match="Discrepancy detected"):
        graph.cross_check(traffic, raise_on_mismatch=True)

    # 2. With raise_on_mismatch=False
    report = graph.cross_check(traffic, raise_on_mismatch=False)
    assert report.status == "MISMATCH"
    assert "rogue-service" in report.undeclared_services
    assert ("rogue-service", "data-service") in report.undeclared_edges


@pytest.mark.asyncio
async def test_cross_check_prometheus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify cross_check_prometheus coordinates observer and cross-checker."""
    from understudy.graph.traffic import TrafficObserver

    graph = ServiceDependencyGraph.from_yaml("deploy/prod/dependencies.yaml")

    mock_traffic = ObservedTraffic(
        services={"edge-gateway", "auth-service", "data-service", "worker"},
        edges={("edge-gateway", "auth-service"), ("worker", "data-service")},
        request_counts={"edge-gateway": 10},
        total_requests=10,
    )

    async def _mock_observe(_self: TrafficObserver, namespace: str = "ust-prod") -> ObservedTraffic:
        assert namespace == "test-ns"
        return mock_traffic

    async def _mock_close(_self: TrafficObserver) -> None:
        pass

    monkeypatch.setattr(TrafficObserver, "observe", _mock_observe)
    monkeypatch.setattr(TrafficObserver, "close", _mock_close)

    report = await graph.cross_check_prometheus(namespace="test-ns")
    assert report.status == "OK"


def test_cross_check_zero_requests() -> None:
    """Cross-check with zero requests preserves existing request shares."""
    graph = ServiceDependencyGraph.from_yaml("deploy/prod/dependencies.yaml")
    initial_share = graph.request_share("edge-gateway")
    traffic = ObservedTraffic(
        services={"edge-gateway", "auth-service", "data-service", "worker"},
        edges=set(),
        request_counts={},
        total_requests=0.0,
    )
    report = graph.cross_check(traffic, raise_on_mismatch=True)
    assert report.status == "OK"
    assert graph.request_share("edge-gateway") == initial_share


def test_service_dependency_graph_edge_filtering() -> None:
    """Direct construction with an edge referencing undeclared nodes handles gracefully."""
    graph = ServiceDependencyGraph(nodes=["a"], edges=[("a", "b")])
    assert graph.dependents("a") == set()
    assert graph.reachable_set("a") == set()
