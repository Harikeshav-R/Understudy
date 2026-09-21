#!/usr/bin/env python3
"""Generate deploy/system/{prod,twin,system}-postgres.yaml from one shared template.

deploy/system/prod-postgres.yaml, twin-postgres.yaml, and system-postgres.yaml were
three independently hand-written, near-identical Deployment+Service pairs (identical
skeleton: replicas, labels, imagePullPolicy, POSTGRES_USER/POSTGRES_HOST_AUTH_METHOD
env, pg_isready probes; differing only in namespace, image, POSTGRES_DB, resources,
host port, and system-postgres's extra init-script ConfigMap/volume). Nothing else
kept them from drifting apart independently.

This repo has no existing manifest-templating tool (no Helm/Kustomize/Jsonnet
anywhere), and introducing one would need a new ADR (AGENTS.md SS2.1/SS9) -- out of
scope for a cleanup. ADR-004 commits to "Python everywhere", so this generator
(a plain Python script building manifests as dicts and dumping them with PyYAML,
which the repo already depends on) fits the existing idiom better than a new tool.

Manifests are committed, plain YAML -- nothing in `make up`/`kubectl apply` runs this
script at deploy time. Run it in one of two modes:

    python deploy/generate_postgres_manifests.py          # (re)write the 3 files
    python deploy/generate_postgres_manifests.py --check   # exit 1 on drift, no writes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_DIR = REPO_ROOT / "deploy" / "system"

# Runnable directly (`python deploy/generate_postgres_manifests.py`) as well as
# importable as `deploy.generate_postgres_manifests` (from tests); only the direct-run
# case needs the repo root added to sys.path for the absolute `deploy.*` import below.
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO_ROOT))

from deploy._manifest_gen_common import PART_OF_LABEL as _PART_OF_LABEL  # noqa: E402
from deploy._manifest_gen_common import LiteralStr as _LiteralStr  # noqa: E402
from deploy._manifest_gen_common import dump_documents  # noqa: E402


def _probe(db_name: str) -> dict[str, Any]:
    return {
        "exec": {"command": ["pg_isready", "-U", "postgres", "-d", db_name]},
        "initialDelaySeconds": 5,
        "periodSeconds": 5,
    }


def build_manifest(
    *,
    name: str,
    namespace: str,
    image: str,
    db_name: str,
    resources: dict[str, dict[str, str]],
    service_port: int,
    service_port_name: str,
    init_configmap: dict[str, Any] | None = None,
    args: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build the [ConfigMap?, Deployment, Service] document list for one postgres
    instance. init_configmap (system-postgres only) is prepended and wired into the
    pod's volumes/volumeMounts."""
    container: dict[str, Any] = {
        "name": "postgres",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": 5432, "name": "postgres"}],
        "env": [
            {"name": "POSTGRES_USER", "value": "postgres"},
            {"name": "POSTGRES_HOST_AUTH_METHOD", "value": "trust"},
            {"name": "POSTGRES_DB", "value": db_name},
        ],
    }
    if args is not None:
        container["args"] = args
    if init_configmap is not None:
        container["volumeMounts"] = [
            {
                "name": "init-script",
                "mountPath": "/docker-entrypoint-initdb.d/init.sql",
                "subPath": "init.sql",
            }
        ]
    container["resources"] = resources
    container["livenessProbe"] = _probe(db_name)
    container["readinessProbe"] = _probe(db_name)

    pod_spec: dict[str, Any] = {"containers": [container]}

    docs: list[dict[str, Any]] = []
    if init_configmap is not None:
        docs.append(init_configmap)
        pod_spec["volumes"] = [
            {"name": "init-script", "configMap": {"name": init_configmap["metadata"]["name"]}}
        ]

    docs.append(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {
                    "app": name,
                    "app.kubernetes.io/name": name,
                    **_PART_OF_LABEL,
                    "app.kubernetes.io/component": "database",
                },
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {
                        "labels": {"app": name, "app.kubernetes.io/name": name, **_PART_OF_LABEL},
                    },
                    "spec": pod_spec,
                },
            },
        }
    )

    docs.append(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {"app": name, "app.kubernetes.io/name": name, **_PART_OF_LABEL},
            },
            "spec": {
                "type": "LoadBalancer",
                "selector": {"app": name},
                "ports": [
                    {"name": service_port_name, "port": service_port, "targetPort": 5432}
                ],
            },
        }
    )

    return docs


def _system_postgres_init_configmap() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "system-postgres-init",
            "namespace": "ust-system",
            "labels": {
                "app": "system-postgres",
                "app.kubernetes.io/name": "system-postgres",
                **_PART_OF_LABEL,
            },
        },
        "data": {"init.sql": _LiteralStr("CREATE EXTENSION IF NOT EXISTS vector;\n")},
    }


# One entry per deploy/system/<filename>.yaml this script owns.
INSTANCES: dict[str, dict[str, Any]] = {
    "prod-postgres.yaml": {
        "name": "prod-postgres",
        "namespace": "ust-prod",
        "image": "postgres:16-alpine",
        "args": ["-c", "max_connections=200"],
        "db_name": "ust_prod",
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "500m", "memory": "400Mi"},
        },
        "service_port": 5432,
        "service_port_name": "postgres",
    },
    "twin-postgres.yaml": {
        "name": "twin-postgres",
        "namespace": "ust-system",
        "image": "postgres:16-alpine",
        "args": ["-c", "max_connections=300"],
        "db_name": "postgres",
        "resources": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "1000m", "memory": "700Mi"},
        },
        "service_port": 5433,
        "service_port_name": "host-port",
    },
    "system-postgres.yaml": {
        "name": "system-postgres",
        "namespace": "ust-system",
        "image": "pgvector/pgvector:pg16",
        "db_name": "ust_system",
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "500m", "memory": "400Mi"},
        },
        "service_port": 5434,
        "service_port_name": "host-port",
        "init_configmap": _system_postgres_init_configmap(),
    },
}


def render(filename: str) -> str:
    """Render one deploy/system/<filename>.yaml's full text."""
    return dump_documents(build_manifest(**INSTANCES[filename]))


def check_drift() -> list[str]:
    """Return a list of filenames whose committed content no longer matches what this
    generator would produce (compared structurally, not byte-for-byte, since PyYAML's
    dump style need not match hand-formatted YAML)."""
    drifted: list[str] = []
    for filename in INSTANCES:
        path = SYSTEM_DIR / filename
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
        print("OK: deploy/system/*-postgres.yaml match the generator.")
        return 0

    for filename in INSTANCES:
        (SYSTEM_DIR / filename).write_text(render(filename))
        print(f"wrote {SYSTEM_DIR / filename}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
