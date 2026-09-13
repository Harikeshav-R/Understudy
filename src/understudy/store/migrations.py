"""Programmatic runner for Understudy Alembic migrations."""

from pathlib import Path

from alembic import command
from alembic.config import Config

from understudy.common.config import get_settings
from understudy.common.errors import StoreError
from understudy.store.database import normalize_dsn


def get_alembic_config(dsn: str | None = None) -> Config:
    """Create and configure an Alembic Config object for the store migrations."""
    root_dir = Path(__file__).resolve().parent.parent.parent.parent
    ini_path = root_dir / "alembic.ini"
    script_loc = Path(__file__).resolve().parent / "migrations"

    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(script_loc))

    raw_url = dsn or get_settings().endpoints.postgres_system
    cfg.set_main_option("sqlalchemy.url", normalize_dsn(raw_url))
    return cfg


def apply_migrations(dsn: str | None = None, revision: str = "head") -> None:
    """Apply Alembic migrations up to the specified revision."""
    try:
        cfg = get_alembic_config(dsn=dsn)
        command.upgrade(cfg, revision)
    except Exception as exc:
        raise StoreError(f"Failed to apply database migrations: {exc}") from exc


def rollback_migrations(dsn: str | None = None, revision: str = "base") -> None:
    """Roll back Alembic migrations down to the specified revision."""
    try:
        cfg = get_alembic_config(dsn=dsn)
        command.downgrade(cfg, revision)
    except Exception as exc:
        raise StoreError(f"Failed to rollback database migrations: {exc}") from exc
