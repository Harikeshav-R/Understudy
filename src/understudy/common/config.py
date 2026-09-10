"""Layered configuration loader and frozen Settings schema."""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from understudy.common.errors import ConfigError


class ScoringSettings(BaseModel):
    """Scoring weights and ceilings configuration."""

    model_config = ConfigDict(frozen=True)

    recovery_weight: float = 0.40
    blast_weight: float = 0.25
    downstream_weight: float = 0.20
    violations_weight: float = 0.15
    ambiguity_margin: float = 0.15
    mirror_drop_ceiling: float = 0.05
    min_probe_samples: int = 60
    recovery_consecutive_seconds: int = 15
    recovery_timeout_seconds: int = 180
    downstream_error_ceiling: float = 0.10
    violation_ceiling: int = 5


class TimeoutSettings(BaseModel):
    """Execution timeouts in seconds."""

    model_config = ConfigDict(frozen=True)

    fork_seconds: int = 120
    candidate_apply_seconds: int = 60
    observation_seconds: int = 200
    kernel_seconds: int = 5
    incident_seconds: int = 600


class ClusterSettings(BaseModel):
    """Cluster namespace and connection parameters."""

    model_config = ConfigDict(frozen=True)

    context: str = "k3d-ust"
    prod_namespace: str = "ust-prod"
    system_namespace: str = "ust-system"
    twin_namespace_prefix: str = "ust-twin"


class EndpointSettings(BaseModel):
    """Service URLs and connection strings."""

    model_config = ConfigDict(frozen=True)

    mirror_gateway: str = "http://localhost:8080"
    prometheus: str = "http://localhost:9090"
    loki: str = "http://localhost:3100"
    postgres_prod: str = "postgresql://postgres:postgres@localhost:5432/ust_prod"
    postgres_twin: str = "postgresql://postgres:postgres@localhost:5433/postgres"
    postgres_system: str = "postgresql://postgres:postgres@localhost:5434/ust_system"


class SecretSettings(BaseModel):
    """External API tokens and secrets."""

    model_config = ConfigDict(frozen=True)

    openrouter_api_key: str | None = None
    github_token: str | None = None
    github_repo: str = "Harikeshav-R/Understudy"
    slack_bot_token: str | None = None
    slack_channel_id: str | None = None
    pagerduty_token: str | None = None
    pagerduty_service_id: str | None = None
    pagerduty_webhook_secret: str | None = None
    pagerduty_routing_key: str | None = None
    datadog_api_key: str | None = None
    datadog_app_key: str | None = None


class Settings(BaseModel):
    """Root configuration model for Understudy."""

    model_config = ConfigDict(frozen=True)

    env: str = "development"
    log_level: str = "INFO"
    candidate_count: int = 3
    actuation_enabled: bool = True
    fault_injection_enabled: bool = False
    role: str = "agent"
    llm_model: str = "anthropic/claude-3.5-sonnet"
    embedding_model: str = "text-embedding-3-small"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    timeouts: TimeoutSettings = TimeoutSettings()
    cluster: ClusterSettings = ClusterSettings()
    scoring: ScoringSettings = ScoringSettings()
    endpoints: EndpointSettings = EndpointSettings()
    secrets: SecretSettings = SecretSettings()


_SETTINGS_INSTANCE: Settings | None = None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge two dictionaries."""
    merged = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple .env file into key-value pairs."""
    env_vars: dict[str, str] = {}
    if not path.exists():
        return env_vars
    if not path.is_file():
        raise ConfigError(f"Specified .env path at {path} is not a regular file")
    try:
        content = path.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'\"")
            env_vars[key] = val
    except Exception as exc:
        raise ConfigError(f"Failed to read .env file at {path}: {exc}") from exc
    return env_vars


def _load_yaml_file(path: Path, label: str = "config") -> dict[str, Any]:
    """Load and parse a YAML file into a dictionary, returning empty dict if missing."""
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
            return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:
        raise ConfigError(f"Failed to load {label} config at {path}: {exc}") from exc


