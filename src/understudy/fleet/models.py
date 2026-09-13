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
