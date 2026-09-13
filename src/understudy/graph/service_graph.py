"""Service dependency graph constructed from dependencies.yaml with traffic cross-checking."""

from collections import deque
from pathlib import Path

import httpx
import yaml
from pydantic import ValidationError

from understudy.common.clock import Clock, resolve_clock
from understudy.common.errors import GraphDiscrepancyError, GraphError
from understudy.common.logging import get_logger
from understudy.contracts.incident import DependencyEdge, DependencyGraphSnapshot
from understudy.graph.api import DependencyGraph
from understudy.graph.models import (
    CrossCheckReport,
    ObservedTraffic,
    ServiceManifest,
)
from understudy.graph.traffic import TrafficObserver

logger = get_logger(__name__)

# Default canonical request shares for demo services per architecture §2.6
DEFAULT_DEMO_REQUEST_SHARES: dict[str, float] = {
    "edge-gateway": 0.40,
    "auth-service": 0.30,
    "data-service": 0.20,
    "worker": 0.10,
}


class ServiceDependencyGraph(DependencyGraph):
    """Production service dependency DAG loaded from declarative manifest."""

    def __init__(
        self,
        nodes: list[str],
        edges: list[tuple[str, str]],
        descriptions: dict[str, str] | None = None,
        clock: Clock | None = None,
        request_shares: dict[str, float] | None = None,
    ) -> None:
        self.clock: Clock = resolve_clock(clock)
        self._nodes: list[str] = sorted(set(nodes))
        self._edges: set[tuple[str, str]] = set(edges)
        self._descriptions: dict[str, str] = descriptions or {}
        self._request_shares: dict[str, float] = dict(request_shares) if request_shares else {}

        # Adjacency structures
        # source -> target: source calls target (source depends on target)
        self._dependencies: dict[str, set[str]] = {n: set() for n in self._nodes}
        # target -> callers: callers that depend on target
        self._dependents: dict[str, set[str]] = {n: set() for n in self._nodes}

        for src, dst in self._edges:
            if src in self._dependencies and dst in self._dependencies:
                self._dependencies[src].add(dst)
                self._dependents[dst].add(src)

    @classmethod
    def from_yaml(
        cls,
        path: Path | str,
        clock: Clock | None = None,
    ) -> "ServiceDependencyGraph":
        """Load, validate, and construct the service dependency DAG from dependencies.yaml."""
        yaml_path = Path(path)
        if not yaml_path.exists():
            raise GraphError(
                f"Dependency manifest file not found: {yaml_path}",
                details={"path": str(yaml_path)},
            )

        try:
            with yaml_path.open(encoding="utf-8") as f:
                raw_data = yaml.safe_load(f)
        except Exception as exc:
            raise GraphError(
                f"Failed to parse YAML manifest at {yaml_path}: {exc}",
                details={"path": str(yaml_path)},
            ) from exc

        if not isinstance(raw_data, dict):
            raise GraphError(
                f"Invalid dependency manifest format in {yaml_path}: expected dictionary",
                details={"data_type": type(raw_data).__name__},
            )

        try:
            manifest = ServiceManifest.model_validate(raw_data)
        except ValidationError as exc:
            raise GraphError(
                f"Validation failed for dependency manifest schema at {yaml_path}: {exc}",
                details={"errors": exc.errors()},
            ) from exc

        node_names = [s.name for s in manifest.services]
        node_set = set(node_names)
        descriptions = {s.name: s.description for s in manifest.services}

        edges: list[tuple[str, str]] = []
        for e in manifest.edges:
            if e.source not in node_set:
                raise GraphError(
                    f"Edge source {e.source!r} is not declared under services in {yaml_path}",
                    details={"source": e.source},
                )
            if e.target not in node_set:
                raise GraphError(
                    f"Edge target {e.target!r} is not declared under services in {yaml_path}",
                    details={"target": e.target},
                )
            edges.append((e.source, e.target))

        # Check for cycles using Kahn's algorithm
        in_degree = dict.fromkeys(node_names, 0)
        adjacency: dict[str, list[str]] = {n: [] for n in node_names}
        for src, dst in edges:
            adjacency[src].append(dst)
            in_degree[dst] += 1

        queue = deque([n for n, deg in in_degree.items() if deg == 0])
        visited_count = 0
        while queue:
            node = queue.popleft()
            visited_count += 1
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(node_names):
            raise GraphError(
                f"Cyclic dependency detected in {yaml_path}: not a valid DAG",
                details={"node_count": len(node_names), "visited": visited_count},
            )

        return cls(
            nodes=node_names,
            edges=edges,
            descriptions=descriptions,
            clock=clock,
        )

    def dependents(self, service: str) -> set[str]:
        """Return the immediate callers depending on service."""
        if service not in self._dependents:
            raise GraphError(f"Unknown service: {service!r}")
        return set(self._dependents[service])

    def reachable_set(self, service: str) -> set[str]:
        """Return the transitive closure of dependents that rely on service."""
        if service not in self._dependents:
            raise GraphError(f"Unknown service: {service!r}")

        visited: set[str] = set()
        queue = list(self._dependents[service])
        while queue:
            node = queue.pop(0)
            if node not in visited:
                visited.add(node)
                queue.extend(self._dependents.get(node, set()))
        return visited

    def request_share(self, service: str) -> float:
        """Return relative request share for service in [0.0, 1.0]."""
        if service not in self._nodes:
            return 0.0
        if service in self._request_shares:
            return self._request_shares[service]
        # Fall back to canonical demo shares if applicable
        if service in DEFAULT_DEMO_REQUEST_SHARES:
            return DEFAULT_DEMO_REQUEST_SHARES[service]
        return 1.0 / len(self._nodes)

    def set_request_shares(self, shares: dict[str, float]) -> None:
        """Explicitly set or calibrate request shares from observed traffic."""
        self._request_shares = dict(shares)

    def snapshot(self) -> DependencyGraphSnapshot:
        """Export an immutable snapshot of graph topology."""
        return DependencyGraphSnapshot(
            nodes=list(self._nodes),
            edges=[
                DependencyEdge(source=s, target=t)
                for s, t in sorted(self._edges, key=lambda x: (x[0], x[1]))
            ],
            observed_at=self.clock.now(),
        )

    def format_chains(self) -> str:
        """Format maximal call paths from entrypoints to terminal services."""
        # Entrypoints: services with no callers calling them
        entrypoints = sorted([n for n in self._nodes if len(self._dependents[n]) == 0])

        all_paths: list[list[str]] = []

        def _dfs(current: str, current_path: list[str]) -> None:
            outgoing = sorted(self._dependencies.get(current, set()))
            if not outgoing:
                all_paths.append(current_path)
                return
            for nxt in outgoing:
                _dfs(nxt, [*current_path, nxt])

        for entry in entrypoints:
            _dfs(entry, [entry])

        # Filter out sub-paths if a longer path subsumes it
        maximal_paths: list[list[str]] = []
        for p in all_paths:
            is_subpath = False
            for other in all_paths:
                if len(other) > len(p) and p[0] == other[0] and p[-1] == other[-1]:
                    # If this is a direct edge that bypasses intermediate node in other
                    is_subpath = True
                    break
            if not is_subpath:
                maximal_paths.append(p)

        # Build formatted strings
        path_strs = [" -> ".join(p) for p in maximal_paths]
        return ", ".join(path_strs)

    def cross_check(
        self,
        observed: ObservedTraffic,
        raise_on_mismatch: bool = True,
    ) -> CrossCheckReport:
        """Cross-check declared graph against observed traffic telemetry."""
        declared_nodes = set(self._nodes)
        undeclared_services = observed.services - declared_nodes
        undeclared_edges = observed.edges - self._edges

        mismatches: list[str] = []
        if undeclared_services:
            mismatches.append(
                f"Undeclared services observed in traffic: {sorted(undeclared_services)}"
            )
        if undeclared_edges:
            mismatches.append(
                f"Undeclared call edges observed in traffic: {sorted(undeclared_edges)}"
            )

        if mismatches:
            msg = (
                "Discrepancy detected between declared graph and observed traffic: "
                f"{'; '.join(mismatches)}"
            )
            if raise_on_mismatch:
                raise GraphDiscrepancyError(
                    msg,
                    details={
                        "undeclared_services": sorted(undeclared_services),
                        "undeclared_edges": sorted(undeclared_edges),
                    },
                )
            return CrossCheckReport(
                status="MISMATCH",
                declared_services=declared_nodes,
                observed_services=observed.services,
                declared_edges=self._edges,
                observed_edges=observed.edges,
                undeclared_services=undeclared_services,
                undeclared_edges=undeclared_edges,
                message=msg,
            )

        # If traffic observed, update request shares accordingly
        if observed.total_requests > 0:
            updated_shares: dict[str, float] = {}
            for svc in self._nodes:
                cnt = observed.request_counts.get(svc, 0.0)
                updated_shares[svc] = round(cnt / observed.total_requests, 4)
            self._request_shares = updated_shares

        return CrossCheckReport(
            status="OK",
            declared_services=declared_nodes,
            observed_services=observed.services,
            declared_edges=self._edges,
            observed_edges=observed.edges,
            undeclared_services=set(),
            undeclared_edges=set(),
            message="declared graph matches observed traffic: OK",
        )

    async def cross_check_prometheus(
        self,
        prometheus_url: str | None = None,
        namespace: str = "ust-prod",
        client: httpx.AsyncClient | None = None,
        raise_on_mismatch: bool = True,
    ) -> CrossCheckReport:
        """Fetch live traffic from Prometheus and cross-check against declared topology."""
        observer = TrafficObserver(base_url=prometheus_url, client=client)
        try:
            observed = await observer.observe(namespace=namespace)
        finally:
            await observer.close()

        return self.cross_check(observed, raise_on_mismatch=raise_on_mismatch)
