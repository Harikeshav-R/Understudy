"""Unit tests for layered configuration loading and Settings."""

from pathlib import Path

import pytest

from understudy.common import ConfigError
from understudy.common.config import (
    Settings,
    get_settings,
    load_settings,
    reset_settings,
)


def test_default_config_loading() -> None:
    reset_settings()
    settings = load_settings()
    assert isinstance(settings, Settings)
    assert settings.candidate_count == 3
    assert settings.timeouts.fork_seconds == 120
    assert settings.cluster.prod_namespace == "ust-prod"
    assert settings.scoring.recovery_weight == 0.40
    assert settings.endpoints.mirror_gateway == "http://localhost:8080"


def test_singleton_get_settings() -> None:
    reset_settings()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


def test_local_config_override(tmp_path: Path) -> None:
    def_file = tmp_path / "default.yaml"
    def_file.write_text("env: dev\ncandidate_count: 3\ntimeouts:\n  fork_seconds: 100\n")

    loc_file = tmp_path / "local.yaml"
    loc_file.write_text("candidate_count: 5\ntimeouts:\n  fork_seconds: 200\n")

    settings = load_settings(default_config_path=def_file, local_config_path=loc_file)
    assert settings.candidate_count == 5
    assert settings.timeouts.fork_seconds == 200
    assert settings.env == "dev"


def test_env_file_and_os_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "UNDERSTUDY_LOG_LEVEL=DEBUG\nANTHROPIC_API_KEY=test-ant-key\nGITHUB_TOKEN=test-gh-token\n"
    )

    monkeypatch.setenv("UNDERSTUDY_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("UNDERSTUDY_ACTUATION_ENABLED", "false")
    monkeypatch.setenv("UNDERSTUDY_FAULT_INJECTION_ENABLED", "true")
    monkeypatch.setenv("UNDERSTUDY_ROLE", "twin")
    monkeypatch.setenv("UNDERSTUDY_CANDIDATE_COUNT", "4")
    monkeypatch.setenv("UNDERSTUDY_ENV", "staging")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    settings = load_settings(
        default_config_path=tmp_path / "none.yaml",
        local_config_path=tmp_path / "none.yaml",
        env_file_path=env_file,
    )

    assert settings.log_level == "WARNING"  # OS env beats .env
    assert settings.actuation_enabled is False
    assert settings.fault_injection_enabled is True
    assert settings.role == "twin"
    assert settings.candidate_count == 4
    assert settings.env == "staging"
    assert settings.secrets.anthropic_api_key == "test-ant-key"
    assert settings.secrets.github_token == "test-gh-token"
    assert settings.secrets.slack_bot_token == "xoxb-test"


def test_config_errors_on_malformed_files(tmp_path: Path) -> None:
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(": : invalid yaml\n")

    with pytest.raises(ConfigError, match="Failed to load default config"):
        load_settings(default_config_path=bad_yaml)

    with pytest.raises(ConfigError, match="Failed to load local config"):
        load_settings(default_config_path=tmp_path / "empty.yaml", local_config_path=bad_yaml)


def test_invalid_env_file_raises(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    # Create an unreadable directory where a file was expected to cause read exception
    env_file.mkdir()

    with pytest.raises(ConfigError, match=r"is not a regular file"):
        load_settings(env_file_path=env_file)


def test_env_file_read_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=BAR\n")

    def mock_read_text(*_args: object, **_kwargs: object) -> str:
        raise OSError("Permission denied")

    monkeypatch.setattr(Path, "read_text", mock_read_text)
    with pytest.raises(ConfigError, match=r"Failed to read \.env file"):
        load_settings(env_file_path=env_file)


def test_env_file_with_comments_and_empty_lines(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("\n# comment line\nKEY_WITHOUT_EQUALS\nUNDERSTUDY_LOG_LEVEL=DEBUG\n")
    settings = load_settings(
        default_config_path=tmp_path / "none.yaml",
        local_config_path=tmp_path / "none.yaml",
        env_file_path=env_file,
    )
    assert settings.log_level == "DEBUG"


def test_non_dict_yaml_handling(tmp_path: Path) -> None:
    scalar_yaml = tmp_path / "scalar.yaml"
    scalar_yaml.write_text("just a string\n")
    settings = load_settings(default_config_path=scalar_yaml, local_config_path=scalar_yaml)
    assert isinstance(settings, Settings)


def test_invalid_candidate_count_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERSTUDY_CANDIDATE_COUNT", "not-an-int")
    with pytest.raises(ConfigError, match=r"Invalid integer for UNDERSTUDY_CANDIDATE_COUNT"):
        load_settings()


def test_model_validation_failure_raises_config_error(tmp_path: Path) -> None:
    bad_cfg = tmp_path / "bad_val.yaml"
    bad_cfg.write_text("timeouts:\n  fork_seconds: 'not_an_int'\n")
    with pytest.raises(ConfigError, match=r"Configuration validation failed"):
        load_settings(default_config_path=bad_cfg)
