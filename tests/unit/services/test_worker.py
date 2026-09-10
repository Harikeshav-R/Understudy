"""Unit tests for worker background job polling, stall faults, and job queue."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import services.worker.app as worker_module
from services.worker.app import (
    app,
    check_db_readiness,
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
    mock_pool.connection.side_effect = ConnectionResetError("conn lost")
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
async def test_worker_loop_handles_exception() -> None:
    with patch(
        "services.worker.app.process_one_job",
        side_effect=[RuntimeError("transient"), asyncio.CancelledError()],
    ):
        # Should catch RuntimeError and continue, then exit cleanly on CancelledError
        await worker_loop()


@pytest.mark.asyncio
async def test_check_db_readiness() -> None:
    worker_module.db_pool = None
    assert await check_db_readiness() is True

    mock_pool = AsyncMock()
    worker_module.db_pool = mock_pool
    with patch("services.worker.app.ping_db", return_value=True):
        assert await check_db_readiness() is True
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
        patch("services.worker.app.create_pool", return_value=mock_pool),
        patch("services.worker.app.init_worker_db", side_effect=RuntimeError("init failed")),
    ):
        async with worker_module.lifespan(app):
            assert mock_pool.open.await_count == 1
        assert mock_pool.close.await_count == 1
    worker_module.db_pool = None
