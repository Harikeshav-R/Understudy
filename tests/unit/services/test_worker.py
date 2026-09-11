"""Unit tests for worker background job polling, stall faults, and job queue."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import psycopg
import pytest

import services.worker.app as worker_module
from services._common.faults import FaultKind, FaultRequest
from services.worker.app import (
    _worker_iteration,
    app,
    check_db_readiness,
    fault_manager,
    init_worker_db,
    process_one_job,
    worker_loop,
)


@pytest.mark.asyncio
async def test_process_one_job_in_memory() -> None:
    worker_module.IN_MEMORY_JOBS = [
        {"id": 1, "payload": "task1", "status": "pending"},
        {"id": 2, "payload": "task2", "status": "completed"},
    ]
    # 1. Claims pending job
    assert await process_one_job(None) is True
    assert worker_module.IN_MEMORY_JOBS[0]["status"] == "completed"

    # 2. No pending jobs remain
    assert await process_one_job(None) is False


@pytest.mark.asyncio
async def test_init_worker_db() -> None:
    mock_cursor = AsyncMock()
    mock_conn = MagicMock()
    mock_conn.commit = AsyncMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    await init_worker_db(mock_pool)
    assert mock_cursor.execute.await_count == 1
    assert mock_conn.commit.await_count == 1


@pytest.mark.asyncio
async def test_process_one_job_db() -> None:
    mock_cursor = AsyncMock()
    mock_conn = MagicMock()
    mock_conn.commit = AsyncMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    # 1. Job found
    mock_cursor.fetchone.return_value = (101,)
    assert await process_one_job(mock_pool) is True
    assert mock_cursor.execute.await_count == 2
    assert mock_conn.commit.await_count == 1

    # 2. No job found
    mock_cursor.reset_mock()
    mock_cursor.fetchone.return_value = None
    assert await process_one_job(mock_pool) is False

    # 3. DB Error
    mock_pool.connection.side_effect = psycopg.OperationalError("conn lost")
    assert await process_one_job(mock_pool) is False


@pytest.mark.asyncio
async def test_worker_loop_iteration_and_cancel() -> None:
    # Run loop for a split second then cancel
    task = asyncio.create_task(worker_loop())
    await asyncio.sleep(0.02)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_worker_loop_handles_cancellation_only() -> None:
    """worker_loop only special-cases CancelledError; process_one_job is responsible for
    never raising for expected (psycopg) failures -- see test_process_one_job_db."""
    with patch(
        "services.worker.app.process_one_job",
        side_effect=asyncio.CancelledError(),
    ):
        await worker_loop()


@pytest.mark.asyncio
async def test_worker_loop_propagates_unexpected_errors() -> None:
    """An exception that is not psycopg.Error or CancelledError is a bug, not an expected
    failure mode, and must not be silently swallowed (CLAUDE.md §5.4)."""
    with (
        patch(
            "services.worker.app.process_one_job",
            side_effect=RuntimeError("bug"),
        ),
        pytest.raises(RuntimeError),
    ):
        await worker_loop()


@pytest.mark.asyncio
async def test_worker_iteration_applies_error_rate_fault() -> None:
    """Previously worker_loop never called pre_request_hook, so ERROR_RATE (and
    LATENCY/CPU_SPIN) faults were no-ops against job processing -- only STALL worked, via
    the separate wait_if_stalled call. A magnitude of 1.0 always triggers (see
    faults.py's fraction/percentage convention), so the job must be skipped, not
    processed, on every iteration while the fault is active."""
    worker_module.IN_MEMORY_JOBS = [{"id": 1, "payload": "task", "status": "pending"}]
    await fault_manager.apply_fault(
        FaultRequest(kind=FaultKind.ERROR_RATE, magnitude=1.0, ttl_seconds=10)
    )
    try:
        with patch("services.worker.app.process_one_job") as mock_process:
            await _worker_iteration()
            mock_process.assert_not_called()
        assert worker_module.IN_MEMORY_JOBS[0]["status"] == "pending"
    finally:
        await fault_manager.clear_fault()


@pytest.mark.asyncio
async def test_worker_iteration_no_fault_processes_job() -> None:
    """With no active fault, pre_request_hook is a no-op and the job is processed exactly
    as before this fault-injection gap was fixed."""
    worker_module.IN_MEMORY_JOBS = [{"id": 1, "payload": "task", "status": "pending"}]
    await _worker_iteration()
    assert worker_module.IN_MEMORY_JOBS[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_check_db_readiness_no_pool() -> None:
    worker_module.db_pool = None
    assert await check_db_readiness() is True


@pytest.mark.asyncio
async def test_check_db_readiness_ping_fails() -> None:
    mock_pool = AsyncMock()
    worker_module.db_pool = mock_pool
    with patch("services.worker.app.ping_db", return_value=False):
        assert await check_db_readiness() is False
    worker_module.db_pool = None


def _mock_pool_with_regclass(regclass_value: object) -> MagicMock:
    mock_cursor = AsyncMock()
    mock_cursor.fetchone.return_value = (regclass_value,)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
    mock_pool = MagicMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn
    return mock_pool


@pytest.mark.asyncio
async def test_check_db_readiness_jobs_table_present() -> None:
    mock_pool = _mock_pool_with_regclass("jobs")
    worker_module.db_pool = mock_pool
    with patch("services.worker.app.ping_db", return_value=True):
        assert await check_db_readiness() is True
    worker_module.db_pool = None


@pytest.mark.asyncio
async def test_check_db_readiness_jobs_table_missing() -> None:
    """`/readyz` must not report ready if init_worker_db never created the table, even
    though a plain connectivity check (SELECT 1) would still succeed."""
    mock_pool = _mock_pool_with_regclass(None)
    worker_module.db_pool = mock_pool
    with patch("services.worker.app.ping_db", return_value=True):
        assert await check_db_readiness() is False
    worker_module.db_pool = None


@pytest.mark.asyncio
async def test_check_db_readiness_query_error() -> None:
    mock_pool = MagicMock()
    mock_pool.connection.side_effect = psycopg.OperationalError("conn lost")
    worker_module.db_pool = mock_pool
    with patch("services.worker.app.ping_db", return_value=True):
        assert await check_db_readiness() is False
    worker_module.db_pool = None


@pytest.mark.asyncio
async def test_list_jobs_endpoints() -> None:
    worker_module.db_pool = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/jobs")
        assert resp.status_code == 200
        assert "jobs" in resp.json()
        assert resp.json()["is_stalled"] is False

    # With DB pool
    mock_cursor = AsyncMock()
    mock_cursor.fetchall.return_value = [(1, "payload1", "pending")]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    worker_module.db_pool = mock_pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp_db = await client.get("/jobs")
        assert resp_db.status_code == 200
        assert resp_db.json()["jobs"][0]["id"] == 1
    worker_module.db_pool = None


@pytest.mark.asyncio
async def test_worker_lifespan_no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "DATABASE_URL", None)
    worker_module.db_pool = None
    async with worker_module.lifespan(app):
        pass
    assert worker_module.db_pool is None


@pytest.mark.asyncio
async def test_worker_lifespan_with_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "DATABASE_URL", "postgresql://fake:5432/db")
    mock_pool = AsyncMock()
    with (
        patch("services._common.bootstrap.create_pool", return_value=mock_pool),
        patch(
            "services.worker.app.init_worker_db",
            side_effect=psycopg.OperationalError("init failed"),
        ),
    ):
        async with worker_module.lifespan(app):
            assert mock_pool.open.await_count == 1
        assert mock_pool.close.await_count == 1
    worker_module.db_pool = None
