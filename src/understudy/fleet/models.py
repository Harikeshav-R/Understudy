"""Data models representing captured Kubernetes workload snapshots and configuration."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ResourceSpec(BaseModel):
    """Resource requests and limits for a container."""

    model_config = ConfigDict(frozen=True)

    requests: dict[str, str] = Field(default_factory=dict)
    limits: dict[str, str] = Field(default_factory=dict)


class EnvVar(BaseModel):
    """Container environment variable specification."""

    model_config = ConfigDict(frozen=True)

    name: str
    value: str | None = None
    value_from: dict[str, Any] | None = None


class ContainerSnapshot(BaseModel):
    """Snapshot of a container in a workload with resolved digest."""

    model_config = ConfigDict(frozen=True)

    name: str
    image_tag: str
    image_digest: str
    pinned_image: str
    resources: ResourceSpec
    env: list[EnvVar] = Field(default_factory=list)
    ports: list[dict[str, Any]] = Field(default_factory=list)
    liveness_probe: dict[str, Any] | None = None
    readiness_probe: dict[str, Any] | None = None


class WorkloadSnapshot(BaseModel):
    """Snapshot of a Kubernetes workload (Deployment)."""

    model_config = ConfigDict(frozen=True)

    name: str
    namespace: str
    component: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    replicas: int = 1
    containers: list[ContainerSnapshot] = Field(default_factory=list)
    config_map_refs: list[str] = Field(default_factory=list)
    volumes: list[dict[str, Any]] = Field(default_factory=list)


class ClusterWorkloadSnapshot(BaseModel):
    """Full snapshot of production workloads and referenced configuration."""

    model_config = ConfigDict(frozen=True)

    namespace: str
    workloads: dict[str, WorkloadSnapshot] = Field(default_factory=dict)
    config_maps: dict[str, dict[str, str]] = Field(default_factory=dict)
    captured_at: datetime


class TwinManifestBundle(BaseModel):
    """Rendered Kubernetes manifests for an isolated twin environment."""

    model_config = ConfigDict(frozen=True)

    twin_id: str
    incident_id: str
    candidate_index: int
    namespace: str
    database_name: str
    database_dsn: str
    manifests: list[dict[str, Any]] = Field(default_factory=list)

    def all_manifests(self) -> list[dict[str, Any]]:
        """Return a copy of all manifests in the bundle."""
        return list(self.manifests)

    def to_yaml(self) -> str:
        """Serialize all manifests in the bundle to a multi-document YAML string."""
        import yaml

        return yaml.safe_dump_all(self.manifests, sort_keys=False)

    @property
    def namespace_manifest(self) -> dict[str, Any] | None:
        """Return the Namespace manifest if present."""
        return next((m for m in self.manifests if m.get("kind") == "Namespace"), None)

    @property
    def network_policy_manifest(self) -> dict[str, Any] | None:
        """Return the NetworkPolicy manifest if present."""
        return next((m for m in self.manifests if m.get("kind") == "NetworkPolicy"), None)

    @property
    def rbac_manifests(self) -> list[dict[str, Any]]:
        """Return all ServiceAccount and RoleBinding manifests."""
        return [m for m in self.manifests if m.get("kind") in ("ServiceAccount", "RoleBinding")]

    @property
    def config_map_manifests(self) -> list[dict[str, Any]]:
        """Return all ConfigMap manifests."""
        return [m for m in self.manifests if m.get("kind") == "ConfigMap"]

    @property
    def service_manifests(self) -> list[dict[str, Any]]:
        """Return all Service manifests."""
        return [m for m in self.manifests if m.get("kind") == "Service"]

    @property
    def deployment_manifests(self) -> list[dict[str, Any]]:
        """Return all Deployment manifests."""
        return [m for m in self.manifests if m.get("kind") == "Deployment"]


class DatabaseSnapshotMetadata(BaseModel):
    """Metadata recorded for a successful snapshot refresh cycle."""

    model_config = ConfigDict(frozen=True)

    snapshot_name: str
    source_database: str
    source_namespace: str
    refreshed_at: datetime
    duration_seconds: float = 0.0


class DatabaseCloneResult(BaseModel):
    """Result of cloning a twin database from the snapshot template."""

    model_config = ConfigDict(frozen=True)

    database_name: str
    incident_id: str
    candidate_index: int
    forked_from_snapshot_at: datetime
    cloned_at: datetime
