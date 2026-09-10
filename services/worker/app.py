"""Worker demo service: background job consumer using FOR UPDATE SKIP LOCKED.

Supports stall and crash faults.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from services._common import (
    FaultManager,
    create_pool,
    ping_db,
    setup_fault_middleware,
    setup_fault_routes,
    setup_health_routes,
    setup_metrics,
)

DATABASE_URL = os.getenv("DATABASE_URL")
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "1.0"))

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
    except Exception:
        return False


async def worker_loop() -> None:
    """Continuous polling loop that honors active stall faults."""
    while True:
        try:
            await fault_manager.wait_if_stalled()
            await process_one_job(db_pool)
        except asyncio.CancelledError:
            break
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage worker polling task and database connection pool lifecycle."""
    global db_pool
    if DATABASE_URL:
        db_pool = create_pool(DATABASE_URL)
        await db_pool.open()
        fault_manager.pool = db_pool
        with contextlib.suppress(Exception):
            await init_worker_db(db_pool)

    task = asyncio.create_task(worker_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if db_pool:
            await db_pool.close()


app = FastAPI(title="worker", lifespan=lifespan)
setup_metrics(app, "worker")
setup_fault_middleware(app, fault_manager)
setup_fault_routes(app, fault_manager)


async def check_db_readiness() -> bool:
    """Readiness probe for worker database connection."""
    if db_pool is None:
        return True
    return await ping_db(db_pool)


setup_health_routes(app, [check_db_readiness])


@app.get("/jobs", tags=["Jobs"])
async def list_jobs() -> dict[str, Any]:
    """Return status of jobs in queue."""
    if db_pool is None:
        return {"jobs": IN_MEMORY_JOBS, "is_stalled": fault_manager.is_stalled}

    async with db_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT id, payload, status FROM jobs ORDER BY id DESC LIMIT 50")
        rows = await cur.fetchall()
        jobs = [{"id": r[0], "payload": r[1], "status": r[2]} for r in rows]
        return {"jobs": jobs, "is_stalled": fault_manager.is_stalled}
