"""Database connection pooling and health checks for demo services."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool


def get_default_conninfo() -> str:
    """Return the database connection string from environment."""
    return os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/ust_prod",
    )


def create_pool(
    conninfo: str | None = None,
    min_size: int = 1,
    max_size: int = 10,
    timeout: float = 5.0,
) -> AsyncConnectionPool:
    """Create an asynchronous Postgres connection pool."""
    dsn = conninfo or get_default_conninfo()
    return AsyncConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        open=False,
    )


async def ping_db(pool: AsyncConnectionPool) -> bool:
    """Check database connectivity for readiness probes."""
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1")
            row = await cur.fetchone()
            return bool(row and row[0] == 1)
    except Exception:
        return False


@asynccontextmanager
async def get_connection(pool: AsyncConnectionPool) -> AsyncGenerator[AsyncConnection, None]:
    """Acquire a connection from the pool with clean context management."""
    async with pool.connection() as conn:
        yield conn
