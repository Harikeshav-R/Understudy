"""Auth demo service: token validation and in-memory credential cache."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, status
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
TOKEN_CACHE_MAX_SIZE = int(os.getenv("AUTH_TOKEN_CACHE_MAX_SIZE", "1000"))

# In-memory token cache, bounded to TOKEN_CACHE_MAX_SIZE entries (oldest evicted first)
TOKEN_CACHE: dict[str, dict[str, Any]] = {
    "Bearer valid-token": {"user_id": "user_admin", "scope": "admin"},
    "Bearer test-token": {"user_id": "user_test", "scope": "read_write"},
}

db_pool: AsyncConnectionPool | None = None
fault_manager = FaultManager()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage database connection pool lifecycle if database URL is configured."""
    global db_pool
    if DATABASE_URL:
        db_pool = create_pool(DATABASE_URL)
        await db_pool.open()
        fault_manager.pool = db_pool
    try:
        yield
    finally:
        if db_pool:
            await db_pool.close()


app = FastAPI(title="auth-service", lifespan=lifespan)
setup_metrics(app, "auth-service")
setup_fault_middleware(app, fault_manager)
setup_fault_routes(app, fault_manager)


async def check_db_readiness() -> bool:
    """Readiness probe for database connection if configured."""
    if db_pool is None:
        return True
    return await ping_db(db_pool)


setup_health_routes(app, [check_db_readiness])


@app.get("/validate", tags=["Authentication"])
async def validate_token(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Validate token against in-memory cache and optional database fallback."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    # 1. Fast path: check in-memory cache
    if authorization in TOKEN_CACHE:
        cached_info = TOKEN_CACHE[authorization]
        return {
            "valid": True,
            "user_id": cached_info["user_id"],
            "scope": cached_info["scope"],
            "cached": True,
        }

    # 2. Synthetic token pattern acceptance for testing
    if authorization.startswith("Bearer valid-") or authorization.startswith("Bearer token-"):
        user_info = {"user_id": "user_dynamic", "scope": "standard"}
        if len(TOKEN_CACHE) >= TOKEN_CACHE_MAX_SIZE:
            TOKEN_CACHE.pop(next(iter(TOKEN_CACHE)))
        TOKEN_CACHE[authorization] = user_info
        return {
            "valid": True,
            "user_id": user_info["user_id"],
            "scope": user_info["scope"],
            "cached": False,
        }

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authorization token",
    )
