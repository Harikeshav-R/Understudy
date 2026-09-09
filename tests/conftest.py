"""Global pytest fixtures and test environment isolation."""

from collections.abc import Generator
from pathlib import Path

import pytest

import understudy.common.config as config_module


@pytest.fixture(autouse=True)
def isolate_test_settings(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Ensure unit tests run isolated from the local developer .env file."""
    config_module.reset_settings()

    original_read_env = config_module._read_env_file

    def safe_read_env_file(path: Path) -> dict[str, str]:
        if path == Path(".env"):
            return {}
        return original_read_env(path)

    monkeypatch.setattr(config_module, "_read_env_file", safe_read_env_file)
    yield
    config_module.reset_settings()
