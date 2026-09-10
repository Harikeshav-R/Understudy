"""Unit tests for database pooling and ping utilities."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from services._common.db import (
    create_pool,
    get_connection,
    get_default_conninfo,
    ping_db,
)


def test_get_default_conninfo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert "localhost:5432/ust_prod" in get_default_conninfo()

    monkeypatch.setenv("DATABASE_URL", "postgresql://custom:5432/custom_db")
    assert get_default_conninfo() == "postgresql://custom:5432/custom_db"


def test_create_pool() -> None:
    pool = create_pool("postgresql://test:5432/db", min_size=2, max_size=5, timeout=2.0)
    assert pool.min_size == 2
    assert pool.max_size == 5


@pytest.mark.asyncio
async def test_ping_db_success() -> None:
    mock_cursor = AsyncMock()
    mock_cursor.fetchone.return_value = (1,)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    assert await ping_db(mock_pool) is True


@pytest.mark.asyncio
async def test_ping_db_wrong_value() -> None:
    mock_cursor = AsyncMock()
    mock_cursor.fetchone.return_value = (0,)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    assert await ping_db(mock_pool) is False


@pytest.mark.asyncio
async def test_ping_db_exception() -> None:
    mock_pool = MagicMock()
    mock_pool.connection.side_effect = ConnectionError("offline")

    assert await ping_db(mock_pool) is False


@pytest.mark.asyncio
async def test_get_connection_context() -> None:
    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    async with get_connection(mock_pool) as conn:
        assert conn is mock_conn
