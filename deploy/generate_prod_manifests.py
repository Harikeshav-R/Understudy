#!/usr/bin/env python3
"""Generate the four deploy/prod/*.yaml service manifests from one shared template.

deploy/prod/auth-service.yaml, data-service.yaml, edge-gateway.yaml, and worker.yaml
were four independently hand-written, near-identical Deployment+Service pairs
(identical label scheme, Prometheus scrape annotations, replicas, imagePullPolicy,
resources, probe shape, UNDERSTUDY_ROLE/FAULT_INJECTION_SEED env; differing only in
each service's own env vars, Service type/ports, and
app.kubernetes.io/component) with nothing keeping them from drifting apart.

See generate_postgres_manifests.py for the same convention applied to the postgres
manifests, and deploy/_manifest_gen_common.py for the shared PyYAML dumping helpers
both scripts use -- neither script imports the other; they're independent generators
that happen to share dumping conventions and this module's `--check` philosophy.

Manifests are committed, plain YAML -- nothing in `make up`/`kubectl apply` runs this
script at deploy time. Run it in one of two modes:

    python deploy/generate_prod_manifests.py          # (re)write the 4 files
    python deploy/generate_prod_manifests.py --check   # exit 1 on drift, no writes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PROD_DIR = REPO_ROOT / "deploy" / "prod"

if __package__ in (None, ""):
    sys.path.insert(0, str(REPO_ROOT))

from deploy._manifest_gen_common import PART_OF_LABEL  # noqa: E402
from deploy._manifest_gen_common import dump_documents  # noqa: E402

_RESOURCES = {
    "requests": {"cpu": "50m", "memory": "64Mi"},
    "limits": {"cpu": "200m", "memory": "150Mi"},
}


def _probe(path: str) -> dict[str, Any]:
    return {
        "httpGet": {"path": path, "port": 8000},
        "initialDelaySeconds": 2,
        "periodSeconds": 5,
    }


def _fault_seed_env() -> dict[str, Any]:
    return {
        "name": "FAULT_INJECTION_SEED",
        "valueFrom": {"configMapKeyRef": {"name": "app-config", "key": "FAULT_INJECTION_SEED"}},
    }


def build_manifest(
    *,
    name: str,
    component: str,
    custom_env: list[dict[str, Any]],
    service_type: str,
    service_ports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the [Deployment, Service] document pair for one prod service.

    Every service's env list is [UNDERSTUDY_ROLE, *custom_env, FAULT_INJECTION_SEED]
    -- the role guard and fault seed are common to all four; custom_env is what
    actually differs per service (DB URL, timeouts, peer service URLs, ...).
    """
    env: list[dict[str, Any]] = [
        {"name": "UNDERSTUDY_ROLE", "value": "prod"},
        *custom_env,
        _fault_seed_env(),
    ]

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": "ust-prod",
            "labels": {
                "app": name,
                "app.kubernetes.io/name": name,
                **PART_OF_LABEL,
                "app.kubernetes.io/component": component,
            },
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {
                    "labels": {
                        "app": name,
                        "app.kubernetes.io/name": name,
                        **PART_OF_LABEL,
                        "understudy.dev/scrape": "true",
                    },
                    "annotations": {
                        "prometheus.io/scrape": "true",
                        "prometheus.io/port": "8000",
                        "prometheus.io/path": "/metrics",
                    },
                },
                "spec": {
                    "containers": [
                        {
                            "name": name,
                            "image": f"localhost:5001/{name}:good",
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"containerPort": 8000, "name": "http"}],
                            "env": env,
                            "resources": _RESOURCES,
                            "livenessProbe": _probe("/healthz"),
                            "readinessProbe": _probe("/readyz"),
                        }
                    ],
                },
            },
        },
    }

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": "ust-prod",
            "labels": {"app": name, "app.kubernetes.io/name": name, **PART_OF_LABEL},
        },
        "spec": {
            "type": service_type,
            "selector": {"app": name},
            "ports": service_ports,
        },
    }

    return [deployment, service]


# One entry per deploy/prod/<filename>.yaml this script owns.
INSTANCES: dict[str, dict[str, Any]] = {
    "auth-service.yaml": {
        "name": "auth-service",
        "component": "auth",
        "custom_env": [
            {"name": "DATABASE_URL", "value": "postgresql://postgres@prod-postgres:5432/ust_prod"},
            {"name": "AUTH_TOKEN_CACHE_MAX_SIZE", "value": "1000"},
        ],
        "service_type": "ClusterIP",
        "service_ports": [{"name": "http", "port": 8000, "targetPort": 8000}],
    },
    "data-service.yaml": {
        "name": "data-service",
        "component": "data",
        "custom_env": [
            {"name": "DATABASE_URL", "value": "postgresql://postgres@prod-postgres:5432/ust_prod"},
            {"name": "DATA_SERVICE_VARIANT", "value": "good"},
        ],
        "service_type": "LoadBalancer",
        "service_ports": [
            {"name": "http", "port": 8000, "targetPort": 8000},
            {"name": "admin-fault", "port": 8081, "targetPort": 8000},
        ],
    },
    "edge-gateway.yaml": {
        "name": "edge-gateway",
        "component": "gateway",
        "custom_env": [
            {"name": "UNDERSTUDY_FAULT_INJECTION_ENABLED", "value": "false"},
            {"name": "AUTH_SERVICE_URL", "value": "http://auth-service:8000"},
            {"name": "DATA_SERVICE_URL", "value": "http://data-service:8000"},
            {"name": "HTTP_TIMEOUT_SECONDS", "value": "5.0"},
        ],
        "service_type": "LoadBalancer",
        "service_ports": [{"name": "gateway", "port": 8080, "targetPort": 8000}],
    },
    "worker.yaml": {
        "name": "worker",
        "component": "worker",
        "custom_env": [
            {"name": "DATABASE_URL", "value": "postgresql://postgres@prod-postgres:5432/ust_prod"},
            {"name": "POLL_INTERVAL_SECONDS", "value": "1.0"},
        ],
        "service_type": "ClusterIP",
        "service_ports": [{"name": "http", "port": 8000, "targetPort": 8000}],
    },
}


def render(filename: str) -> str:
    """Render one deploy/prod/<filename>.yaml's full text."""
    return dump_documents(build_manifest(**INSTANCES[filename]))


def check_drift() -> list[str]:
    """Return a list of filenames whose committed content no longer matches what this
    generator would produce (compared structurally, not byte-for-byte)."""
    drifted: list[str] = []
    for filename in INSTANCES:
        path = PROD_DIR / filename
        rendered_docs = build_manifest(**INSTANCES[filename])
        if not path.is_file():
            drifted.append(filename)
            continue
        committed_docs = [d for d in yaml.safe_load_all(path.read_text()) if d is not None]
        if committed_docs != rendered_docs:
            drifted.append(filename)
    return drifted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any committed file differs from what this script would generate",
    )
    args = parser.parse_args(argv)

    if args.check:
        drifted = check_drift()
        if drifted:
            print("Drift detected in:", ", ".join(drifted), file=sys.stderr)
            return 1
        print("OK: deploy/prod/{auth-service,data-service,edge-gateway,worker}.yaml match the generator.")
        return 0

    for filename in INSTANCES:
        (PROD_DIR / filename).write_text(render(filename))
        print(f"wrote {PROD_DIR / filename}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