def load_settings(
    default_config_path: Path | str | None = None,
    local_config_path: Path | str | None = None,
    env_file_path: Path | str | None = None,
) -> Settings:
    """Load settings by layering default.yaml, local.yaml, .env, and environment variables."""
    data: dict[str, Any] = {}

    # 1. default.yaml
    def_path = Path(default_config_path or "config/default.yaml")
    data = _deep_merge(data, _load_yaml_file(def_path, "default"))

    # 2. local.yaml
    loc_path = Path(local_config_path or "config/local.yaml")
    data = _deep_merge(data, _load_yaml_file(loc_path, "local"))

    # 3. .env file
    env_path = Path(env_file_path or ".env")
    env_file_vars = _read_env_file(env_path)

    # Combined env lookup (.env file overridden by os.environ)
    env_lookup: dict[str, str] = {**env_file_vars, **os.environ}

    # 4. Apply environment overrides
    env_str_fields = {
        "UNDERSTUDY_ENV": "env",
        "UNDERSTUDY_LOG_LEVEL": "log_level",
        "UNDERSTUDY_ROLE": "role",
        "UNDERSTUDY_LLM_MODEL": "llm_model",
        "UNDERSTUDY_EMBEDDING_MODEL": "embedding_model",
        "UNDERSTUDY_OPENROUTER_BASE_URL": "openrouter_base_url",
    }
    for env_key, field_name in env_str_fields.items():
        if env_key in env_lookup:
            data[field_name] = env_lookup[env_key]

    env_bool_fields = {
        "UNDERSTUDY_ACTUATION_ENABLED": "actuation_enabled",
        "UNDERSTUDY_FAULT_INJECTION_ENABLED": "fault_injection_enabled",
    }
    for env_key, field_name in env_bool_fields.items():
        if env_key in env_lookup:
            data[field_name] = env_lookup[env_key].lower() in ("true", "1", "yes")

    if "UNDERSTUDY_CANDIDATE_COUNT" in env_lookup:
        try:
            data["candidate_count"] = int(env_lookup["UNDERSTUDY_CANDIDATE_COUNT"])
        except ValueError as exc:
            val_str = env_lookup["UNDERSTUDY_CANDIDATE_COUNT"]
            raise ConfigError(f"Invalid integer for UNDERSTUDY_CANDIDATE_COUNT: {val_str}") from exc

    # Secrets
    secrets_data = data.setdefault("secrets", {})
    secret_keys = {
        "OPENROUTER_API_KEY": "openrouter_api_key",
        "GITHUB_TOKEN": "github_token",
        "GITHUB_REPO": "github_repo",
        "SLACK_BOT_TOKEN": "slack_bot_token",
        "SLACK_CHANNEL_ID": "slack_channel_id",
        "PAGERDUTY_TOKEN": "pagerduty_token",
        "PAGERDUTY_SERVICE_ID": "pagerduty_service_id",
        "PAGERDUTY_WEBHOOK_SECRET": "pagerduty_webhook_secret",
        "PAGERDUTY_ROUTING_KEY": "pagerduty_routing_key",
        "DATADOG_API_KEY": "datadog_api_key",
        "DATADOG_APP_KEY": "datadog_app_key",
    }
    for env_key, secret_field in secret_keys.items():
        if env_key in env_lookup:
            secrets_data[secret_field] = env_lookup[env_key]
        elif secret_field not in secrets_data:
            try:
                import importlib

                keyring_mod = importlib.import_module("keyring")
                keyring_val = keyring_mod.get_password("understudy", env_key)
                if keyring_val and isinstance(keyring_val, str):
                    secrets_data[secret_field] = keyring_val
            except Exception:
                pass

    try:
        return Settings.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"Configuration validation failed: {exc}") from exc


def get_settings() -> Settings:
    """Return the cached singleton Settings instance, initializing it if necessary."""
    global _SETTINGS_INSTANCE
    if _SETTINGS_INSTANCE is None:
        _SETTINGS_INSTANCE = load_settings()
    return _SETTINGS_INSTANCE


def reset_settings() -> None:
    """Reset cached singleton Settings instance (useful in tests)."""
    global _SETTINGS_INSTANCE
    _SETTINGS_INSTANCE = None
