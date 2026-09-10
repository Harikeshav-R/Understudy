"""Global pytest fixtures and test environment isolation."""

import os
from collections.abc import Generator
from pathlib import Path

import pytest

import understudy.common.config as config_module

_ISOLATED_SECRET_KEYS = (
    "OPENROUTER_API_KEY",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    "SLACK_BOT_TOKEN",
    "SLACK_CHANNEL_ID",
    "PAGERDUTY_TOKEN",
    "PAGERDUTY_SERVICE_ID",
    "PAGERDUTY_WEBHOOK_SECRET",
    "PAGERDUTY_ROUTING_KEY",
    "DATADOG_API_KEY",
    "DATADOG_APP_KEY",
)


@pytest.fixture(autouse=True)
def isolate_test_settings(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Ensure unit tests run isolated from the local developer .env file and shell environment."""
    config_module.reset_settings()

    original_read_env = config_module._read_env_file

    def safe_read_env_file(path: Path) -> dict[str, str]:
        if path == Path(".env"):
            return {}
        return original_read_env(path)

    monkeypatch.setattr(config_module, "_read_env_file", safe_read_env_file)

    for key in list(os.environ):
        if key.startswith("UNDERSTUDY_") or key in _ISOLATED_SECRET_KEYS:
            monkeypatch.delenv(key, raising=False)

    yield
    config_module.reset_settings()
