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
    with patch("services.auth_service.app.create_pool", return_value=mock_pool):
        async with auth_module.lifespan(app):
            assert mock_pool.open.await_count == 1
        assert mock_pool.close.await_count == 1
    auth_module.db_pool = None
