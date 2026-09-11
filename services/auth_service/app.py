"""Auth demo service: token validation and in-memory credential cache."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, status
from psycopg_pool import AsyncConnectionPool

from services._common import (
    FaultManager,
    create_service_app,
    db_pool_lifespan,
    get_service_role,
    ping_db,
    setup_health_routes,
)
from services._common.settings import get_services_settings

DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN_CACHE_MAX_SIZE = get_services_settings().auth_service.token_cache_max_size

# Seeded tokens are authoritative and never evicted or re-derived from the synthetic
# pattern below, even though "Bearer valid-token" also matches that pattern's prefix --
# otherwise, once the plain FIFO cache below evicted it, revalidating it would silently
# fall through to the synthetic branch and downgrade it from scope: admin to
# scope: standard with no error (the reported admin-token-downgrade bug).
SEEDED_TOKENS: dict[str, dict[str, Any]] = {
    "Bearer valid-token": {"user_id": "user_admin", "scope": "admin"},
    "Bearer test-token": {"user_id": "user_test", "scope": "read_write"},
}

# In-memory synthetic-token cache, bounded to TOKEN_CACHE_MAX_SIZE entries (oldest
# evicted first). Never holds a SEEDED_TOKENS entry.
TOKEN_CACHE: dict[str, dict[str, Any]] = {}


def _synthetic_tokens_allowed() -> bool:
    # MOCKED: any Bearer valid-*/token-* token is accepted as valid with no real
    # credential check. Real path: a Postgres-backed token lookup (not yet built; see
    # docs/02-architecture.md's "in-memory cache over Postgres" auth-service design).
    # Tracked in #16.
    override = os.getenv("AUTH_SERVICE_ALLOW_SYNTHETIC_TOKENS")
    if override is not None:
        return override.strip().lower() in ("true", "1", "yes")
    return get_service_role() != "prod"


db_pool: AsyncConnectionPool | None = None
fault_manager = FaultManager()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage database connection pool lifecycle if database URL is configured."""
    global db_pool
    async with db_pool_lifespan(DATABASE_URL, fault_manager) as pool:
        db_pool = pool
        yield


app = create_service_app("auth-service", fault_manager, lifespan)


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

    # 1. Seeded tokens are authoritative: always checked first, never evicted, never
    # re-derived from the synthetic-pattern branch below.
    if authorization in SEEDED_TOKENS:
        seeded_info = SEEDED_TOKENS[authorization]
        return {
            "valid": True,
            "user_id": seeded_info["user_id"],
            "scope": seeded_info["scope"],
            "cached": True,
        }

    # 2. Fast path: check in-memory cache
    if authorization in TOKEN_CACHE:
        cached_info = TOKEN_CACHE[authorization]
        return {
            "valid": True,
            "user_id": cached_info["user_id"],
            "scope": cached_info["scope"],
            "cached": True,
        }

    # 3. Synthetic token pattern acceptance for testing
    if _synthetic_tokens_allowed() and (
        authorization.startswith("Bearer valid-") or authorization.startswith("Bearer token-")
    ):
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
