"""Unit tests for programmatic Alembic migration runner in understudy.store.migrations."""

from unittest.mock import patch

import pytest

from understudy.common.errors import StoreError
from understudy.store.migrations import (
    apply_migrations,
    get_alembic_config,
    rollback_migrations,
)


def test_get_alembic_config_default_and_custom() -> None:
    """Verify get_alembic_config sets script location and normalized URL."""
    cfg_default = get_alembic_config()
    default_url = cfg_default.get_main_option("sqlalchemy.url")
    assert default_url is not None and "postgresql+psycopg://" in default_url

    cfg_custom = get_alembic_config(dsn="postgresql://custom:5434/test_db")
    assert (
        cfg_custom.get_main_option("sqlalchemy.url") == "postgresql+psycopg://custom:5434/test_db"
    )


def test_apply_migrations_success() -> None:
    """Verify apply_migrations calls command.upgrade."""
    with patch("understudy.store.migrations.command.upgrade") as mock_upgrade:
        apply_migrations(dsn="postgresql://dummy/db", revision="head")
        mock_upgrade.assert_called_once()
        assert mock_upgrade.call_args[0][1] == "head"


def test_apply_migrations_error() -> None:
    """Verify apply_migrations wraps errors in StoreError."""
    with patch("understudy.store.migrations.command.upgrade") as mock_upgrade:
        mock_upgrade.side_effect = Exception("connection refused")
        with pytest.raises(StoreError, match="Failed to apply database migrations"):
            apply_migrations(dsn="postgresql://dummy/db")


def test_rollback_migrations_success() -> None:
    """Verify rollback_migrations calls command.downgrade."""
    with patch("understudy.store.migrations.command.downgrade") as mock_downgrade:
        rollback_migrations(dsn="postgresql://dummy/db", revision="base")
        mock_downgrade.assert_called_once()
        assert mock_downgrade.call_args[0][1] == "base"


def test_rollback_migrations_error() -> None:
    """Verify rollback_migrations wraps errors in StoreError."""
    with patch("understudy.store.migrations.command.downgrade") as mock_downgrade:
        mock_downgrade.side_effect = Exception("migration table corrupted")
        with pytest.raises(StoreError, match="Failed to rollback database migrations"):
            rollback_migrations(dsn="postgresql://dummy/db")
