"""Unit tests for twin manifest rendering (build-plan step A2.2).

Validates:
- Namespace, ServiceAccount, RoleBinding, NetworkPolicy, ConfigMap, Service, Deployment rendering
- Invariant K6 & ADR-014: egress containment to ust-prod and internet
- ADR-009: database DSN rewriting to twin-postgres
- Image digest pinning enforcement (sha256 digests from A2.1)
- UNDERSTUDY_ROLE=twin injection and rewriting
- External URL redirection to egress-stub
- 100% line and branch coverage
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from understudy.common.config import Settings
from understudy.common.errors import FleetError
from understudy.fleet.api import ManifestRenderer, TwinManifestBundle, TwinManifestRenderer
from understudy.fleet.models import (
    ClusterWorkloadSnapshot,
    ContainerSnapshot,
    EnvVar,
    ResourceSpec,
    WorkloadSnapshot,
)
from understudy.fleet.render import (
    DEFAULT_EGRESS_STUB_URL,
    DEFAULT_TWIN_POSTGRES_HOST,
    build_twin_namespace,
    render_twin_manifests,
    rewrite_database_dsn,
    rewrite_url,
    sanitize_database_name,
)


def _make_sample_snapshot() -> ClusterWorkloadSnapshot:
    """Create a realistic production cluster snapshot with four services."""
    now = datetime.now(UTC)

    auth_container = ContainerSnapshot(
        name="auth-service",
        image_tag="localhost:5001/auth-service:good",
        image_digest="sha256:1111111111111111111111111111111111111111111111111111111111111111",
        pinned_image="localhost:5001/auth-service@sha256:1111111111111111111111111111111111111111111111111111111111111111",
        resources=ResourceSpec(requests={"cpu": "50m", "memory": "64Mi"}, limits={"cpu": "200m"}),
        env=[
            EnvVar(name="UNDERSTUDY_ROLE", value="prod"),
            EnvVar(name="DATABASE_URL", value="postgresql://postgres@prod-postgres:5432/ust_prod"),
            EnvVar(
                name="FAULT_INJECTION_SEED",
                value_from={
                    "configMapKeyRef": {"name": "app-config", "key": "FAULT_INJECTION_SEED"}
                },
            ),
        ],
        ports=[{"containerPort": 8000, "name": "http", "protocol": "TCP"}],
        liveness_probe={"httpGet": {"path": "/healthz", "port": 8000}},
        readiness_probe={"httpGet": {"path": "/readyz", "port": 8000}},
    )

    data_container = ContainerSnapshot(
        name="data-service",
        image_tag="localhost:5001/data-service:good",
        image_digest="sha256:2222222222222222222222222222222222222222222222222222222222222222",
        pinned_image="localhost:5001/data-service@sha256:2222222222222222222222222222222222222222222222222222222222222222",
        resources=ResourceSpec(requests={"cpu": "50m"}, limits={"memory": "150Mi"}),
        env=[
            EnvVar(name="UNDERSTUDY_ROLE", value="prod"),
            EnvVar(name="DATABASE_URL", value="postgresql://postgres@prod-postgres:5432/ust_prod"),
            EnvVar(name="DATA_SERVICE_VARIANT", value="good"),
        ],
        ports=[{"containerPort": 8000, "name": "http", "protocol": "TCP"}],
    )

    edge_container = ContainerSnapshot(
        name="edge-gateway",
        image_tag="localhost:5001/edge-gateway:good",
        image_digest="sha256:3333333333333333333333333333333333333333333333333333333333333333",
        pinned_image="localhost:5001/edge-gateway@sha256:3333333333333333333333333333333333333333333333333333333333333333",
        resources=ResourceSpec(requests={"cpu": "50m"}),
        env=[
            EnvVar(name="UNDERSTUDY_ROLE", value="prod"),
            EnvVar(name="AUTH_SERVICE_URL", value="http://auth-service:8000"),
            EnvVar(name="DATA_SERVICE_URL", value="http://data-service:8000"),
            EnvVar(name="EXTERNAL_BILLING_URL", value="https://api.stripe.com/v1/charges?limit=10"),
            EnvVar(name="LITERAL_IP_TARGET", value="http://192.168.1.10:9000/report"),
            EnvVar(name="PLAIN_ENV", value="plain-val"),
            EnvVar(
                name="EXTERNAL_WITH_VALUE_FROM",
                value="https://api.external.com/v1",
                value_from={"fieldRef": {"fieldPath": "metadata.name"}},
            ),
        ],
        ports=[{"containerPort": 8000, "name": "http", "protocol": "TCP"}],
    )

    worker_container = ContainerSnapshot(
        name="worker",
        image_tag="localhost:5001/worker:good",
        image_digest="sha256:4444444444444444444444444444444444444444444444444444444444444444",
        pinned_image="localhost:5001/worker@sha256:4444444444444444444444444444444444444444444444444444444444444444",
        resources=ResourceSpec(),
        env=[
            EnvVar(name="DATABASE_URL", value="postgresql://postgres@prod-postgres:5432/ust_prod"),
            EnvVar(name="POLL_INTERVAL_SECONDS", value="1.0"),
        ],
        ports=[],
    )

    workloads = {
        "auth-service": WorkloadSnapshot(
            name="auth-service",
            namespace="ust-prod",
            component="auth",
            labels={"app": "auth-service", "app.kubernetes.io/name": "auth-service"},
            annotations={"prometheus.io/scrape": "true"},
            replicas=1,
            containers=[auth_container],
            config_map_refs=["app-config"],
            volumes=[{"name": "vol-data", "emptyDir": {}}],
        ),
        "data-service": WorkloadSnapshot(
            name="data-service",
            namespace="ust-prod",
            component="data",
            labels={"app": "data-service"},
            replicas=1,
            containers=[data_container],
        ),
        "edge-gateway": WorkloadSnapshot(
            name="edge-gateway",
            namespace="ust-prod",
            component="gateway",
            labels={"app": "edge-gateway"},
            replicas=1,
            containers=[edge_container],
        ),
        "worker": WorkloadSnapshot(
            name="worker",
            namespace="ust-prod",
            component="worker",
            labels={"app": "worker"},
            replicas=1,
            containers=[worker_container],
        ),
    }

    config_maps = {
        "app-config": {
            "ENVIRONMENT": "production",
            "LOG_LEVEL": "INFO",
            "FAULT_INJECTION_SEED": "1337",
            "NOTIFICATION_WEBHOOK": "https://hooks.slack.com/services/T00/B00/X00",
            "INTERNAL_DB_CONFIG": "postgresql://postgres@prod-postgres:5432/ust_prod",
        },
        "feature-flags": {
            "enable_recommendations": "true",
            "enable_fast_cache": "true",
        },
    }

    return ClusterWorkloadSnapshot(
        namespace="ust-prod",
        workloads=workloads,
        config_maps=config_maps,
        captured_at=now,
    )


def test_protocol_conformance() -> None:
    """Verify TwinManifestRenderer conforms to ManifestRenderer Protocol."""
    renderer = TwinManifestRenderer()
    assert isinstance(renderer, ManifestRenderer)


def test_sanitize_database_name() -> None:
    """Validate database identifier derivation and PostgreSQL hygiene."""
    assert sanitize_database_name("inc_test", 0) == "twin_inc_test_0"
    assert sanitize_database_name("inc-abc-123", 2) == "twin_inc_abc_123_2"

    with pytest.raises(FleetError, match="Incident ID cannot be empty"):
        sanitize_database_name("   ", 0)

    with pytest.raises(FleetError, match="Candidate index must be non-negative"):
        sanitize_database_name("inc_test", -1)


def test_build_twin_namespace() -> None:
    """Validate twin namespace name synthesis."""
    assert build_twin_namespace("ust-twin", "inc_test", 0) == "ust-twin-inc_test-0"
    assert build_twin_namespace("ust-twin", "inc-42", 1) == "ust-twin-inc-42-1"

    with pytest.raises(FleetError, match="Incident ID cannot be empty"):
        build_twin_namespace("ust-twin", "", 0)

    with pytest.raises(FleetError, match="Candidate index must be non-negative"):
        build_twin_namespace("ust-twin", "inc_test", -2)


def test_rewrite_database_dsn() -> None:
    """Validate database DSN rewriting across various URI shapes."""
    # 1. Default none
    dsn = rewrite_database_dsn(None, "twin_inc_0")
    assert dsn == f"postgresql://postgres@{DEFAULT_TWIN_POSTGRES_HOST}:5432/twin_inc_0"

    # 2. Standard postgresql DSN
    orig = "postgresql://postgres@prod-postgres:5432/ust_prod"
    dsn = rewrite_database_dsn(orig, "twin_inc_0")
    assert dsn == f"postgresql://postgres@{DEFAULT_TWIN_POSTGRES_HOST}:5432/twin_inc_0"

    # 3. DSN with password
    orig_auth = "postgresql://myuser:secret123@prod-postgres:5432/ust_prod"
    dsn_auth = rewrite_database_dsn(orig_auth, "twin_inc_0")
    assert dsn_auth == f"postgresql://myuser:secret123@{DEFAULT_TWIN_POSTGRES_HOST}:5432/twin_inc_0"

    # 4. DSN with query parameters
    orig_q = (
        "postgresql://postgres@prod-postgres:5432/ust_prod?sslmode=disable&application_name=test"
    )
    dsn_q = rewrite_database_dsn(orig_q, "twin_inc_0")
    assert dsn_q.startswith(f"postgresql://postgres@{DEFAULT_TWIN_POSTGRES_HOST}:5432/twin_inc_0?")
    assert "sslmode=disable" in dsn_q
    assert "application_name=test" in dsn_q


def test_rewrite_database_dsn_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify exception fallback branch in rewrite_database_dsn."""
    import understudy.fleet.render as render_mod

    def mock_broken_split(_u: str) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(render_mod, "urlsplit", mock_broken_split)
    fallback_dsn = rewrite_database_dsn("postgresql://host/db", "twin_inc_0")
    assert fallback_dsn == f"postgresql://postgres@{DEFAULT_TWIN_POSTGRES_HOST}:5432/twin_inc_0"


