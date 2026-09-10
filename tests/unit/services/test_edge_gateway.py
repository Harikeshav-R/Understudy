"""Unit tests for edge-gateway service."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import httpx
import pytest

import services.edge_gateway.app as gw_module
from services.edge_gateway.app import (
    app,
    check_auth_reachability,
    check_data_reachability,
)


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with (
        gw_module.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


@pytest.mark.asyncio
async def test_reachability_probes() -> None:
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    # Success case
    mock_http.get.return_value = httpx.Response(200)
    assert await check_auth_reachability() is True
    assert await check_data_reachability() is True

    # Failure case (non-200)
    mock_http.get.return_value = httpx.Response(500)
    assert await check_auth_reachability() is False
    assert await check_data_reachability() is False

    # Exception case
    mock_http.get.side_effect = httpx.ConnectError("refused")
    assert await check_auth_reachability() is False
    assert await check_data_reachability() is False


@pytest.mark.asyncio
async def test_get_items_success(client: httpx.AsyncClient) -> None:
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    mock_http.get.side_effect = [
        httpx.Response(200, json={"valid": True}),
        httpx.Response(200, json={"items": [{"id": 1, "name": "item1"}]}),
    ]

    resp = await client.get("/api/items")
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"id": 1, "name": "item1"}]}


@pytest.mark.asyncio
async def test_get_items_auth_failed(client: httpx.AsyncClient) -> None:
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    mock_http.get.return_value = httpx.Response(401, json={"detail": "Unauthorized"})

    resp = await client.get("/api/items")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_items_auth_unavailable(client: httpx.AsyncClient) -> None:
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    mock_http.get.side_effect = httpx.ConnectError("auth down")

    resp = await client.get("/api/items")
    assert resp.status_code == 503
    assert "Auth service unavailable" in resp.text


@pytest.mark.asyncio
async def test_get_items_data_unavailable(client: httpx.AsyncClient) -> None:
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    mock_http.get.side_effect = [
        httpx.Response(200, json={"valid": True}),
        httpx.ConnectError("data down"),
    ]

    resp = await client.get("/api/items")
    assert resp.status_code == 503
    assert "Data service unavailable" in resp.text


@pytest.mark.asyncio
async def test_get_items_data_error_status(client: httpx.AsyncClient) -> None:
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    mock_http.get.side_effect = [
        httpx.Response(200, json={"valid": True}),
        httpx.Response(500, text="Internal error in data service"),
    ]

    resp = await client.get("/api/items")
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_create_item_success(client: httpx.AsyncClient) -> None:
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    mock_http.get.return_value = httpx.Response(200, json={"valid": True})
    mock_http.post.return_value = httpx.Response(201, json={"id": 10, "name": "new item"})

    resp = await client.post("/api/items", json={"name": "new item", "description": "test"})
    assert resp.status_code == 201
    assert resp.json() == {"id": 10, "name": "new item"}


@pytest.mark.asyncio
async def test_create_item_auth_failed(client: httpx.AsyncClient) -> None:
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    mock_http.get.return_value = httpx.Response(401)

    resp = await client.post("/api/items", json={"name": "new item"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_item_auth_error(client: httpx.AsyncClient) -> None:
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    mock_http.get.side_effect = httpx.ConnectError("auth down")

    resp = await client.post("/api/items", json={"name": "new item"})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_create_item_data_error(client: httpx.AsyncClient) -> None:
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    mock_http.get.return_value = httpx.Response(200)
    mock_http.post.side_effect = httpx.ConnectError("data down")

    resp = await client.post("/api/items", json={"name": "new item"})
    assert resp.status_code == 503
