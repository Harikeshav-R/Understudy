"""Database engine and session management for Understudy system store."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from understudy.common.config import get_settings
from understudy.common.errors import StoreError


def normalize_dsn(dsn: str) -> str:
    """Normalize a PostgreSQL DSN to use the async psycopg driver dialect."""
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+psycopg://", 1)
    return dsn


class StoreDatabase:
    """Manages AsyncEngine and sessionmaker for Understudy system store."""

    def __init__(
        self,
        dsn: str | None = None,
        echo: bool = False,
        pool_size: int = 5,
        max_overflow: int = 10,
    ) -> None:
        raw_dsn = dsn or get_settings().endpoints.postgres_system
        self._dsn = normalize_dsn(raw_dsn)
        self._engine: AsyncEngine = create_async_engine(
            self._dsn,
            echo=echo,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
        )
        self._sessionmaker = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        """Return the underlying AsyncEngine."""
        return self._engine

    @property
    def dsn(self) -> str:
        """Return the normalized connection DSN."""
        return self._dsn

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Provide an transactional async session with automatic rollback on error."""
        async with self._sessionmaker() as session:
            try:
                yield session
            except SQLAlchemyError as exc:
                await session.rollback()
                raise StoreError(f"Database session error: {exc}") from exc

    async def dispose(self) -> None:
        """Dispose the underlying connection pool."""
        await self._engine.dispose()
