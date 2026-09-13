"""Layered configuration loader and frozen settings schema for services/ tunables.

Mirrors src/understudy/common/config.py's pattern (layered load, frozen pydantic
BaseModel per section, cached singleton with a reset hook for tests) as an
**independent** module: services/ and src/understudy/ are separate package trees with
zero imports between them, and import-linter's root_package = "understudy" does not
govern services/ (see AGENTS.md §5.2 -- src/understudy/common must stay
psycopg/fastapi-free, so it cannot be the home for these tunables either).

One deliberate divergence from config.py's pattern: fields here keep reading the exact
env var name they already used before this module existed (e.g.
AUTH_TOKEN_CACHE_MAX_SIZE, FAULT_INJECTION_SEED), documented per field below, rather
than a new UNDERSTUDY_<SECTION>__<FIELD> scheme. Those names are already wired into
deploy/prod/*.yaml's env/configMapKeyRef entries and loadgen's own CLI flag fallbacks;
renaming them would silently break already-deployed configuration for no benefit.
"""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict


class EdgeGatewaySettings(BaseModel):
    """Tunables for the edge-gateway service."""

    model_config = ConfigDict(frozen=True)

    http_timeout_seconds: float = 5.0  # env: HTTP_TIMEOUT_SECONDS


class AuthServiceSettings(BaseModel):
    """Tunables for the auth-service."""

    model_config = ConfigDict(frozen=True)

    token_cache_max_size: int = 1000  # env: AUTH_TOKEN_CACHE_MAX_SIZE


class WorkerSettings(BaseModel):
    """Tunables for the worker service."""

    model_config = ConfigDict(frozen=True)

    poll_interval_seconds: float = 1.0  # env: POLL_INTERVAL_SECONDS
    job_list_limit: int = 50  # env: WORKER_JOB_LIST_LIMIT


class LoadgenSettings(BaseModel):
    """Default configuration for the load generator, and its CLI flag fallbacks."""

    model_config = ConfigDict(frozen=True)

    rps: float = 20.0  # env: LOADGEN_RPS
    duration_seconds: float = 30.0  # env: LOADGEN_DURATION
    target_url: str = "http://localhost:8080"  # env: LOADGEN_TARGET_URL
    seed: int = 42  # env: LOADGEN_SEED
    auth_token: str = "valid-token"  # env: LOADGEN_AUTH_TOKEN
    http_timeout_seconds: float = 10.0  # env: LOADGEN_TIMEOUT
    max_connections: int = 200  # env: LOADGEN_MAX_CONNECTIONS
    max_keepalive_connections: int = 50  # env: LOADGEN_MAX_KEEPALIVE_CONNECTIONS
    p99_sla_ms: float = 400.0  # env: LOADGEN_P99_SLA_MS


class DbSettings(BaseModel):
    """Connection pool defaults shared by every service with a database."""

    model_config = ConfigDict(frozen=True)

    pool_min_size: int = 1  # env: DB_POOL_MIN_SIZE
    pool_max_size: int = 10  # env: DB_POOL_MAX_SIZE
    pool_timeout_seconds: float = 5.0  # env: DB_POOL_TIMEOUT_SECONDS


class FaultsSettings(BaseModel):
    """Tunables for the fault-injection engine (services/_common/faults.py)."""

    model_config = ConfigDict(frozen=True)

    injection_seed: int = 1337  # env: FAULT_INJECTION_SEED
    # env: FAULT_POOL_EXHAUSTION_GETCONN_TIMEOUT_SECONDS
    pool_exhaustion_getconn_timeout_seconds: float = 1.0


class MirrorGatewaySettings(BaseModel):
    """Tunables for the mirror-gateway service."""

    model_config = ConfigDict(frozen=True)

    target_prod_url: str = "http://edge-gateway.ust-prod:8080"  # env: TARGET_PROD_URL
    queue_maxsize: int = 1000  # env: MIRROR_QUEUE_MAXSIZE
    worker_timeout_seconds: float = 2.0  # env: MIRROR_WORKER_TIMEOUT_SECONDS
    http_timeout_seconds: float = 5.0  # env: MIRROR_HTTP_TIMEOUT_SECONDS
    max_connections: int = 200  # env: MIRROR_MAX_CONNECTIONS
    max_keepalive_connections: int = 50  # env: MIRROR_MAX_KEEPALIVE_CONNECTIONS


class MetricsSettings(BaseModel):
    """Tunables for Prometheus metric collection. No env override: a bucket list isn't
    a good fit for a single scalar env var; edit config/services.yaml directly."""

    model_config = ConfigDict(frozen=True)

    histogram_buckets: tuple[float, ...] = (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.4,
        0.5,
        1.0,
        2.0,
        5.0,
    )


