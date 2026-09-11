"""Shared FastAPI app bootstrap and db-pool lifespan skeleton for demo services.

Every service's app.py repeated the same four-line FastAPI/metrics/fault-middleware/
fault-routes bootstrap, and auth_service/data_service/worker repeated the same
open-pool/assign-to-fault-manager/optional-init/close pool lifecycle around it. Factored
out here so each service's app.py only has to supply what's actually different: its
title, its own routes, and (for the three with a database) an optional init callback.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI
from psycopg import Error as PsycopgError
from psycopg_pool import AsyncConnectionPool

from services._common.db import create_pool
from services._common.faults import FaultManager, setup_fault_middleware, setup_fault_routes
from services._common.logging import get_logger
from services._common.metrics import setup_metrics

logger = get_logger(__name__)


def create_service_app(
    title: str,
    fault_manager: FaultManager,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]],
) -> FastAPI:
    """Build a FastAPI app with the bootstrap every demo service repeats: metrics
    collection, fault-injection middleware, and the /admin/fault control routes."""
    app = FastAPI(title=title, lifespan=lifespan)
    setup_metrics(app, title)
    setup_fault_middleware(app, fault_manager)
    setup_fault_routes(app, fault_manager)
    return app


@asynccontextmanager
async def db_pool_lifespan(
    database_url: str | None,
    fault_manager: FaultManager,
    on_open: Callable[[AsyncConnectionPool], Awaitable[None]] | None = None,
) -> AsyncGenerator[AsyncConnectionPool | None, None]:
    """Open a connection pool (if configured), assign it to fault_manager.pool, run an
    optional init callback, and close it on exit. Yields the pool (or None) so the
    caller can assign it to its own module-level global -- each service reads its
    `db_pool` directly from route handlers, and this helper has no way to reach into
    another module's globals itself.

    A PsycopgError from `on_open` is logged and swallowed, matching every service's
    prior behavior: a failed schema init must not prevent the app from starting (the
    readiness probe is what's supposed to catch this, not app startup).
    """
    pool: AsyncConnectionPool | None = None
    if database_url:
        pool = create_pool(database_url)
        await pool.open()
        fault_manager.pool = pool
        if on_open is not None:
            try:
                await on_open(pool)
            except PsycopgError as exc:
                logger.error("service_db_init_failed", error=str(exc))
    try:
        yield pool
    finally:
        if pool:
            await pool.close()
