"""Unit tests for services/loadgen modules to enforce 100% line and branch coverage."""

import os
import runpy
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services.loadgen.generator import RequestSpec, SeededRequestGenerator
from services.loadgen.main import main, parse_args
from services.loadgen.runner import (
    LoadgenConfig,
    LoadgenMetrics,
    RequestResult,
    _execute_single_request,
    calculate_percentile,
    run_loadgen,
)


def test_generator_determinism_and_batch() -> None:
    """Verify seeded generator produces identical batches given the same seed."""
    gen1 = SeededRequestGenerator(seed=42, auth_token="token-a")
    gen2 = SeededRequestGenerator(seed=42, auth_token="token-a")
    gen_other = SeededRequestGenerator(seed=999, auth_token="token-a")

    batch1 = gen1.generate_batch(50)
    batch2 = gen2.generate_batch(50)
    batch_other = gen_other.generate_batch(50)

    assert batch1 == batch2
    assert batch1 != batch_other

    # Verify presence of both GET and POST requests
    methods = {r.method for r in batch1}
    assert "GET" in methods
    assert "POST" in methods

    # Verify authorization header
    for r in batch1:
        assert r.headers["Authorization"] == "Bearer token-a"
        assert r.path == "/api/items"
        if r.method == "POST":
            assert r.json_body is not None
            assert "details" in r.json_body
        else:
            assert r.json_body is None


def test_calculate_percentile_edges() -> None:
    """Validate percentile calculations across empty, single, and multiple values."""
    assert calculate_percentile([], 99.0) == 0.0
    assert calculate_percentile([42.0], 50.0) == 42.0
    assert calculate_percentile([42.0], 99.0) == 42.0

    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert calculate_percentile(values, 0.0) == 10.0
    assert calculate_percentile(values, 50.0) == 30.0
    assert calculate_percentile(values, 100.0) == 50.0


