"""Fleet component protocol interfaces."""

from datetime import datetime
from typing import Protocol, runtime_checkable

from understudy.contracts.twin import TwinHandle
from understudy.fleet.models import (
    ClusterWorkloadSnapshot,
    ContainerSnapshot,
    DatabaseCloneResult,
    DatabaseSnapshotMetadata,
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


@runtime_checkable
class SnapshotRefresher(Protocol):
    """Protocol for refreshing the PostgreSQL snapshot template from production."""

    async def refresh_snapshot(self) -> datetime:
        """Perform a staging-and-rename refresh from production to snapshot_template."""
        raise NotImplementedError

    async def get_last_snapshot_time(self) -> datetime | None:
        """Return the timestamp of the last successful snapshot refresh."""
        raise NotImplementedError

    async def start(self) -> None:
        """Start the background periodic refresh loop."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Gracefully stop the background periodic refresh loop."""
        raise NotImplementedError


@runtime_checkable
class DatabaseCloner(Protocol):
    """Protocol for cloning and managing isolated twin databases."""

    async def clone_twin_database(
        self, incident_id: str, candidate_index: int
    ) -> DatabaseCloneResult:
        """Clone snapshot_template into twin_<incident>_<candidate> with retry."""
        raise NotImplementedError

    async def drop_twin_database(self, database_name: str) -> None:
        """Drop a twin database."""
        raise NotImplementedError

    async def drop_all_incident_databases(self, incident_id: str) -> list[str]:
        """Drop all twin databases for an incident."""
        raise NotImplementedError

    async def list_twin_databases(self, incident_id: str | None = None) -> list[str]:
        """List twin databases matching optional incident_id prefix."""
        raise NotImplementedError

    async def get_item_count(self, database_name: str) -> int:
        """Count rows in the items table for fidelity verification."""
        raise NotImplementedError


__all__ = [
    "ClusterWorkloadSnapshot",
    "ContainerSnapshot",
    "DatabaseCloneResult",
    "DatabaseCloner",
    "DatabaseSnapshotMetadata",
    "EnvVar",
    "FleetController",
    "ManifestRenderer",
    "ResourceSpec",
    "SnapshotRefresher",
    "TwinManifestBundle",
    "TwinManifestRenderer",
    "WorkloadReader",
    "WorkloadSnapshot",
    "render_twin_manifests",
]
