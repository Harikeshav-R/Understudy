"""Unit tests for store database engine and session management."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from understudy.common.errors import StoreError
from understudy.store.database import StoreDatabase, normalize_dsn


def test_normalize_dsn() -> None:
    """Verify DSN normalization converts PostgreSQL URLs to the psycopg async dialect."""
    assert (
        normalize_dsn("postgresql://user:pass@localhost:5432/db")
        == "postgresql+psycopg://user:pass@localhost:5432/db"
    )
    assert (
        normalize_dsn("postgres://user:pass@localhost:5432/db")
        == "postgresql+psycopg://user:pass@localhost:5432/db"
    )
    assert (
        normalize_dsn("postgresql+psycopg://user:pass@localhost:5432/db")
        == "postgresql+psycopg://user:pass@localhost:5432/db"
    )
    assert normalize_dsn("sqlite+aiosqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"


def test_store_database_initialization() -> None:
    """Verify StoreDatabase initializes engine and properties."""
    with patch("understudy.store.database.create_async_engine") as mock_create_engine:
        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine

        db = StoreDatabase(dsn="postgresql://custom:5434/db")
        assert db.dsn == "postgresql+psycopg://custom:5434/db"
        assert db.engine == mock_engine
        mock_create_engine.assert_called_once()


def test_store_database_default_settings() -> None:
    """Verify StoreDatabase falls back to settings endpoints.postgres_system."""
    with patch("understudy.store.database.create_async_engine") as mock_create:
        db = StoreDatabase()
        assert "postgresql+psycopg://" in db.dsn
        mock_create.assert_called_once()


@pytest.mark.asyncio
async def test_store_database_session_success() -> None:
    """Verify session context manager provides session."""
    with patch("understudy.store.database.create_async_engine"):
        db = StoreDatabase(dsn="postgresql://dummy/db")
        mock_session = AsyncMock()
        db._sessionmaker = MagicMock(return_value=mock_session)
        mock_session.__aenter__.return_value = mock_session

        async with db.session() as s:
            assert s == mock_session


@pytest.mark.asyncio
async def test_store_database_session_rollback_on_error() -> None:
    """Verify session rolls back and raises StoreError on SQLAlchemyError."""
    with patch("understudy.store.database.create_async_engine"):
        db = StoreDatabase(dsn="postgresql://dummy/db")
        mock_session = AsyncMock()
        db._sessionmaker = MagicMock(return_value=mock_session)
        mock_session.__aenter__.return_value = mock_session

        with pytest.raises(StoreError, match="Database session error"):
            async with db.session():
                raise OperationalError(
                    "query failed", params=None, orig=Exception("connection drop")
                )

        mock_session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_database_dispose() -> None:
    """Verify dispose delegates to engine.dispose."""
    with patch("understudy.store.database.create_async_engine"):
        db = StoreDatabase(dsn="postgresql://dummy/db")
        db._engine = AsyncMock()
        await db.dispose()
        db._engine.dispose.assert_awaited_once()
