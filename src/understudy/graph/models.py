"""Data models for dependency graph declarations, observed traffic, and cross-checking."""

from pydantic import BaseModel, ConfigDict, Field


class ServiceManifestEntry(BaseModel):
    """Declared service metadata in dependencies.yaml."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)


class DependencyEdgeDecl(BaseModel):
    """Declared dependency edge between two services."""

    model_config = ConfigDict(frozen=True)

    source: str
    target: str


class ServiceManifest(BaseModel):
    """Parsed and validated schema for dependencies.yaml."""

    model_config = ConfigDict(frozen=True)

    version: str
    services: list[ServiceManifestEntry]
    edges: list[DependencyEdgeDecl]


class ObservedTraffic(BaseModel):
    """Aggregated traffic metrics and call patterns observed from telemetry."""

    model_config = ConfigDict(frozen=True)

    services: set[str] = Field(default_factory=set)
    edges: set[tuple[str, str]] = Field(default_factory=set)
    request_counts: dict[str, float] = Field(default_factory=dict)
    total_requests: float = 0.0


class CrossCheckReport(BaseModel):
    """Comparison report between declared topology and observed traffic."""

    model_config = ConfigDict(frozen=True)

    status: str  # "OK" or "MISMATCH"
    declared_services: set[str]
    observed_services: set[str]
    declared_edges: set[tuple[str, str]]
    observed_edges: set[tuple[str, str]]
    undeclared_services: set[str]
    undeclared_edges: set[tuple[str, str]]
    missing_services: set[str] = Field(default_factory=set)
    missing_edges: set[tuple[str, str]] = Field(default_factory=set)
    message: str
