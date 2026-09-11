"""Unit tests for auth-service."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import services.auth_service.app as auth_module
from services.auth_service.app import app, check_db_readiness


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with (
        auth_module.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


@pytest.mark.asyncio
async def test_validate_missing_header(client: httpx.AsyncClient) -> None:
    resp = await client.get("/validate")
    assert resp.status_code == 401
    assert "Missing Authorization" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_validate_cached_token(client: httpx.AsyncClient) -> None:
    resp = await client.get("/validate", headers={"Authorization": "Bearer valid-token"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["cached"] is True
    assert data["user_id"] == "user_admin"


@pytest.mark.asyncio
async def test_validate_dynamic_token(client: httpx.AsyncClient) -> None:
    # First request caches it
    token = "Bearer valid-dyn-123"
    resp1 = await client.get("/validate", headers={"Authorization": token})
    assert resp1.status_code == 200
    assert resp1.json()["cached"] is False

    # Second request hits cache
    resp2 = await client.get("/validate", headers={"Authorization": token})
    assert resp2.status_code == 200
    assert resp2.json()["cached"] is True


@pytest.mark.asyncio
async def test_token_cache_bounded(
    monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient
) -> None:
    """TOKEN_CACHE must not grow without bound: oldest entry is evicted at capacity."""
    monkeypatch.setattr(auth_module, "TOKEN_CACHE_MAX_SIZE", 2)
    monkeypatch.setattr(
        auth_module,
        "TOKEN_CACHE",
        {
            "Bearer seed-a": {"user_id": "a", "scope": "x"},
            "Bearer seed-b": {"user_id": "b", "scope": "x"},
        },
    )

    resp = await client.get("/validate", headers={"Authorization": "Bearer valid-new"})
    assert resp.status_code == 200

    assert len(auth_module.TOKEN_CACHE) == 2
    assert "Bearer seed-a" not in auth_module.TOKEN_CACHE
    assert "Bearer valid-new" in auth_module.TOKEN_CACHE


@pytest.mark.asyncio
async def test_validate_invalid_token(client: httpx.AsyncClient) -> None:
    resp = await client.get("/validate", headers={"Authorization": "Bearer totally-fake"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_check_db_readiness() -> None:
    auth_module.db_pool = None
    assert await check_db_readiness() is True

    mock_pool = AsyncMock()
    auth_module.db_pool = mock_pool
    with patch("services.auth_service.app.ping_db", return_value=True):
        assert await check_db_readiness() is True
    auth_module.db_pool = None


@pytest.mark.asyncio
async def test_auth_lifespan_with_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_module, "DATABASE_URL", "postgresql://fake:5432/db")
    mock_pool = AsyncMock()
    with patch("services._common.bootstrap.create_pool", return_value=mock_pool):
        async with auth_module.lifespan(app):
            assert mock_pool.open.await_count == 1
        assert mock_pool.close.await_count == 1
    auth_module.db_pool = None