class ServicesSettings(BaseModel):
    """Root configuration model for services/."""

    model_config = ConfigDict(frozen=True)

    edge_gateway: EdgeGatewaySettings = EdgeGatewaySettings()
    auth_service: AuthServiceSettings = AuthServiceSettings()
    worker: WorkerSettings = WorkerSettings()
    loadgen: LoadgenSettings = LoadgenSettings()
    db: DbSettings = DbSettings()
    faults: FaultsSettings = FaultsSettings()
    mirror_gateway: MirrorGatewaySettings = MirrorGatewaySettings()
    metrics: MetricsSettings = MetricsSettings()


# (section, field) -> (env var name, caster). The env var name is what each field
# already read before this module existed (see per-field comments above); a few fields
# (worker.job_list_limit, db.*, faults.pool_exhaustion_getconn_timeout_seconds,
# loadgen.max_connections/max_keepalive_connections/p99_sla_ms) had no env var before
# and are given one here for the first time.
_ENV_OVERRIDES: dict[tuple[str, str], tuple[str, type]] = {
    ("edge_gateway", "http_timeout_seconds"): ("HTTP_TIMEOUT_SECONDS", float),
    ("auth_service", "token_cache_max_size"): ("AUTH_TOKEN_CACHE_MAX_SIZE", int),
    ("worker", "poll_interval_seconds"): ("POLL_INTERVAL_SECONDS", float),
    ("worker", "job_list_limit"): ("WORKER_JOB_LIST_LIMIT", int),
    ("loadgen", "rps"): ("LOADGEN_RPS", float),
    ("loadgen", "duration_seconds"): ("LOADGEN_DURATION", float),
    ("loadgen", "target_url"): ("LOADGEN_TARGET_URL", str),
    ("loadgen", "seed"): ("LOADGEN_SEED", int),
    ("loadgen", "auth_token"): ("LOADGEN_AUTH_TOKEN", str),
    ("loadgen", "http_timeout_seconds"): ("LOADGEN_TIMEOUT", float),
    ("loadgen", "max_connections"): ("LOADGEN_MAX_CONNECTIONS", int),
    ("loadgen", "max_keepalive_connections"): ("LOADGEN_MAX_KEEPALIVE_CONNECTIONS", int),
    ("loadgen", "p99_sla_ms"): ("LOADGEN_P99_SLA_MS", float),
    ("db", "pool_min_size"): ("DB_POOL_MIN_SIZE", int),
    ("db", "pool_max_size"): ("DB_POOL_MAX_SIZE", int),
    ("db", "pool_timeout_seconds"): ("DB_POOL_TIMEOUT_SECONDS", float),
    ("faults", "injection_seed"): ("FAULT_INJECTION_SEED", int),
    ("faults", "pool_exhaustion_getconn_timeout_seconds"): (
        "FAULT_POOL_EXHAUSTION_GETCONN_TIMEOUT_SECONDS",
        float,
    ),
    ("mirror_gateway", "target_prod_url"): ("TARGET_PROD_URL", str),
    ("mirror_gateway", "queue_maxsize"): ("MIRROR_QUEUE_MAXSIZE", int),
    ("mirror_gateway", "worker_timeout_seconds"): ("MIRROR_WORKER_TIMEOUT_SECONDS", float),
    ("mirror_gateway", "http_timeout_seconds"): ("MIRROR_HTTP_TIMEOUT_SECONDS", float),
    ("mirror_gateway", "max_connections"): ("MIRROR_MAX_CONNECTIONS", int),
    ("mirror_gateway", "max_keepalive_connections"): ("MIRROR_MAX_KEEPALIVE_CONNECTIONS", int),
}


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """Load and parse a YAML file into a dictionary, returning empty dict if missing."""
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
        return loaded if isinstance(loaded, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge two dictionaries."""
    merged = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def load_services_settings(
    config_path: Path | str | None = None,
    local_config_path: Path | str | None = None,
) -> ServicesSettings:
    """Load services/ tunables by layering config/services.yaml, an optional
    config/services.local.yaml (gitignored, for a developer's own overrides), then
    each field's environment variable."""
    data: dict[str, Any] = {}
    data = _deep_merge(data, _load_yaml_file(Path(config_path or "config/services.yaml")))
    data = _deep_merge(
        data, _load_yaml_file(Path(local_config_path or "config/services.local.yaml"))
    )

    for (section, field), (env_key, caster) in _ENV_OVERRIDES.items():
        if env_key not in os.environ:
            continue
        raw = os.environ[env_key]
        try:
            value: Any = caster(raw)
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_key}: {raw!r}") from exc
        data.setdefault(section, {})[field] = value

    return ServicesSettings.model_validate(data)


_SETTINGS_INSTANCE: ServicesSettings | None = None


def get_services_settings() -> ServicesSettings:
    """Return the cached singleton ServicesSettings instance, loading it if necessary."""
    global _SETTINGS_INSTANCE
    if _SETTINGS_INSTANCE is None:
        _SETTINGS_INSTANCE = load_services_settings()
    return _SETTINGS_INSTANCE


def reset_services_settings() -> None:
    """Reset the cached singleton ServicesSettings instance (useful in tests)."""
    global _SETTINGS_INSTANCE
    _SETTINGS_INSTANCE = None
