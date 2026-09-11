"""Prometheus metrics middleware and endpoint setup for demo services."""

import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    Info,
    generate_latest,
)

from services._common.role_guard import get_service_role

# Metrics definitions
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total number of HTTP requests processed",
    ["service", "method", "endpoint", "status_code"],
)

REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["service", "method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.4, 0.5, 1.0, 2.0, 5.0),
)

SERVICE_INFO = Info(
    "understudy_service",
    "Understudy demo service information",
)


def setup_metrics(app: FastAPI, service_name: str) -> None:
    """Attach metrics middleware and /metrics scraping route to the FastAPI application."""
    SERVICE_INFO.info({"service": service_name, "role": get_service_role()})

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
            REQUEST_COUNT.labels(
                service=service_name,
                method=method,
                endpoint=path,
                status_code=str(status_code),
            ).inc()
            REQUEST_DURATION.labels(
                service=service_name,
                method=method,
                endpoint=path,
            ).observe(duration)

    @app.get("/metrics", tags=["Observability"])
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
