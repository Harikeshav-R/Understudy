"""Prometheus metrics middleware and endpoint setup for demo services."""

import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    Info,
    generate_latest,
)

from services._common.role_guard import get_service_role
from services._common.settings import get_services_settings


def setup_metrics(
    app: FastAPI, service_name: str, registry: CollectorRegistry | None = None
) -> CollectorRegistry:
    """Attach metrics middleware and /metrics scraping route to the FastAPI application.

    Builds its own CollectorRegistry (and Counter/Histogram/Info instances against it)
    per call rather than sharing process-wide globals. Every service's app.py calls this
    once at import time, and every service test file imports its app module into the
    same pytest process -- module-level globals here would mean whichever service (or
    test) called setup_metrics last determines what /metrics reports for the rest of the
    run, and counters/histograms would accumulate across unrelated services' tests.
    `registry` is exposed so tests can pass their own to assert in isolation; production
    call sites omit it and get a fresh one.
    """
    metrics_registry = registry if registry is not None else CollectorRegistry()
    app.state.metrics_registry = metrics_registry

    request_count = Counter(
        "http_requests_total",
        "Total number of HTTP requests processed",
        ["service", "method", "endpoint", "status_code"],
        registry=metrics_registry,
    )
    request_duration = Histogram(
        "http_request_duration_seconds",
        "HTTP request latency in seconds",
        ["service", "method", "endpoint"],
        buckets=get_services_settings().metrics.histogram_buckets,
        registry=metrics_registry,
    )
    service_info = Info(
        "understudy_service",
        "Understudy demo service information",
        registry=metrics_registry,
    )
    service_info.info({"service": service_name, "role": get_service_role()})

    @app.middleware("http")
    async def metrics_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Don't record metrics for the metrics scrape itself to prevent loop inflation
        if request.url.path == "/metrics":
            return await call_next(request)

        start_time = time.monotonic()
        method = request.method
        path = request.url.path

        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration = time.monotonic() - start_time
            request_count.labels(
                service=service_name,
                method=method,
                endpoint=path,
                status_code=str(status_code),
            ).inc()
            request_duration.labels(
                service=service_name,
                method=method,
                endpoint=path,
            ).observe(duration)

    @app.get("/metrics", tags=["Observability"])
    async def metrics() -> Response:
        return Response(content=generate_latest(metrics_registry), media_type=CONTENT_TYPE_LATEST)

    return metrics_registry
