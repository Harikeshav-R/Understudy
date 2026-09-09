"""Deterministic fake dependency graph implementations."""

from datetime import UTC, datetime

from understudy.contracts.incident import DependencyEdge, DependencyGraphSnapshot
from understudy.graph.api import BlastRadiusCalculator, DependencyGraph


class FakeDependencyGraph(DependencyGraph):
    """Deterministic in-memory service dependency graph."""

    def __init__(self) -> None:
        self._nodes = ["edge-gateway", "auth-service", "data-service", "worker"]
        self._edges = [
            ("edge-gateway", "auth-service"),
            ("edge-gateway", "data-service"),
            ("auth-service", "data-service"),
            ("worker", "data-service"),
        ]

    def dependents(self, service: str) -> set[str]:
        """Return immediate callers depending on service."""
        return {src for src, dst in self._edges if dst == service}

    def reachable_set(self, service: str) -> set[str]:
        """Return transitive dependents reachable from service."""
        visited: set[str] = set()
        queue = list(self.dependents(service))
        while queue:
            node = queue.pop(0)
            if node not in visited:
                visited.add(node)
                queue.extend(self.dependents(node))
        return visited

    def request_share(self, service: str) -> float:
        """Return relative request share for service."""
        shares = {
            "edge-gateway": 0.40,
            "auth-service": 0.30,
            "data-service": 0.20,
            "worker": 0.10,
        }
        return shares.get(service, 0.0)

    def snapshot(self) -> DependencyGraphSnapshot:
        """Return graph snapshot."""
        return DependencyGraphSnapshot(
            nodes=list(self._nodes),
            edges=[DependencyEdge(source=s, target=t) for s, t in self._edges],
            observed_at=datetime.now(UTC),
        )


class FakeBlastRadiusCalculator(BlastRadiusCalculator):
    """Deterministic blast radius calculator."""

    def calculate(self, target_service: str, affected_services: set[str]) -> float:
        """Return a normalized blast radius proportion."""
        _ = target_service
        if not affected_services:
            return 0.0
        return min(len(affected_services) / 4.0, 1.0)
