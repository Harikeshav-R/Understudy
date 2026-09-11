"""Database connection pooling and health checks for demo services."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg import Error as PsycopgError
from psycopg_pool import AsyncConnectionPool

from services._common.settings import get_services_settings


def get_default_conninfo() -> str:
    """Return the database connection string from environment."""
    return os.getenv(
        "DATABASE_URL",
        "postgresql://postgres@localhost:5432/ust_prod",
    )


def create_pool(
    conninfo: str | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    timeout: float | None = None,
) -> AsyncConnectionPool:
    """Create an asynchronous Postgres connection pool."""
    dsn = conninfo or get_default_conninfo()
    db_settings = get_services_settings().db
    return AsyncConnectionPool(
        conninfo=dsn,
        min_size=min_size if min_size is not None else db_settings.pool_min_size,
        max_size=max_size if max_size is not None else db_settings.pool_max_size,
        timeout=timeout if timeout is not None else db_settings.pool_timeout_seconds,
        open=False,
    )


async def ping_db(pool: AsyncConnectionPool) -> bool:
    """Check database connectivity for readiness probes."""
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1")
            row = await cur.fetchone()
            return bool(row and row[0] == 1)
    except PsycopgError:
        return False


@asynccontextmanager
async def get_connection(pool: AsyncConnectionPool) -> AsyncGenerator[AsyncConnection, None]:
    """Acquire a connection from the pool with clean context management."""
    async with pool.connection() as conn:
        yield conn
