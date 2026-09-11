"""Unit tests for deploy/prod manifests and dependencies.yaml."""

from pathlib import Path
from typing import Any

import yaml


def _load_manifests(file_path: Path) -> list[dict[str, Any]]:
    """Load all YAML documents from a manifest file."""
    assert file_path.exists(), f"{file_path} must exist"
    with file_path.open() as f:
        docs = list(yaml.safe_load_all(f))
    return [d for d in docs if d is not None]


def test_prod_namespace_manifest() -> None:
    """Validate ust-prod namespace definition."""
    path = Path("deploy/prod/namespace.yaml")
    docs = _load_manifests(path)
    assert len(docs) == 1
    ns = docs[0]
    assert ns["apiVersion"] == "v1"
    assert ns["kind"] == "Namespace"
    assert ns["metadata"]["name"] == "ust-prod"
    assert ns["metadata"]["labels"]["app.kubernetes.io/part-of"] == "understudy"
    assert ns["metadata"]["labels"]["environment"] == "production"


def test_prod_configmaps() -> None:
    """Validate feature-flags and app-config in deploy/prod/configmap.yaml."""
    path = Path("deploy/prod/configmap.yaml")
    docs = _load_manifests(path)
    assert len(docs) == 2

    by_name = {d["metadata"]["name"]: d for d in docs}
    assert "feature-flags" in by_name
    assert "app-config" in by_name

    ff = by_name["feature-flags"]
    assert ff["metadata"]["namespace"] == "ust-prod"
    assert "enable_recommendations" in ff["data"]
    assert "enable_fast_cache" in ff["data"]
    assert "enable_v2_catalogue" in ff["data"]

    app_cfg = by_name["app-config"]
    assert app_cfg["metadata"]["namespace"] == "ust-prod"
    assert app_cfg["data"]["ENVIRONMENT"] == "production"
    assert app_cfg["data"]["LOG_LEVEL"] == "INFO"


def test_dependencies_yaml_structure_and_dag() -> None:
    """Validate dependencies.yaml schema and service DAG per architecture §2.5."""
    path = Path("deploy/prod/dependencies.yaml")
    assert path.exists(), "dependencies.yaml must exist"
    with path.open() as f:
        data: dict[str, Any] = yaml.safe_load(f)

    assert data["version"] == "1"
    service_names = {s["name"] for s in data["services"]}
    expected_services = {"edge-gateway", "auth-service", "data-service", "worker"}
    assert service_names == expected_services

    # Verify declared edges match DAG
    declared_edges = {(e["source"], e["target"]) for e in data["edges"]}
    expected_edges = {
        ("edge-gateway", "auth-service"),
        ("edge-gateway", "data-service"),
        ("auth-service", "data-service"),
        ("worker", "data-service"),
    }
    assert declared_edges == expected_edges


def test_prod_demo_service_workloads() -> None:
    """Validate Deployments and Services for 4 demo services per architecture §2.10."""
    service_files = [
        "edge-gateway.yaml",
        "auth-service.yaml",
        "data-service.yaml",
        "worker.yaml",
    ]

    for fname in service_files:
        path = Path("deploy/prod") / fname
        docs = _load_manifests(path)
        deployments = [d for d in docs if d.get("kind") == "Deployment"]
        services = [d for d in docs if d.get("kind") == "Service"]

        assert len(deployments) == 1, f"{fname} must contain exactly one Deployment"
        assert len(services) == 1, f"{fname} must contain exactly one Service"

        deploy = deployments[0]
        assert deploy["metadata"]["namespace"] == "ust-prod"
        spec = deploy["spec"]["template"]["spec"]
        containers = spec["containers"]
        assert len(containers) == 1
        c = containers[0]

        # Verify ADR-003 / Architecture §2.10 memory limit <= 150 MB (150Mi)
        limits = c["resources"]["limits"]
        requests = c["resources"]["requests"]
        assert limits["memory"] == "150Mi", f"{fname} container memory limit must be 150Mi"
        assert requests["memory"] == "64Mi", f"{fname} container memory request must be 64Mi"

        # Verify role guard env var
        env_dict = {item["name"]: item["value"] for item in c.get("env", []) if "value" in item}
        assert env_dict.get("UNDERSTUDY_ROLE") == "prod"

        # Verify probes
        assert "livenessProbe" in c, f"{fname} must configure livenessProbe"
        assert "readinessProbe" in c, f"{fname} must configure readinessProbe"


def test_edge_gateway_service_ports() -> None:
    """Validate edge-gateway service exposes port 8080 for k3d mapping."""
    path = Path("deploy/prod/edge-gateway.yaml")
    docs = _load_manifests(path)
    svc = next(d for d in docs if d.get("kind") == "Service")
    ports = {p["port"]: p["targetPort"] for p in svc["spec"]["ports"]}
    assert 8080 in ports
    assert ports[8080] == 8000
    assert svc["spec"]["type"] == "LoadBalancer"


def test_data_service_ports() -> None:
    """Validate data-service exposes port 8081 for direct fault injection."""
    path = Path("deploy/prod/data-service.yaml")
    docs = _load_manifests(path)
    svc = next(d for d in docs if d.get("kind") == "Service")
    ports = {p["port"]: p["targetPort"] for p in svc["spec"]["ports"]}
    assert 8081 in ports
    assert ports[8081] == 8000
    assert svc["spec"]["type"] == "LoadBalancer"
