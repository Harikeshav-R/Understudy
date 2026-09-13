"""Prometheus metrics instrumentation for the mirror gateway service.

Conforms to ADR-012, ADR-013, and build-plan step A3.3:
- understudy_mirror_delivered_total{twin_id}: counter of mirrored requests delivered to twins
- understudy_mirror_dropped_total{twin_id}: counter of mirrored requests dropped for twins
- understudy_mirror_latency_seconds{target}: histogram of request latency in seconds for
  targets ('prod' for synchronous proxy, twin_id for mirrored fan-out).
"""

from prometheus_client import CollectorRegistry, Counter, Histogram

from services._common.settings import get_services_settings


class MirrorGatewayMetrics:
    """Prometheus metrics collector for mirror gateway proxy and twin fan-out."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()

        self.delivered_total = Counter(
            "understudy_mirror_delivered_total",
            "Total number of mirrored requests successfully delivered to twins",
            ["twin_id"],
            registry=self.registry,
        )
        self.dropped_total = Counter(
            "understudy_mirror_dropped_total",
            "Total number of mirrored requests dropped for twins",
            ["twin_id"],
            registry=self.registry,
        )
        self.latency_seconds = Histogram(
            "understudy_mirror_latency_seconds",
            "Latency of proxied production requests and mirrored twin requests in seconds",
            ["target"],
            buckets=get_services_settings().metrics.histogram_buckets,
            registry=self.registry,
        )

    def init_twin(self, twin_id: str) -> None:
        """Initialize counters with 0.0 value for a newly registered twin."""
        self.delivered_total.labels(twin_id=twin_id)
        self.dropped_total.labels(twin_id=twin_id)

    def record_delivered(self, twin_id: str) -> None:
        """Increment delivered counter for a twin."""
        self.delivered_total.labels(twin_id=twin_id).inc()

    def record_dropped(self, twin_id: str) -> None:
        """Increment dropped counter for a twin."""
        self.dropped_total.labels(twin_id=twin_id).inc()

    def record_latency(self, target: str, duration: float) -> None:
        """Observe request duration in seconds for target ('prod' or twin_id)."""
        self.latency_seconds.labels(target=target).observe(duration)
