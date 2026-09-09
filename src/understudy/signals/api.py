"""Signals component protocol interfaces."""

from datetime import datetime
from typing import Protocol, runtime_checkable

from understudy.contracts.incident import (
    Alert,
    DeployRef,
    ErrorSignature,
    MetricWindow,
)


@runtime_checkable
class AlertSource(Protocol):
    """Source of incoming incident triggers."""

    async def receive_alert(self) -> Alert:
        """Wait for and return an incoming alert."""
        raise NotImplementedError

    async def inject_synthetic_alert(self, alert: Alert) -> None:
        """Inject an alert into the source queue."""
        raise NotImplementedError


@runtime_checkable
class ObservabilityAdapter(Protocol):
    """Adapter for metrics, error logs, and service health queries."""

    async def metric_window(self, service: str, since: datetime) -> MetricWindow:
        """Fetch telemetry metric window for a service since a given timestamp."""
        raise NotImplementedError

    async def error_signatures(self, service: str, since: datetime) -> list[ErrorSignature]:
        """Aggregate log error signatures for a service since a given timestamp."""
        raise NotImplementedError

    async def service_health(self, namespace: str, service: str) -> bool:
        """Check whether a service in a namespace is currently healthy."""
        raise NotImplementedError


@runtime_checkable
class DeployHistory(Protocol):
    """Interface for querying repository deploy history and commit digests."""

    async def recent_deploys(self, limit: int = 5) -> list[DeployRef]:
        """Fetch recent deployment metadata from version control."""
        raise NotImplementedError
