"""Unit tests for deploy/system manifests and resource budgets."""

from pathlib import Path
from typing import Any

import yaml


def _load_manifests(file_path: Path) -> list[dict[str, Any]]:
    """Load all YAML documents from a manifest file."""
    assert file_path.exists(), f"{file_path} must exist"
    with file_path.open() as f:
        docs = list(yaml.safe_load_all(f))
    return [d for d in docs if d is not None]


def test_system_namespace_manifest() -> None:
    """Validate ust-system namespace definition."""
    path = Path("deploy/system/namespace.yaml")
    docs = _load_manifests(path)
    assert len(docs) == 1
    ns = docs[0]
    assert ns["apiVersion"] == "v1"
    assert ns["kind"] == "Namespace"
    assert ns["metadata"]["name"] == "ust-system"
    assert ns["metadata"]["labels"]["app.kubernetes.io/part-of"] == "understudy"
    assert ns["metadata"]["labels"]["environment"] == "system"


def test_prod_postgres_manifest() -> None:
    """Validate prod-postgres per ADR-009 (in ust-prod) and §2.10 (400 MB limit)."""
    path = Path("deploy/system/prod-postgres.yaml")
    docs = _load_manifests(path)
    deploy = next(d for d in docs if d.get("kind") == "Deployment")
    svc = next(d for d in docs if d.get("kind") == "Service")

    # Must reside in ust-prod per ADR-009 / ADR-002
    assert deploy["metadata"]["namespace"] == "ust-prod"
    assert svc["metadata"]["namespace"] == "ust-prod"

    c = deploy["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == "postgres:16-alpine"
    assert c["resources"]["limits"]["memory"] == "400Mi"
    assert "livenessProbe" in c
    assert "readinessProbe" in c

    # Service port 5432
    assert svc["spec"]["type"] == "LoadBalancer"
    port_entry = svc["spec"]["ports"][0]
    assert port_entry["port"] == 5432
    assert port_entry["targetPort"] == 5432


def test_twin_postgres_manifest() -> None:
    """Validate twin-postgres per ADR-009 (in ust-system) and §2.10 (700 MB limit)."""
    path = Path("deploy/system/twin-postgres.yaml")
    docs = _load_manifests(path)
    deploy = next(d for d in docs if d.get("kind") == "Deployment")
    svc = next(d for d in docs if d.get("kind") == "Service")

    assert deploy["metadata"]["namespace"] == "ust-system"
    assert svc["metadata"]["namespace"] == "ust-system"

    c = deploy["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == "postgres:16-alpine"
    assert c["resources"]["limits"]["memory"] == "700Mi"
    assert "livenessProbe" in c
    assert "readinessProbe" in c

    # Exposes port 5433 (host)
    assert svc["spec"]["type"] == "LoadBalancer"
    ports = {p["name"]: p["port"] for p in svc["spec"]["ports"]}
    assert ports.get("host-port") == 5433


def test_system_postgres_manifest() -> None:
    """Validate system-postgres per ADR-030 (with pgvector) and §2.10 (400 MB limit)."""
    path = Path("deploy/system/system-postgres.yaml")
    docs = _load_manifests(path)
    deploy = next(d for d in docs if d.get("kind") == "Deployment")
    svc = next(d for d in docs if d.get("kind") == "Service")
    cm = next(d for d in docs if d.get("kind") == "ConfigMap")

    assert deploy["metadata"]["namespace"] == "ust-system"
    assert svc["metadata"]["namespace"] == "ust-system"

    # Init script creates vector extension
    assert "CREATE EXTENSION IF NOT EXISTS vector;" in cm["data"]["init.sql"]

    c = deploy["spec"]["template"]["spec"]["containers"][0]
    assert "pgvector" in c["image"]
    assert c["resources"]["limits"]["memory"] == "400Mi"
    assert "livenessProbe" in c
    assert "readinessProbe" in c

    # Exposes port 5434 (host)
    assert svc["spec"]["type"] == "LoadBalancer"
    ports = {p["name"]: p["port"] for p in svc["spec"]["ports"]}
    assert ports.get("host-port") == 5434


def test_prometheus_manifest() -> None:
    """Validate prometheus scrape configs and §2.10 resource limits (700 MB)."""
    path = Path("deploy/system/prometheus.yaml")
    docs = _load_manifests(path)
    deploy = next(d for d in docs if d.get("kind") == "Deployment")
    svc = next(d for d in docs if d.get("kind") == "Service")
    cm = next(d for d in docs if d.get("kind") == "ConfigMap")

    assert deploy["metadata"]["namespace"] == "ust-system"
    assert svc["metadata"]["namespace"] == "ust-system"

    c = deploy["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"]["limits"]["memory"] == "700Mi"

    # Scrape configs must cover all namespaces
    prom_cfg = yaml.safe_load(cm["data"]["prometheus.yml"])
    job_names = [j["job_name"] for j in prom_cfg["scrape_configs"]]
    assert "ust-prod" in job_names
    assert "ust-system" in job_names
    assert "kubernetes-pods" in job_names

    # Service on 9090
    assert svc["spec"]["type"] == "LoadBalancer"
    assert svc["spec"]["ports"][0]["port"] == 9090


def test_loki_and_promtail_manifests() -> None:
    """Validate loki and promtail manifests and §2.10 limits."""
    # Loki
    loki_path = Path("deploy/system/loki.yaml")
    loki_docs = _load_manifests(loki_path)
    loki_deploy = next(d for d in loki_docs if d.get("kind") == "Deployment")
    loki_svc = next(d for d in loki_docs if d.get("kind") == "Service")
    loki_c = loki_deploy["spec"]["template"]["spec"]["containers"][0]
    assert loki_c["resources"]["limits"]["memory"] == "400Mi"
    assert loki_svc["spec"]["type"] == "LoadBalancer"
    assert loki_svc["spec"]["ports"][0]["port"] == 3100

    # Promtail
    promtail_path = Path("deploy/system/promtail.yaml")
    promtail_docs = _load_manifests(promtail_path)
    promtail_ds = next(d for d in promtail_docs if d.get("kind") == "DaemonSet")
    promtail_c = promtail_ds["spec"]["template"]["spec"]["containers"][0]
    assert promtail_c["resources"]["limits"]["memory"] == "100Mi"
