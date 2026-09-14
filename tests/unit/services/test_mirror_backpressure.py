"""Unit tests for mirror-gateway backpressure behaviour and measurement (A3.6).

Verifies:
- Production latency p99 delta between baseline and dead twin is < 5 ms
- Dead twin bounded queue caps memory at maxsize and drops overflow requests
- Dead twin drop ratio rises toward 1.0
- No gateway errors on production requests when twins are unresponsive
- Healthy sibling twins continue receiving traffic (fault containment)
- Dispatching to twins remains non-blocking (< 1 ms) even when queues are saturated
- Clean worker cancellation and resource teardown under backpressure
"""

import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import status

from services.mirror_gateway.app import app, lifespan
from services.mirror_gateway.core import (
    MirroredRequest,
    MirrorGatewayManager,
)
from services.mirror_gateway.metrics import MirrorGatewayMetrics


def _percentile(values: list[float], p: float) -> float:
    """Calculate percentile from a sorted list of floats."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    rank = (p / 100.0) * (len(values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    weight = rank - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


@pytest.mark.asyncio
async def test_backpressure_p99_latency_and_drop_ratio() -> None:
    """A dead twin does not raise prod p99 latency by more than 5 ms, its drop ratio
    rises toward 1.0, and no gateway errors occur on the proxy path.
    """
    mock_http = AsyncMock(spec=httpx.AsyncClient)

    # Fast production responses (~1ms)
    async def fake_request(*_args: object, **kwargs: object) -> httpx.Response:
        url = str(kwargs.get("url", ""))
        if "ust-prod" in url:
            await asyncio.sleep(0.001)
            return httpx.Response(200, json={"status": "ok"})
        if "dead-twin" in url:
            # Dead twin hangs indefinitely (simulating unresponsive container / scaled to 0)
            await asyncio.sleep(10.0)
            return httpx.Response(200)
        # Healthy twin responds promptly
        return httpx.Response(200, json={"received": True})

    mock_http.request.side_effect = fake_request

    async with (
        lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        app.state.http_client = mock_http
        manager: MirrorGatewayManager = app.state.mirror_manager
        manager.client = mock_http

        # Warm-up ASGI client and router caches
        for _ in range(10):
            await client.get("/warmup")

        # ----------------------------------------------------------------------
        # Phase 1: Baseline measurement with healthy twin
        # ----------------------------------------------------------------------
        manager.register_twin("healthy-twin", "http://healthy-twin:8000")
        baseline_latencies: list[float] = []

        for i in range(100):
            t0 = time.perf_counter()
            resp = await client.get(f"/api/resource/{i}")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert resp.status_code == status.HTTP_200_OK
            baseline_latencies.append(elapsed_ms)

        baseline_sorted = sorted(baseline_latencies)
        baseline_p99 = _percentile(baseline_sorted, 99.0)
        manager.unregister_twin("healthy-twin")

        # ----------------------------------------------------------------------
        # Phase 2: Backpressure measurement with dead twin + sibling healthy twin
        # ----------------------------------------------------------------------
        # Use queue_maxsize=20 so the queue saturates quickly
        manager.queue_maxsize = 20
        dead_twin = manager.register_twin("dead-twin", "http://dead-twin:8000")
        manager.register_twin("healthy-sibling", "http://healthy-sibling:8000")

        # Let workers initialize and warm up ASGI path
        await asyncio.sleep(0.01)
        for _ in range(5):
            await client.get("/warmup")

        degraded_latencies: list[float] = []
        # Send 150 requests (well above queue_maxsize=20)
        for i in range(150):
            t0 = time.perf_counter()
            resp = await client.get(f"/api/resource/{i}")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert resp.status_code == status.HTTP_200_OK  # Zero gateway errors
            degraded_latencies.append(elapsed_ms)

        degraded_sorted = sorted(degraded_latencies)
        degraded_p99 = _percentile(degraded_sorted, 99.0)

        # 1. Backpressure assertion: prod p99 delta < 5 ms
        p99_delta = abs(degraded_p99 - baseline_p99)
        assert p99_delta < 5.0, (
            f"Production p99 delta exceeded 5ms: baseline={baseline_p99:.2f}ms, "
            f"degraded={degraded_p99:.2f}ms, delta={p99_delta:.2f}ms"
        )

        # 2. Dead twin queue bound: queue size cannot exceed maxsize
        assert dead_twin.queue.qsize() <= manager.queue_maxsize

        # 3. Dead twin drop ratio rises toward 1.0
        dead_stats = manager.get_stats("dead-twin")
        assert dead_stats is not None
        assert dead_stats.dropped >= 120  # Majority of 150 requests were dropped
        assert dead_stats.drop_ratio > 0.80, (
            f"Expected drop_ratio -> 1.0, got {dead_stats.drop_ratio}"
        )

        # 4. Sibling healthy twin isolation: sibling continues receiving traffic
        sibling_stats = manager.get_stats("healthy-sibling")
        assert sibling_stats is not None
        # Drain sibling queue
        await asyncio.sleep(0.05)
        assert sibling_stats.delivered > 0
        assert sibling_stats.drop_ratio < 0.05

        manager.unregister_twin("dead-twin")
        manager.unregister_twin("healthy-sibling")


@pytest.mark.asyncio
async def test_dispatch_to_twins_non_blocking_performance() -> None:
    """dispatch_to_twins takes < 1 ms even when queues are completely full."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)

    async def slow_mock(*_a: object, **_kw: object) -> None:
        await asyncio.sleep(1.0)

    mock_http.request.side_effect = slow_mock

    manager = MirrorGatewayManager(client=mock_http, queue_maxsize=5)
    twin = manager.register_twin("saturated-twin", "http://saturated:8000")

    req = MirroredRequest(
        method="GET",
        path="/test",
        query="",
        headers={"accept": "application/json"},
        body=b"payload",
    )

    # Fill queue to capacity
    for _ in range(10):
        manager.dispatch_to_twins(req)

    assert twin.queue.qsize() == 5
    assert twin.dropped == 5

    # Measure dispatch execution time on saturated queue
    dispatch_times: list[float] = []
    for _ in range(100):
        t0 = time.perf_counter()
        manager.dispatch_to_twins(req)
        dispatch_times.append((time.perf_counter() - t0) * 1000.0)

    max_dispatch_ms = max(dispatch_times)
    mean_dispatch_ms = sum(dispatch_times) / len(dispatch_times)

    assert max_dispatch_ms < 1.0, f"Max dispatch time {max_dispatch_ms:.3f}ms exceeded 1.0ms"
    assert mean_dispatch_ms < 0.2, f"Mean dispatch time {mean_dispatch_ms:.3f}ms exceeded 0.2ms"

    await manager.close()


