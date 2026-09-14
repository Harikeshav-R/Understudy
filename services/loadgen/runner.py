"""Load generation execution engine and metrics collector."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from services._common.logging import get_logger
from services._common.settings import load_services_settings
from services.loadgen.generator import RequestSpec, SeededRequestGenerator

logger = get_logger("loadgen")


@dataclass(frozen=True)
class LoadgenConfig:
    """Configuration options for load generation run. Defaults come from
    load_services_settings().loadgen so a CLI run with no flags matches the same
    tunable the settings docs describe (see services/loadgen/main.py, which sources
    its own flag defaults from the same settings object)."""

    rps: float = field(default_factory=lambda: load_services_settings().loadgen.rps)
    duration_seconds: float = field(
        default_factory=lambda: load_services_settings().loadgen.duration_seconds
    )
    target_url: str = field(default_factory=lambda: load_services_settings().loadgen.target_url)
    seed: int = field(default_factory=lambda: load_services_settings().loadgen.seed)
    auth_token: str = field(default_factory=lambda: load_services_settings().loadgen.auth_token)
    http_timeout_seconds: float = field(
        default_factory=lambda: load_services_settings().loadgen.http_timeout_seconds
    )


@dataclass(frozen=True)
class RequestResult:
    """Result of an individual HTTP request."""

    success: bool
    status_code: int | None
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True)
class LoadgenMetrics:
    """Aggregated metrics collected from a load generation run."""

    total_requests: int
    successful_requests: int
    failed_requests: int
    error_rate: float
    min_latency_ms: float
    max_latency_ms: float
    mean_latency_ms: float
    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float
    summary_line: str


def calculate_percentile(sorted_values: list[float], percentile: float) -> float:
    """Calculate percentile from a sorted list of floats using linear interpolation."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]

    rank = (percentile / 100.0) * (len(sorted_values) - 1)
    lower_idx = int(rank)
    upper_idx = min(lower_idx + 1, len(sorted_values) - 1)
    weight = rank - lower_idx
    return sorted_values[lower_idx] * (1.0 - weight) + sorted_values[upper_idx] * weight


async def _execute_single_request(
    client: httpx.AsyncClient,
    base_url: str,
    spec: RequestSpec,
    time_fn: Callable[[], float],
) -> RequestResult:
    """Execute a single HTTP request and record its outcome and latency."""
    url = f"{base_url.rstrip('/')}{spec.path}"
    start = time_fn()
    try:
        response = await client.request(
            method=spec.method,
            url=url,
            headers=spec.headers,
            json=spec.json_body,
        )
        latency_ms = (time_fn() - start) * 1000.0
        success = 200 <= response.status_code < 400
        return RequestResult(
            success=success,
            status_code=response.status_code,
            latency_ms=latency_ms,
            error=None if success else f"HTTP {response.status_code}",
        )
    except httpx.RequestError as exc:
        latency_ms = (time_fn() - start) * 1000.0
        return RequestResult(
            success=False,
            status_code=None,
            latency_ms=latency_ms,
            error=str(exc),
        )


async def run_loadgen(
    config: LoadgenConfig,
    client: httpx.AsyncClient | None = None,
    time_fn: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], Awaitable[None]] | None = None,
) -> LoadgenMetrics:
    """Run deterministic load generation according to config and compute metrics."""
    time_provider = time_fn or time.perf_counter
    sleep_provider = sleep_fn or asyncio.sleep

    generator = SeededRequestGenerator(seed=config.seed, auth_token=config.auth_token)
    total_requests = max(1, int(config.rps * config.duration_seconds))
    specs = generator.generate_batch(total_requests)

    interval = 1.0 / config.rps if config.rps > 0 else 0.0

    loadgen_settings = load_services_settings().loadgen
    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(config.http_timeout_seconds),
        limits=httpx.Limits(
            max_connections=loadgen_settings.max_connections,
            max_keepalive_connections=loadgen_settings.max_keepalive_connections,
        ),
    )

    logger.info(
        "loadgen_starting",
        rps=config.rps,
        duration=config.duration_seconds,
        target_url=config.target_url,
        total_requests=total_requests,
        seed=config.seed,
    )

    results: list[RequestResult] = []
    tasks: list[asyncio.Task[RequestResult]] = []

    start_time = time_provider()
    try:
        for idx, spec in enumerate(specs):
            target_dispatch_time = start_time + (idx * interval)
            now = time_provider()
            delay = target_dispatch_time - now
            if delay > 0:
                await sleep_provider(delay)

            task = asyncio.create_task(
                _execute_single_request(http_client, config.target_url, spec, time_provider)
            )
            tasks.append(task)

        # Wait for all in-flight requests to complete
        results = await asyncio.gather(*tasks)
    finally:
        if owns_client:
            await http_client.aclose()

    # Aggregate metrics
    successful = sum(1 for r in results if r.success)
    failed = len(results) - successful
    error_rate = failed / len(results) if results else 0.0

    latencies = sorted(r.latency_ms for r in results)
    min_lat = latencies[0] if latencies else 0.0
    max_lat = latencies[-1] if latencies else 0.0
    mean_lat = sum(latencies) / len(latencies) if latencies else 0.0

    p50 = calculate_percentile(latencies, 50.0)
    p90 = calculate_percentile(latencies, 90.0)
    p95 = calculate_percentile(latencies, 95.0)
    p99 = calculate_percentile(latencies, 99.0)

    p99_sla_ms = loadgen_settings.p99_sla_ms
    p99_str = (
        f"p99 < {p99_sla_ms:.0f}ms ({p99:.1f}ms)"
        if p99 < p99_sla_ms
        else f"p99 >= {p99_sla_ms:.0f}ms ({p99:.1f}ms)"
    )
    summary_line = (
        f"Summary: total={len(results)}, success={successful}, failed={failed}, "
        f"error rate {error_rate:.2f}, p50={p50:.1f}ms, p90={p90:.1f}ms, "
        f"p95={p95:.1f}ms, p99={p99:.1f}ms, max={max_lat:.1f}ms [{p99_str}]"
    )

    if failed > 0:
        logger.warning(
            "loadgen_failures_detected",
            failed_count=failed,
            distinct_errors=list({r.error for r in results if r.error}),
        )

    logger.info(
        "loadgen_completed",
        total=len(results),
        success=successful,
        failed=failed,
        error_rate=error_rate,
        p50=p50,
        p99=p99,
        summary=summary_line,
    )

    return LoadgenMetrics(
        total_requests=len(results),
        successful_requests=successful,
        failed_requests=failed,
        error_rate=error_rate,
        min_latency_ms=min_lat,
        max_latency_ms=max_lat,
        mean_latency_ms=mean_lat,
        p50_ms=p50,
        p90_ms=p90,
        p95_ms=p95,
        p99_ms=p99,
        summary_line=summary_line,
    )
