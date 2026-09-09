"""Incident and context contract models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from understudy.contracts.enums import FailureClass


class MetricPoint(BaseModel):
    """A single metric measurement point."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    value: float


class MetricSeries(BaseModel):
    """A named time series with labels and points."""

    model_config = ConfigDict(frozen=True)

    metric_name: str
    labels: dict[str, str] = Field(default_factory=dict)
    points: list[MetricPoint] = Field(default_factory=list)


class MetricWindow(BaseModel):
    """Metrics collected over a time window for a service."""

    model_config = ConfigDict(frozen=True)

    service: str
    start_time: datetime
    end_time: datetime
    series: list[MetricSeries] = Field(default_factory=list)
    p99_latency_ms: float | None = None
    error_rate: float | None = None
    request_count: int = 0


class DependencyEdge(BaseModel):
    """A directed dependency between two services."""

    model_config = ConfigDict(frozen=True)

    source: str
    target: str


class DependencyGraphSnapshot(BaseModel):
    """Snapshot of service dependency graph topology."""

    model_config = ConfigDict(frozen=True)

    nodes: list[str] = Field(default_factory=list)
    edges: list[DependencyEdge] = Field(default_factory=list)
    observed_at: datetime


class Alert(BaseModel):
    """Normalized incident trigger alert."""

    model_config = ConfigDict(frozen=True)

    alert_id: str
    source: Literal["pagerduty", "synthetic"]
    title: str
    service: str  # primary affected service name
    severity: Literal["critical", "error", "warning"]
    fired_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)


class ErrorSignature(BaseModel):
    """Normalized aggregated error signature."""

    model_config = ConfigDict(frozen=True)

    fingerprint: str  # stable hash of normalised message
    message: str
    service: str
    count: int
    first_seen: datetime
    last_seen: datetime


class DeployRef(BaseModel):
    """Reference to a deployed software artifact."""

    model_config = ConfigDict(frozen=True)

    commit_sha: str
    image_digests: dict[str, str]  # service name -> digest
    deployed_at: datetime
    pr_number: int | None = None
    contains_migration: bool = False


class IncidentContext(BaseModel):
    """Full diagnostic context gathered for an active incident."""

    model_config = ConfigDict(frozen=True)

    incident_id: str
    alert: Alert
    signatures: list[ErrorSignature] = Field(default_factory=list)
    metrics_window: MetricWindow
    recent_deploys: list[DeployRef] = Field(default_factory=list)
    dependency_graph: DependencyGraphSnapshot
    inferred_failure_class: FailureClass | None = None
    gathered_at: datetime
