"""Worker demo service: background job consumer using FOR UPDATE SKIP LOCKED.

Supports stall and crash faults.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from psycopg import Error as PsycopgError
from psycopg_pool import AsyncConnectionPool

from services._common import (
    FaultManager,
    create_service_app,
    db_pool_lifespan,
    get_logger,
    ping_db,
    setup_health_routes,
)
from services._common.settings import get_services_settings

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
_settings = get_services_settings().worker
POLL_INTERVAL_SECONDS = _settings.poll_interval_seconds
JOB_LIST_LIMIT = _settings.job_list_limit

db_pool: AsyncConnectionPool | None = None
fault_manager = FaultManager()

# In-memory queue for offline test runs
IN_MEMORY_JOBS: list[dict[str, Any]] = [
    {"id": 1, "payload": "reindex_catalogue", "status": "pending"},
    {"id": 2, "payload": "sync_inventory", "status": "pending"},
]


async def init_worker_db(pool: AsyncConnectionPool) -> None:
    """Initialize jobs table if running against a database."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
                CREATE TABLE IF NOT EXISTS jobs (
                    id SERIAL PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    processed_at TIMESTAMPTZ
                );
                """
        )
        await conn.commit()


async def process_one_job(pool: AsyncConnectionPool | None) -> bool:
    """Acquire and complete a single pending job using FOR UPDATE SKIP LOCKED."""
    if pool is None:
        for job in IN_MEMORY_JOBS:
            if job["status"] == "pending":
                job["status"] = "completed"
                return True
        return False

    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                    SELECT id FROM jobs
                    WHERE status = 'pending'
                    ORDER BY id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
            )
            row = await cur.fetchone()
            if not row:
                return False
            job_id = row[0]
            await cur.execute(
                """
                    UPDATE jobs
                    SET status = 'completed', processed_at = NOW()
                    WHERE id = %s
                    """,
                (job_id,),
            )
            await conn.commit()
            return True
    except PsycopgError as exc:
        logger.warning("job_processing_failed", error=str(exc))
        return False


async def _worker_iteration() -> None:
    """Run one poll-and-process cycle, honoring whatever fault is currently active.

    Factored out of worker_loop so it can be exercised directly under test without
    driving a real polling loop. pre_request_hook's ERROR_RATE branch raises
    HTTPException, which is meaningful in an HTTP handler but has no response to attach
    to here; treat it the same way a real job-processing failure is treated (log and
    skip this iteration's job) rather than letting it crash the polling task.
    """
    await fault_manager.wait_if_stalled()
    try:
        await fault_manager.pre_request_hook()
    except HTTPException as exc:
        logger.warning("worker_fault_injected", detail=exc.detail)
        return
    await process_one_job(db_pool)


async def worker_loop() -> None:
    """Continuous polling loop that honors active faults (stall, latency, error_rate,
    cpu_spin) applied via /admin/fault. Previously only consulted wait_if_stalled, so
    every other fault kind was a no-op against job processing."""
    while True:
        try:
            await _worker_iteration()
        except asyncio.CancelledError:
            break
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage worker polling task and database connection pool lifecycle."""
    global db_pool
    async with db_pool_lifespan(DATABASE_URL, fault_manager, on_open=init_worker_db) as pool:
        db_pool = pool
        task = asyncio.create_task(worker_loop())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = create_service_app("worker", fault_manager, lifespan)


async def check_db_readiness() -> bool:
    """Readiness probe for worker database connection and jobs table presence.

    Connectivity alone (`ping_db`) is not sufficient: if `init_worker_db` failed to run
    (see the lifespan above), the pool would still answer `SELECT 1` while the worker can
    never process a job, so readiness must also confirm the table exists.
    """
    if db_pool is None:
        return True
    if not await ping_db(db_pool):
        return False
    try:
        async with db_pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT to_regclass('jobs')")
            row = await cur.fetchone()
            return bool(row and row[0] is not None)
    except PsycopgError:
        return False


setup_health_routes(app, [check_db_readiness])


@app.get("/jobs", tags=["Jobs"])
async def list_jobs() -> dict[str, Any]:
    """Return status of jobs in queue."""
    if db_pool is None:
        return {"jobs": IN_MEMORY_JOBS, "is_stalled": fault_manager.is_stalled}

    async with db_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, payload, status FROM jobs ORDER BY id DESC LIMIT %s",
            (JOB_LIST_LIMIT,),
        )
        rows = await cur.fetchall()
        jobs = [{"id": r[0], "payload": r[1], "status": r[2]} for r in rows]
        return {"jobs": jobs, "is_stalled": fault_manager.is_stalled}
