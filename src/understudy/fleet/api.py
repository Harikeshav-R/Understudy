"""Fleet component protocol interfaces."""

from typing import Protocol, runtime_checkable

from understudy.contracts.twin import TwinHandle
from understudy.fleet.models import (
    ClusterWorkloadSnapshot,
    ContainerSnapshot,
    EnvVar,
    ResourceSpec,
    TwinManifestBundle,
    WorkloadSnapshot,
)
from understudy.fleet.render import TwinManifestRenderer, render_twin_manifests


@runtime_checkable
class FleetController(Protocol):
    """Twin namespace and database lifecycle controller."""

    async def fork(self, incident_id: str, n: int) -> list[TwinHandle]:
        """Concurrently fork N isolated twin environments for an incident."""
        raise NotImplementedError

    async def teardown(self, twin_handle: TwinHandle) -> None:
        """Tear down a specific twin namespace and its cloned database."""
        raise NotImplementedError

    async def teardown_all(self, incident_id: str) -> None:
        """Idempotently tear down all twin environments associated with an incident."""
        raise NotImplementedError


@runtime_checkable
class WorkloadReader(Protocol):
    """Protocol for reading workloads and configuration from a cluster namespace."""

    async def read_workloads(
        self,
        namespace: str,
        exclude_components: set[str] | None = None,
    ) -> ClusterWorkloadSnapshot:
        """Read all workloads in a namespace, resolving images to digests."""
        raise NotImplementedError


@runtime_checkable
class ManifestRenderer(Protocol):
    """Protocol for rendering twin Kubernetes manifests from a cluster snapshot."""

    def render(
        self,
        snapshot: ClusterWorkloadSnapshot,
        incident_id: str,
        candidate_index: int,
        database_name: str | None = None,
    ) -> TwinManifestBundle:
        """Render isolated twin manifests for a specific candidate."""
        raise NotImplementedError


__all__ = [
    "ClusterWorkloadSnapshot",
    "ContainerSnapshot",
    "EnvVar",
    "FleetController",
    "ManifestRenderer",
    "ResourceSpec",
    "TwinManifestBundle",
    "TwinManifestRenderer",
    "WorkloadReader",
    "WorkloadSnapshot",
    "render_twin_manifests",
]