def test_rewrite_url_rules() -> None:
    """Verify URL rewriting matrix for twin isolation."""
    target_ns = "ust-twin-inc_test-0"
    prod_ns = "ust-prod"
    known = {"auth-service", "data-service", "edge-gateway", "worker"}

    # Non-HTTP string is unchanged
    assert rewrite_url("plain_value", target_ns, prod_ns, known) == "plain_value"

    # In-namespace unqualified service is preserved
    assert (
        rewrite_url("http://auth-service:8000/validate", target_ns, prod_ns, known)
        == "http://auth-service:8000/validate"
    )

    # Qualified ust-prod URL is redirected to twin namespace
    assert (
        rewrite_url("http://data-service.ust-prod:8000/items", target_ns, prod_ns, known)
        == f"http://data-service.{target_ns}:8000/items"
    )
    assert (
        rewrite_url("http://data-service.ust-prod/items", target_ns, prod_ns, known)
        == f"http://data-service.{target_ns}/items"
    )

    # Cluster system namespace is preserved
    assert (
        rewrite_url("http://prometheus.ust-system:9090/api/v1/query", target_ns, prod_ns, known)
        == "http://prometheus.ust-system:9090/api/v1/query"
    )
    assert (
        rewrite_url("http://kube-dns.kube-system:53", target_ns, prod_ns, known)
        == "http://kube-dns.kube-system:53"
    )

    # IP literal destination is blocked and redirected to egress-stub
    assert (
        rewrite_url("http://192.168.1.100:9000/webhook?token=xyz", target_ns, prod_ns, known)
        == f"{DEFAULT_EGRESS_STUB_URL}/webhook?token=xyz"
    )

    # External internet domain is redirected to egress-stub with path & query preserved
    assert (
        rewrite_url("https://api.stripe.com/v1/charges?limit=10", target_ns, prod_ns, known)
        == f"{DEFAULT_EGRESS_STUB_URL}/v1/charges?limit=10"
    )
    assert (
        rewrite_url("https://api.github.com", target_ns, prod_ns, known) == DEFAULT_EGRESS_STUB_URL
    )

    # Missing hostname in URL redirects to egress stub
    assert rewrite_url("http:///no-host", target_ns, prod_ns, known) == DEFAULT_EGRESS_STUB_URL