@pytest.mark.asyncio
async def test_execute_single_request_outcomes() -> None:
    """Validate HTTP success, HTTP error, and connection exceptions."""
    time_cursor = [1.0]

    def fake_time() -> float:
        val = time_cursor[0]
        time_cursor[0] += 0.05  # 50ms latency
        return val

    # 1. Success 200
    mock_handler = httpx.MockTransport(lambda _req: httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient(transport=mock_handler) as client:
        spec = RequestSpec(method="GET", path="/api/items")
        res = await _execute_single_request(client, "http://test", spec, fake_time)
        assert res.success is True
        assert res.status_code == 200
        assert res.error is None
        assert res.latency_ms > 0

    # 2. HTTP 500 error
    mock_handler_500 = httpx.MockTransport(lambda _req: httpx.Response(500, text="Internal Error"))
    async with httpx.AsyncClient(transport=mock_handler_500) as client:
        spec = RequestSpec(method="POST", path="/api/items", json_body={"foo": "bar"})
        res = await _execute_single_request(client, "http://test", spec, fake_time)
        assert res.success is False
        assert res.status_code == 500
        assert res.error == "HTTP 500"

    # 3. Connection exception
    def raise_connect_error(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    mock_handler_exc = httpx.MockTransport(raise_connect_error)
    async with httpx.AsyncClient(transport=mock_handler_exc) as client:
        spec = RequestSpec(method="GET", path="/api/items")
        res = await _execute_single_request(client, "http://test", spec, fake_time)
        assert res.success is False
        assert res.status_code is None
        assert "Connection refused" in (res.error or "")


@pytest.mark.asyncio
async def test_run_loadgen_flow_and_pacing() -> None:
    """Validate run_loadgen with mock client, time pacing, and metrics."""
    time_state = {"current": 100.0}

    def fake_time() -> float:
        return time_state["current"]

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        time_state["current"] += seconds

    mock_handler = httpx.MockTransport(lambda _req: httpx.Response(200, json={"items": []}))
    async with httpx.AsyncClient(transport=mock_handler) as client:
        config = LoadgenConfig(
            rps=10.0,
            duration_seconds=1.0,
            target_url="http://mock-gw:8080",
            seed=42,
            auth_token="test-token",
        )
        metrics = await run_loadgen(
            config=config,
            client=client,
            time_fn=fake_time,
            sleep_fn=fake_sleep,
        )

        assert metrics.total_requests == 10
        assert metrics.successful_requests == 10
        assert metrics.failed_requests == 0
        assert metrics.error_rate == 0.0
        assert "error rate 0.00" in metrics.summary_line
        assert "p99 < 400ms" in metrics.summary_line
        assert len(sleeps) > 0


@pytest.mark.asyncio
async def test_run_loadgen_high_latency_threshold_string() -> None:
    """Validate p99 >= 400ms string formatting when latency exceeds 400ms."""
    time_state = {"current": 0.0}

    def high_latency_time() -> float:
        time_state["current"] += 0.5  # 500ms
        return time_state["current"]

    async def noop_sleep(_s: float) -> None:
        pass

    mock_handler = httpx.MockTransport(lambda _req: httpx.Response(200))
    async with httpx.AsyncClient(transport=mock_handler) as client:
        config = LoadgenConfig(rps=2.0, duration_seconds=1.0)
        metrics = await run_loadgen(
            config=config,
            client=client,
            time_fn=high_latency_time,
            sleep_fn=noop_sleep,
        )
        assert metrics.p99_ms >= 400.0
        assert "p99 >= 400ms" in metrics.summary_line


@pytest.mark.asyncio
async def test_run_loadgen_with_owned_client_and_zero_rps() -> None:
    """Verify run_loadgen initializes and closes its own client and handles zero rps."""
    with (
        patch("httpx.AsyncClient.aclose", new_callable=AsyncMock) as mock_aclose,
        patch(
            "services.loadgen.runner._execute_single_request", new_callable=AsyncMock
        ) as mock_exec,
    ):
        mock_exec.return_value = RequestResult(
            success=True, status_code=200, latency_ms=10.0, error=None
        )

        config = LoadgenConfig(rps=0.0, duration_seconds=0.0)
        metrics = await run_loadgen(config)
        assert metrics.total_requests == 1
        assert mock_aclose.called


@pytest.mark.asyncio
async def test_run_loadgen_with_failures() -> None:
    """Verify run_loadgen logs warning and counts failures when requests fail."""
    mock_handler = httpx.MockTransport(lambda _req: httpx.Response(500, text="Crash"))
    async with httpx.AsyncClient(transport=mock_handler) as client:
        config = LoadgenConfig(rps=5.0, duration_seconds=1.0)
        metrics = await run_loadgen(config=config, client=client)
        assert metrics.failed_requests == 5
        assert metrics.successful_requests == 0
        assert metrics.error_rate == 1.0
        assert "error rate 1.00" in metrics.summary_line


def test_main_arg_parsing_defaults_and_env() -> None:
    """Validate argument parsing with flags and environment variable fallbacks."""
    # 1. Defaults
    cfg = parse_args([])
    assert cfg.rps == 20.0
    assert cfg.duration_seconds == 30.0
    assert cfg.target_url == "http://localhost:8080"
    assert cfg.seed == 42
    assert cfg.auth_token == "valid-token"
    assert cfg.http_timeout_seconds == 10.0

    # 2. CLI flags
    args = [
        "--rps",
        "50",
        "--duration",
        "10",
        "--target-url",
        "http://cluster.local",
        "--seed",
        "101",
        "--auth-token",
        "custom-token",
        "--timeout",
        "2.5",
    ]
    cfg2 = parse_args(args)
    assert cfg2.rps == 50.0
    assert cfg2.duration_seconds == 10.0
    assert cfg2.target_url == "http://cluster.local"
    assert cfg2.seed == 101
    assert cfg2.auth_token == "custom-token"
    assert cfg2.http_timeout_seconds == 2.5

    # 3. Environment variables
    env_patch = {
        "LOADGEN_RPS": "15",
        "LOADGEN_DURATION": "25",
        "LOADGEN_TARGET_URL": "http://env-gw:8080",
        "LOADGEN_SEED": "777",
        "LOADGEN_AUTH_TOKEN": "env-token",
        "LOADGEN_TIMEOUT": "4.0",
    }
    with patch.dict(os.environ, env_patch):
        cfg3 = parse_args([])
        assert cfg3.rps == 15.0
        assert cfg3.duration_seconds == 25.0
        assert cfg3.target_url == "http://env-gw:8080"
        assert cfg3.seed == 777
        assert cfg3.auth_token == "env-token"
        assert cfg3.http_timeout_seconds == 4.0


def test_main_cli_execution(capsys: pytest.CaptureFixture[str]) -> None:
    """Validate main entrypoint invokes run_loadgen and outputs summary line."""
    fake_metrics = LoadgenMetrics(
        total_requests=10,
        successful_requests=10,
        failed_requests=0,
        error_rate=0.0,
        min_latency_ms=5.0,
        max_latency_ms=20.0,
        mean_latency_ms=10.0,
        p50_ms=10.0,
        p90_ms=18.0,
        p95_ms=19.0,
        p99_ms=20.0,
        summary_line="Summary: total=10, success=10, failed=0, error rate 0.00 (p99 < 400ms)",
    )
    with patch("services.loadgen.main.run_loadgen", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = fake_metrics
        exit_code = main(["--rps", "5", "--duration", "2"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Summary: total=10, success=10, failed=0, error rate 0.00" in captured.out


def test_main_module_run() -> None:
    """Validate execution of python -m services.loadgen entrypoint."""
    with (
        patch("services.loadgen.main.main", return_value=0) as mock_main,
        patch.object(sys, "exit") as mock_exit,
    ):
        runpy.run_module("services.loadgen", run_name="__main__")
        mock_main.assert_called_once()
        mock_exit.assert_called_once_with(0)
