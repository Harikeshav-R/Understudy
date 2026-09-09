"""Deterministic in-memory fake signals implementations."""

from datetime import UTC, datetime

from understudy.contracts.incident import (
    Alert,
    DeployRef,
    ErrorSignature,
    MetricPoint,
    MetricSeries,
    MetricWindow,
)
from understudy.signals.api import AlertSource, DeployHistory, ObservabilityAdapter


class FakeAlertSource(AlertSource):
    """Deterministic alert source with injectable alert queue."""

    def __init__(self, alerts: list[Alert] | None = None) -> None:
        self._alerts: list[Alert] = list(alerts or [])
        self._default_alert = Alert(
            alert_id="alt_fake_001",
            source="synthetic",
            title="Synthetic p99 latency breach",
            service="edge-gateway",
            severity="critical",
            fired_at=datetime.now(UTC),
            raw={"details": "p99 > 400ms"},
        )

    async def receive_alert(self) -> Alert:
        """Return next alert from queue, or default alert if empty."""
        if self._alerts:
            return self._alerts.pop(0)
        return self._default_alert

    async def inject_synthetic_alert(self, alert: Alert) -> None:
        """Enqueue a synthetic alert."""
        self._alerts.append(alert)


class FakeObservabilityAdapter(ObservabilityAdapter):
    """Deterministic telemetry adapter returning reproducible metrics and logs."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed

    async def metric_window(self, service: str, since: datetime) -> MetricWindow:
        """Return deterministic metrics window."""
        now = datetime.now(UTC)
        point = MetricPoint(timestamp=now, value=120.0 + (self.seed % 10))
        series = MetricSeries(
            metric_name="http_latency_ms",
            labels={"service": service},
            points=[point],
        )
        return MetricWindow(
            service=service,
            start_time=since,
            end_time=now,
            series=[series],
            p99_latency_ms=120.0,
            error_rate=0.01,
            request_count=1000,
        )

    async def error_signatures(self, service: str, since: datetime) -> list[ErrorSignature]:
        """Return deterministic error signatures."""
        now = datetime.now(UTC)
        return [
            ErrorSignature(
                fingerprint="fp_sig_001",
                message="Downstream connection timeout",
                service=service,
                count=12,
                first_seen=since,
                last_seen=now,
            )
        ]

    async def service_health(self, namespace: str, service: str) -> bool:
        """Check whether service is healthy."""
        _ = (namespace, service)
        return True


class FakeDeployHistory(DeployHistory):
    """Deterministic deploy history from version control."""

    def __init__(self, deploys: list[DeployRef] | None = None) -> None:
        now = datetime.now(UTC)
        self._deploys: list[DeployRef] = list(
            deploys
            or [
                DeployRef(
                    commit_sha="c0ffee1",
                    image_digests={"edge-gateway": "sha256:edge1", "data-service": "sha256:data1"},
                    deployed_at=now,
                    pr_number=101,
                    contains_migration=False,
                ),
                DeployRef(
                    commit_sha="c0ffee0",
                    image_digests={"edge-gateway": "sha256:edge0", "data-service": "sha256:data0"},
                    deployed_at=now,
                    pr_number=100,
                    contains_migration=True,
                ),
            ]
        )

    async def recent_deploys(self, limit: int = 5) -> list[DeployRef]:
        """Return recent deploys up to limit."""
        return self._deploys[:limit]