def test_rewrite_url_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify exception fallback branch in rewrite_url."""
    import understudy.fleet.render as render_mod

    def mock_broken_split(_u: str) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(render_mod, "urlsplit", mock_broken_split)
    assert (
        render_mod.rewrite_url("http://some-url.com", "ns", "prod", set())
        == DEFAULT_EGRESS_STUB_URL
    )


def test_find_default_policies_dir_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify _find_default_policies_dir when no parent directory contains deploy/policies."""
    from understudy.fleet.render import _find_default_policies_dir

    monkeypatch.setattr(Path, "is_dir", lambda _self: False)
    assert _find_default_policies_dir() == Path("deploy/policies")


def test_render_twin_manifests_bundle() -> None:
    """Verify end-to-end twin manifest rendering with realistic snapshot."""
    snapshot = _make_sample_snapshot()
    bundle = render_twin_manifests(
        snapshot=snapshot,
        incident_id="inc_test",
        candidate_index=0,
    )

    assert bundle.twin_id == "twin_inc_test_0"
    assert bundle.incident_id == "inc_test"
    assert bundle.candidate_index == 0
    assert bundle.namespace == "ust-twin-inc_test-0"
    assert bundle.database_name == "twin_inc_test_0"
    assert "twin-postgres.ust-system:5432/twin_inc_test_0" in bundle.database_dsn

    # 1. Namespace
    ns = bundle.namespace_manifest
    assert ns is not None
    assert ns["metadata"]["name"] == "ust-twin-inc_test-0"
    assert ns["metadata"]["labels"]["environment"] == "twin"
    assert ns["metadata"]["labels"]["understudy.dev/incident"] == "inc_test"
    assert ns["metadata"]["labels"]["understudy.dev/candidate"] == "0"

    # 2. RBAC
    rbac = bundle.rbac_manifests
    assert len(rbac) == 2
    sa = next(d for d in rbac if d["kind"] == "ServiceAccount")
    rb = next(d for d in rbac if d["kind"] == "RoleBinding")
    assert sa["metadata"]["name"] == "understudy-twin"
    assert sa["metadata"]["namespace"] == "ust-twin-inc_test-0"
    assert rb["metadata"]["name"] == "understudy-twin"
    assert rb["metadata"]["namespace"] == "ust-twin-inc_test-0"
    assert rb["roleRef"]["name"] == "understudy-twin"
    assert rb["subjects"][0]["namespace"] == "ust-twin-inc_test-0"

    # 3. NetworkPolicy (Golden spec & Invariant K6)
    np = bundle.network_policy_manifest
    assert np is not None
    assert np["metadata"]["name"] == "twin-egress-containment"
    assert np["metadata"]["namespace"] == "ust-twin-inc_test-0"
    assert np["metadata"]["labels"]["environment"] == "twin"
    egress_rules = np["spec"]["egress"]
    assert len(egress_rules) == 3
    # Verify no ust-prod in egress
    assert "ust-prod" not in str(egress_rules)
    assert "0.0.0.0" not in str(egress_rules)

    # 4. ConfigMaps
    cms = bundle.config_map_manifests
    assert len(cms) == 2
    cm_map = {c["metadata"]["name"]: c for c in cms}
    assert "app-config" in cm_map
    app_cfg = cm_map["app-config"]["data"]
    assert app_cfg["ENVIRONMENT"] == "twin"  # Rewritten from production
    assert app_cfg["FAULT_INJECTION_SEED"] == "1337"
    assert app_cfg["NOTIFICATION_WEBHOOK"].startswith(DEFAULT_EGRESS_STUB_URL)
    assert "twin-postgres.ust-system:5432/twin_inc_test_0" in app_cfg["INTERNAL_DB_CONFIG"]

    # 5. Services
    svcs = bundle.service_manifests
    # edge-gateway, data-service, auth-service have ports (worker has none)
    assert len(svcs) == 3
    svc_map = {s["metadata"]["name"]: s for s in svcs}
    assert "edge-gateway" in svc_map
    gw_ports = {p["port"] for p in svc_map["edge-gateway"]["spec"]["ports"]}
    assert 8000 in gw_ports
    assert 8080 in gw_ports

    assert "data-service" in svc_map
    data_ports = {p["port"] for p in svc_map["data-service"]["spec"]["ports"]}
    assert 8000 in data_ports
    assert 8081 in data_ports

    # 6. Deployments
    deps = bundle.deployment_manifests
    assert len(deps) == 4
    dep_map = {d["metadata"]["name"]: d for d in deps}

    # Verify edge-gateway env rewriting
    gw_dep = dep_map["edge-gateway"]
    assert gw_dep["metadata"]["namespace"] == "ust-twin-inc_test-0"
    gw_c = gw_dep["spec"]["template"]["spec"]["containers"][0]
    # Check pinned digest used
    assert "@sha256:333333333333" in gw_c["image"]
    gw_env = {e["name"]: e.get("value") for e in gw_c["env"]}
    assert gw_env["UNDERSTUDY_ROLE"] == "twin"
    assert gw_env["AUTH_SERVICE_URL"] == "http://auth-service:8000"
    assert gw_env["DATA_SERVICE_URL"] == "http://data-service:8000"
    assert gw_env["EXTERNAL_BILLING_URL"].startswith(DEFAULT_EGRESS_STUB_URL)
    assert gw_env["LITERAL_IP_TARGET"].startswith(DEFAULT_EGRESS_STUB_URL)

    # Verify worker got UNDERSTUDY_ROLE injected even though it was absent in snapshot
    worker_dep = dep_map["worker"]
    worker_c = worker_dep["spec"]["template"]["spec"]["containers"][0]
    worker_env = {e["name"]: e.get("value") for e in worker_c["env"]}
    assert worker_env["UNDERSTUDY_ROLE"] == "twin"
    assert "twin-postgres.ust-system:5432/twin_inc_test_0" in str(worker_env["DATABASE_URL"])

    # 7. Helper accessors and YAML dump
    assert len(bundle.all_manifests()) == len(bundle.manifests)
    yaml_text = bundle.to_yaml()
    loaded_docs = list(yaml.safe_load_all(yaml_text))
    assert len(loaded_docs) == len(bundle.manifests)