@pytest.mark.asyncio
async def test_dead_twin_worker_timeout_increments_drop() -> None:
    """When a twin connection times out after worker_timeout_seconds, the worker increments
    dropped counter and observes latency without crashing.
    """
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.request.side_effect = httpx.TimeoutException("Connection timed out")

    metrics = MirrorGatewayMetrics()
    manager = MirrorGatewayManager(
        client=mock_http,
        queue_maxsize=10,
        worker_timeout_seconds=0.01,
        metrics=metrics,
    )

    twin = manager.register_twin("timeout-twin", "http://timeout:8000")

    req = MirroredRequest(method="POST", path="/data", query="", headers={}, body=b"test")
    manager.dispatch_to_twins(req)

    # Wait for drain worker to handle timeout
    for _ in range(20):
        if twin.dropped >= 1:
            break
        await asyncio.sleep(0.01)

    assert twin.dropped == 1
    assert twin.delivered == 0
    stats = manager.get_stats("timeout-twin")
    assert stats is not None
    assert stats.drop_ratio == 1.0

    await manager.close()


@pytest.mark.asyncio
async def test_queue_overflow_log_dampening(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queue overflow warning is logged for initial drops and then dampened periodically."""
    from services.mirror_gateway import core

    logged_events: list[dict[str, object]] = []

    def mock_warning(event: str, **kwargs: object) -> None:
        if event == "mirror_queue_overflow":
            logged_events.append(kwargs)

    monkeypatch.setattr(core.logger, "warning", mock_warning)

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    manager = MirrorGatewayManager(client=mock_http, queue_maxsize=1)
    twin = manager.register_twin("dampen-twin", "http://dampen:8000")

    # Cancel worker so queue stays full
    if twin.worker_task:
        twin.worker_task.cancel()

    req = MirroredRequest(method="GET", path="/item", query="", headers={}, body=b"")

    # 1. Fill queue (size 1)
    manager.dispatch_to_twins(req)
    assert twin.queue.qsize() == 1
    assert len(logged_events) == 0

    # 2. Trigger drops 1 through 105
    for _ in range(105):
        manager.dispatch_to_twins(req)

    assert twin.dropped == 105

    # Drops 1, 2, 3, 4, 5 are logged (dropped <= 5)
    # Drops 6-99 are skipped
    # Drop 100 is logged (100 % 100 == 0)
    # Drops 101-105 are skipped
    logged_drop_counts = [e["dropped"] for e in logged_events]
    assert logged_drop_counts == [1, 2, 3, 4, 5, 100]

    await manager.close()


def test_cli_mirror_compare_fidelity_assertion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify ust mirror compare prints the fidelity assertion line when all twins meet
    the criteria (within 2% and drop_ratio < 0.05).
    """
    from typer.testing import CliRunner

    from understudy.cli import app
    from understudy.contracts.twin import MirrorStats
    from understudy.mirror.registry import HttpMirrorRegistry

    runner = CliRunner()

    async def _mock_perfect_stats(_self: object) -> dict[str, MirrorStats]:
        return {
            "twin_inc_perf_0": MirrorStats(twin_id="twin_inc_perf_0", delivered=3000, dropped=5),
            "twin_inc_perf_1": MirrorStats(twin_id="twin_inc_perf_1", delivered=2980, dropped=10),
            "twin_inc_perf_2": MirrorStats(twin_id="twin_inc_perf_2", delivered=2990, dropped=8),
        }

    monkeypatch.setattr(HttpMirrorRegistry, "get_all_stats", _mock_perfect_stats)

    res = runner.invoke(app, ["mirror", "compare", "--incident", "inc_perf"])
    assert res.exit_code == 0
    assert "twin_inc_perf_0" in res.stdout
    assert "twin_inc_perf_1" in res.stdout
    assert "twin_inc_perf_2" in res.stdout
    assert "OK" in res.stdout
    assert (
        "Fidelity check: per-twin request count within 2% of prod, path distribution identical."
        in res.stdout
    )
