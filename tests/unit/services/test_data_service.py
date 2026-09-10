"""Unit tests for data-service, CRUD operations, and :good / :regression N+1 behavior."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

import services.data_service.app as data_module
from services.data_service.app import (
    ItemCreate,
    app,
    check_db_readiness,
    create_item,
    init_db,
)


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with (
        data_module.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


@pytest.mark.asyncio
async def test_in_memory_items_flow(client: httpx.AsyncClient) -> None:
    data_module.db_pool = None
    data_module.DATA_SERVICE_VARIANT = "good"

    # 1. Get initial items
    resp = await client.get("/items")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) >= 2
    assert resp.json()["variant"] == "good"

    # 2. Post item
    new_item = {
        "name": "Test Item",
        "description": "Test Desc",
        "details": [{"key": "k", "value": "v"}],
    }
    post_res = await client.post("/items", json=new_item)
    assert post_res.status_code == 201
    assert post_res.json()["name"] == "Test Item"


@pytest.mark.asyncio
async def test_init_db_empty_and_populated() -> None:
    mock_cursor = AsyncMock()
    mock_conn = MagicMock()
    mock_conn.commit = AsyncMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    # 1. When items table is empty (count = 0), inserts initial items
    mock_cursor.fetchone.return_value = (0,)
    await init_db(mock_pool)
    assert mock_cursor.execute.await_count >= 3
    assert mock_conn.commit.await_count == 1

    # 2. When items table already has rows (count > 0), does not re-insert
    mock_cursor.reset_mock()
    mock_cursor.fetchone.return_value = (5,)
    await init_db(mock_pool)
    assert mock_cursor.execute.await_count == 2


@pytest.mark.asyncio
async def test_db_get_items_good_variant(
    monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient
) -> None:
    monkeypatch.setattr(data_module, "DATA_SERVICE_VARIANT", "good")

    mock_cursor = AsyncMock()
    # Test details as already-parsed list or raw json string
    mock_cursor.fetchall.return_value = [
        (1, "Item1", "Desc1", [{"key": "k1", "value": "v1"}]),
        (2, "Item2", "Desc2", '[{"key": "k2", "value": "v2"}]'),
    ]

    mock_conn = MagicMock()
    mock_conn.commit = AsyncMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    monkeypatch.setattr(data_module, "db_pool", mock_pool)

    resp = await client.get("/items")
    assert resp.status_code == 200
    data = resp.json()
    assert data["variant"] == "good"
    assert len(data["items"]) == 2
    # Single consolidated database query executed!
    assert mock_cursor.execute.await_count == 1


@pytest.mark.asyncio
async def test_db_get_items_regression_n_plus_one(
    monkeypatch: pytest.MonkeyPatch,
    client: httpx.AsyncClient,
) -> None:
    """Verifies that the :regression variant executes genuine N+1 queries."""
    monkeypatch.setattr(data_module, "DATA_SERVICE_VARIANT", "regression")

    mock_cursor = AsyncMock()
    # Initial query returns 3 items
    mock_cursor.fetchall.side_effect = [
        [(1, "Item1", "Desc1"), (2, "Item2", "Desc2"), (3, "Item3", "Desc3")],  # Query 1
        [("color", "red")],  # Query 2 (Item 1)
        [("color", "blue")],  # Query 3 (Item 2)
        [("color", "green")],  # Query 4 (Item 3)
    ]

    mock_conn = MagicMock()
    mock_conn.commit = AsyncMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    monkeypatch.setattr(data_module, "db_pool", mock_pool)

    resp = await client.get("/items")
    assert resp.status_code == 200
    data = resp.json()
    assert data["variant"] == "regression"
    assert len(data["items"]) == 3
    # N+1 queries executed: 1 initial query + 3 detail queries = 4 total queries!
    assert mock_cursor.execute.await_count == 4


@pytest.mark.asyncio
async def test_db_create_item(monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient) -> None:
    mock_cursor = AsyncMock()
    mock_cursor.fetchone.return_value = (42,)

    mock_conn = MagicMock()
    mock_conn.commit = AsyncMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    monkeypatch.setattr(data_module, "db_pool", mock_pool)

    payload = {"name": "DB Item", "description": "Desc", "details": [{"key": "k", "value": "v"}]}
    resp = await client.post("/items", json=payload)
    assert resp.status_code == 201
    assert resp.json()["id"] == 42


@pytest.mark.asyncio
async def test_db_create_item_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cursor = AsyncMock()
    mock_cursor.fetchone.return_value = None  # Insert failed to return id

    mock_conn = MagicMock()
    mock_conn.commit = AsyncMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    monkeypatch.setattr(data_module, "db_pool", mock_pool)

    with pytest.raises(HTTPException) as exc_info:
        await create_item(ItemCreate(name="Bad", description="Bad"))
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_check_db_readiness() -> None:
    data_module.db_pool = None
    assert await check_db_readiness() is True

    mock_pool = AsyncMock()
    data_module.db_pool = mock_pool
    with patch("services.data_service.app.ping_db", return_value=True):
        assert await check_db_readiness() is True
    data_module.db_pool = None


@pytest.mark.asyncio
async def test_data_lifespan_with_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_module, "DATABASE_URL", "postgresql://fake:5432/db")
    mock_pool = AsyncMock()
    with (
        patch("services.data_service.app.create_pool", return_value=mock_pool),
        patch("services.data_service.app.init_db", side_effect=RuntimeError("db init error")),
    ):
        async with data_module.lifespan(app):
            assert mock_pool.open.await_count == 1
        assert mock_pool.close.await_count == 1
    data_module.db_pool = None