def test_render_twin_manifests_custom_db_name_and_settings() -> None:
    """Verify rendering with custom database name and custom settings."""
    snapshot = _make_sample_snapshot()
    settings = Settings(
        cluster={"twin_namespace_prefix": "custom-twin", "prod_namespace": "ust-prod"}  # type: ignore[arg-type]
    )

    bundle = render_twin_manifests(
        snapshot=snapshot,
        incident_id="inc_custom",
        candidate_index=1,
        database_name="custom_db_1",
        settings=settings,
    )

    assert bundle.namespace == "custom-twin-inc_custom-1"
    assert bundle.database_name == "custom_db_1"
    assert bundle.database_dsn.endswith("/custom_db_1")


def test_render_fallback_policies_when_templates_missing(tmp_path: Path) -> None:
    """Verify golden fallback specs when policies directory does not contain templates."""
    snapshot = _make_sample_snapshot()
    empty_policies_dir = tmp_path / "empty_policies"
    empty_policies_dir.mkdir()

    renderer = TwinManifestRenderer(policies_dir=empty_policies_dir)
    bundle = renderer.render(snapshot, "inc_fallback", 0)

    # Should still render valid NetworkPolicy and RBAC via built-in golden fallback
    assert bundle.network_policy_manifest is not None
    assert bundle.network_policy_manifest["metadata"]["name"] == "twin-egress-containment"
    assert len(bundle.rbac_manifests) == 2


