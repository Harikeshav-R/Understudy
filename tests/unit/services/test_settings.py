"""Unit tests for services/_common/settings.py's layered configuration loader."""

from pathlib import Path

import pytest

from services._common.settings import (
    ServicesSettings,
    get_services_settings,
    load_services_settings,
    reset_services_settings,
)


def test_default_settings_loading() -> None:
    """With no config files and no env overrides, every field falls back to its
    pydantic default."""
    settings = load_services_settings(
        config_path=Path("nonexistent-default.yaml"),
        local_config_path=Path("nonexistent-local.yaml"),
    )
    assert settings.edge_gateway.http_timeout_seconds == 5.0
    assert settings.auth_service.token_cache_max_size == 1000
    assert settings.worker.poll_interval_seconds == 1.0
    assert settings.worker.job_list_limit == 50
    assert settings.loadgen.rps == 20.0
    assert settings.loadgen.seed == 42
    assert settings.db.pool_max_size == 10
    assert settings.faults.injection_seed == 1337
    assert settings.metrics.histogram_buckets[0] == 0.005


def test_singleton_get_and_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_services_settings()
    first = get_services_settings()
    second = get_services_settings()
    assert first is second

    reset_services_settings()
    monkeypatch.setenv("AUTH_TOKEN_CACHE_MAX_SIZE", "5")
    third = get_services_settings()
    assert third is not first
    assert third.auth_service.token_cache_max_size == 5
    reset_services_settings()


def test_yaml_config_override(tmp_path: Path) -> None:
    config_file = tmp_path / "services.yaml"
    config_file.write_text("worker:\n  poll_interval_seconds: 2.5\n  job_list_limit: 200\n")
    settings = load_services_settings(config_path=config_file)
    assert settings.worker.poll_interval_seconds == 2.5
    assert settings.worker.job_list_limit == 200
    # Untouched fields still fall back to their pydantic defaults.
    assert settings.edge_gateway.http_timeout_seconds == 5.0


def test_local_config_overrides_default(tmp_path: Path) -> None:
    default_file = tmp_path / "services.yaml"
    default_file.write_text("loadgen:\n  rps: 20.0\n  seed: 42\n")
    local_file = tmp_path / "services.local.yaml"
    local_file.write_text("loadgen:\n  rps: 99.0\n")

    settings = load_services_settings(config_path=default_file, local_config_path=local_file)
    assert settings.loadgen.rps == 99.0
    # local.yaml only overrode rps; seed still comes from the default file.
    assert settings.loadgen.seed == 42


def test_env_override_takes_precedence_over_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "services.yaml"
    config_file.write_text("faults:\n  injection_seed: 1337\n")
    monkeypatch.setenv("FAULT_INJECTION_SEED", "999")

    settings = load_services_settings(config_path=config_file)
    assert settings.faults.injection_seed == 999


def test_env_override_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every field in _ENV_OVERRIDES is actually wired to its documented env var."""
    monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("AUTH_TOKEN_CACHE_MAX_SIZE", "42")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("WORKER_JOB_LIST_LIMIT", "10")
    monkeypatch.setenv("LOADGEN_RPS", "5.0")
    monkeypatch.setenv("LOADGEN_DURATION", "60.0")
    monkeypatch.setenv("LOADGEN_SEED", "7")
    monkeypatch.setenv("LOADGEN_AUTH_TOKEN", "token-x")
    monkeypatch.setenv("LOADGEN_TIMEOUT", "2.0")
    monkeypatch.setenv("LOADGEN_MAX_CONNECTIONS", "10")
    monkeypatch.setenv("LOADGEN_MAX_KEEPALIVE_CONNECTIONS", "5")
    monkeypatch.setenv("LOADGEN_P99_SLA_MS", "100.0")
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "2")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "20")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "3.0")
    monkeypatch.setenv("FAULT_INJECTION_SEED", "111")
    monkeypatch.setenv("FAULT_POOL_EXHAUSTION_GETCONN_TIMEOUT_SECONDS", "0.25")

    settings = load_services_settings(
        config_path=Path("nonexistent-default.yaml"),
        local_config_path=Path("nonexistent-local.yaml"),
    )
    assert settings.edge_gateway.http_timeout_seconds == 1.5
    assert settings.auth_service.token_cache_max_size == 42
    assert settings.worker.poll_interval_seconds == 0.5
    assert settings.worker.job_list_limit == 10
    assert settings.loadgen.rps == 5.0
    assert settings.loadgen.duration_seconds == 60.0
    assert settings.loadgen.seed == 7
    assert settings.loadgen.auth_token == "token-x"
    assert settings.loadgen.http_timeout_seconds == 2.0
    assert settings.loadgen.max_connections == 10
    assert settings.loadgen.max_keepalive_connections == 5
    assert settings.loadgen.p99_sla_ms == 100.0
    assert settings.db.pool_min_size == 2
    assert settings.db.pool_max_size == 20
    assert settings.db.pool_timeout_seconds == 3.0
    assert settings.faults.injection_seed == 111
    assert settings.faults.pool_exhaustion_getconn_timeout_seconds == 0.25


def test_invalid_env_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_TOKEN_CACHE_MAX_SIZE", "not-an-int")
    with pytest.raises(ValueError, match="AUTH_TOKEN_CACHE_MAX_SIZE"):
        load_services_settings(
            config_path=Path("nonexistent-default.yaml"),
            local_config_path=Path("nonexistent-local.yaml"),
        )


def test_non_dict_yaml_is_ignored(tmp_path: Path) -> None:
    config_file = tmp_path / "services.yaml"
    config_file.write_text("- just\n- a\n- list\n")
    settings = load_services_settings(config_path=config_file)
    assert isinstance(settings, ServicesSettings)
    assert settings.worker.poll_interval_seconds == 1.0


def test_real_config_services_yaml_loads() -> None:
    """The committed config/services.yaml must parse and match its own documented
    defaults exactly, so it never silently drifts from the pydantic model."""
    settings = load_services_settings()
    assert settings.loadgen.rps == 20.0
    assert settings.faults.injection_seed == 1337