def test_render_validation_errors() -> None:
    """Verify input validation in renderer."""
    snapshot = _make_sample_snapshot()
    renderer = TwinManifestRenderer()

    with pytest.raises(FleetError, match="incident_id cannot be empty"):
        renderer.render(snapshot, "", 0)

    with pytest.raises(FleetError, match="candidate_index must be >= 0"):
        renderer.render(snapshot, "inc_test", -1)


def test_render_fails_if_container_missing_pinned_image() -> None:
    """Verify FleetError if a container lacks a pinned image digest."""
    now = datetime.now(UTC)
    broken_container = ContainerSnapshot(
        name="bad-container",
        image_tag="bad:latest",
        image_digest="",
        pinned_image="",
        resources=ResourceSpec(),
    )
    broken_workload = WorkloadSnapshot(
        name="bad-workload",
        namespace="ust-prod",
        containers=[broken_container],
    )
    broken_snapshot = ClusterWorkloadSnapshot(
        namespace="ust-prod",
        workloads={"bad-workload": broken_workload},
        config_maps={},
        captured_at=now,
    )

    renderer = TwinManifestRenderer()
    with pytest.raises(FleetError, match="has no pinned image digest"):
        renderer.render(broken_snapshot, "inc_test", 0)


def test_bundle_empty_properties() -> None:
    """Verify helper property fallbacks when manifests list is empty."""
    bundle = TwinManifestBundle(
        twin_id="twin_0",
        incident_id="inc",
        candidate_index=0,
        namespace="ns",
        database_name="db",
        database_dsn="dsn",
        manifests=[],
    )
    assert bundle.namespace_manifest is None
    assert bundle.network_policy_manifest is None
    assert bundle.rbac_manifests == []
    assert bundle.config_map_manifests == []
    assert bundle.service_manifests == []
    assert bundle.deployment_manifests == []
