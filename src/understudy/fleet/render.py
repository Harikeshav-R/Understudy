"""Twin Kubernetes manifest renderer.

Implements build-plan step A2.2:
Produce twin manifests from that snapshot into ust-twin-<incident>-<candidate>,
rewriting: namespace, UNDERSTUDY_ROLE=twin, database DSN to the twin's cloned DB,
external URLs to egress-stub, and applying the twin NetworkPolicy.
"""

import ipaddress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from understudy.common.config import Settings, get_settings
from understudy.common.errors import FleetError
from understudy.fleet.models import (
    ClusterWorkloadSnapshot,
    EnvVar,
    TwinManifestBundle,
    WorkloadSnapshot,
)

DEFAULT_EGRESS_STUB_URL = "http://egress-stub.ust-system:8000"
DEFAULT_TWIN_POSTGRES_HOST = "twin-postgres.ust-system"
DEFAULT_TWIN_POSTGRES_PORT = 5433
DEFAULT_TWIN_POSTGRES_USER = "postgres"


def _find_default_policies_dir() -> Path:
    """Find the deploy/policies directory relative to the repository root."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "deploy" / "policies"
        if candidate.is_dir():
            return candidate
    return Path("deploy/policies")


def sanitize_database_name(incident_id: str, candidate_index: int) -> str:
    """Derive a safe PostgreSQL database identifier for a twin environment.

    PostgreSQL unquoted database identifiers cannot contain hyphens.
    Hyphens are replaced with underscores to maintain compatibility with `CREATE DATABASE`
    and `psql -c "\\l" | grep twin_`.
    """
    clean_incident = incident_id.replace("-", "_").strip()
    if not clean_incident:
        raise FleetError(f"Incident ID cannot be empty, got: {incident_id!r}")
    if candidate_index < 0:
        raise FleetError(f"Candidate index must be non-negative, got: {candidate_index}")
    return f"twin_{clean_incident}_{candidate_index}"


def build_twin_namespace(twin_prefix: str, incident_id: str, candidate_index: int) -> str:
    """Derive the isolated twin namespace name."""
    clean_incident = incident_id.strip()
    if not clean_incident:
        raise FleetError(f"Incident ID cannot be empty, got: {incident_id!r}")
    if candidate_index < 0:
        raise FleetError(f"Candidate index must be non-negative, got: {candidate_index}")
    return f"{twin_prefix}-{clean_incident}-{candidate_index}"


def rewrite_database_dsn(
    original_dsn: str | None,
    database_name: str,
    host: str = DEFAULT_TWIN_POSTGRES_HOST,
    port: int = DEFAULT_TWIN_POSTGRES_PORT,
    user: str = DEFAULT_TWIN_POSTGRES_USER,
) -> str:
    """Rewrite a PostgreSQL DSN to point to the cloned twin database in twin-postgres."""
    if not original_dsn:
        return f"postgresql://{user}@{host}:{port}/{database_name}"

    try:
        parsed = urlsplit(original_dsn)
        query = f"?{parsed.query}" if parsed.query else ""
        auth_user = parsed.username or user
        user_part = f"{auth_user}:{parsed.password}" if parsed.password else auth_user
        return f"postgresql://{user_part}@{host}:{port}/{database_name}{query}"
    except Exception:
        return f"postgresql://{user}@{host}:{port}/{database_name}"


def _is_ip_literal(hostname: str) -> bool:
    """Check whether hostname is an IPv4 or IPv6 literal."""
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


def rewrite_url(
    url: str,
    target_namespace: str,
    prod_namespace: str,
    known_workloads: set[str],
    egress_stub_url: str = DEFAULT_EGRESS_STUB_URL,
) -> str:
    """Rewrite outbound URLs to egress-stub or local twin namespace endpoints.

    Rules:
    - Non-HTTP(S) values are returned unchanged.
    - IP literals are blocked under ADR-014; redirected to egress-stub preserving path/query.
    - URLs targeting ust-prod (e.g. auth-service.ust-prod) are redirected to the twin namespace.
    - Unqualified in-namespace services (e.g. auth-service:8000) are preserved.
    - System services (ust-system, kube-system) are preserved.
    - Any external URL (e.g. api.stripe.com, api.github.com) is redirected to egress-stub.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        return url

    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return egress_stub_url

        parsed_stub = urlsplit(egress_stub_url)

        if _is_ip_literal(hostname):
            return urlunsplit(
                (
                    parsed_stub.scheme,
                    parsed_stub.netloc,
                    parsed.path,
                    parsed.query,
                    parsed.fragment,
                )
            )

        labels = hostname.split(".")

        # Target referencing production namespace explicitly
        if prod_namespace in labels:
            svc_name = labels[0]
            port_part = f":{parsed.port}" if parsed.port else ""
            return urlunsplit(
                (
                    parsed.scheme,
                    f"{svc_name}.{target_namespace}{port_part}",
                    parsed.path,
                    parsed.query,
                    parsed.fragment,
                )
            )

        # Internal unqualified service name
        if hostname in known_workloads:
            return url

        # Cluster infrastructure namespaces (allowed by NetworkPolicy)
        if "ust-system" in labels or "kube-system" in labels:
            return url

        # External URL -> redirected to egress-stub
        return urlunsplit(
            (
                parsed_stub.scheme,
                parsed_stub.netloc,
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
    except Exception:
        return egress_stub_url


class TwinManifestRenderer:
    """Renders isolated twin Kubernetes manifests from a production snapshot."""

    def __init__(
        self,
        twin_namespace_prefix: str = "ust-twin",
        prod_namespace: str = "ust-prod",
        system_namespace: str = "ust-system",
        twin_postgres_host: str = DEFAULT_TWIN_POSTGRES_HOST,
        twin_postgres_port: int = DEFAULT_TWIN_POSTGRES_PORT,
        twin_postgres_user: str = DEFAULT_TWIN_POSTGRES_USER,
        egress_stub_url: str = DEFAULT_EGRESS_STUB_URL,
        policies_dir: Path | None = None,
    ) -> None:
        self.twin_namespace_prefix = twin_namespace_prefix
        self.prod_namespace = prod_namespace
        self.system_namespace = system_namespace
        self.twin_postgres_host = twin_postgres_host
        self.twin_postgres_port = twin_postgres_port
        self.twin_postgres_user = twin_postgres_user
        self.egress_stub_url = egress_stub_url
        self.policies_dir = policies_dir or _find_default_policies_dir()

    def _render_namespace(
        self, twin_namespace: str, incident_id: str, candidate_index: int
    ) -> dict[str, Any]:
        """Render the twin Namespace manifest."""
        return {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": twin_namespace,
                "labels": {
                    "app.kubernetes.io/part-of": "understudy",
                    "environment": "twin",
                    "understudy.dev/incident": incident_id,
                    "understudy.dev/candidate": str(candidate_index),
                    "kubernetes.io/metadata.name": twin_namespace,
                },
            },
        }

    def _render_network_policy(
        self, twin_namespace: str, incident_id: str, candidate_index: int
    ) -> dict[str, Any]:
        """Render the twin NetworkPolicy adhering to ADR-014 and Invariant K6."""
        template_file = self.policies_dir / "twin-network-policy.template.yaml"
        if template_file.is_file():
            content = template_file.read_text(encoding="utf-8")
            rendered = content.replace("${TWIN_NAMESPACE}", twin_namespace)
            policy = dict(yaml.safe_load(rendered))
            labels = policy.setdefault("metadata", {}).setdefault("labels", {})
            labels["understudy.dev/incident"] = incident_id
            labels["understudy.dev/candidate"] = str(candidate_index)
            return policy

        # Fallback golden spec if template file is absent
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": "twin-egress-containment",
                "namespace": twin_namespace,
                "labels": {
                    "app.kubernetes.io/part-of": "understudy",
                    "environment": "twin",
                    "understudy.dev/incident": incident_id,
                    "understudy.dev/candidate": str(candidate_index),
                },
            },
            "spec": {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": [
                    {"to": [{"podSelector": {}}]},
                    {
                        "to": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                                }
                            }
                        ],
                        "ports": [
                            {"protocol": "UDP", "port": 53},
                            {"protocol": "TCP", "port": 53},
                        ],
                    },
                    {
                        "to": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {
                                        "kubernetes.io/metadata.name": self.system_namespace
                                    }
                                }
                            }
                        ],
                        "ports": [
                            {"protocol": "TCP", "port": 5432},
                            {"protocol": "TCP", "port": 5433},
                            {"protocol": "TCP", "port": 8000},
                            {"protocol": "TCP", "port": 8080},
                        ],
                    },
                ],
            },
        }

    def _render_rbac(
        self, twin_namespace: str, incident_id: str, candidate_index: int
    ) -> list[dict[str, Any]]:
        """Render twin ServiceAccount and RoleBinding."""
        template_file = self.policies_dir / "twin-rbac-template.yaml"
        if template_file.is_file():
            content = template_file.read_text(encoding="utf-8")
            rendered = content.replace("${TWIN_NAMESPACE}", twin_namespace)
            docs = [dict(d) for d in yaml.safe_load_all(rendered) if d is not None]
            for doc in docs:
                labels = doc.setdefault("metadata", {}).setdefault("labels", {})
                labels["understudy.dev/incident"] = incident_id
                labels["understudy.dev/candidate"] = str(candidate_index)
            return docs

        # Fallback golden RBAC manifests
        sa = {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": "understudy-twin",
                "namespace": twin_namespace,
                "labels": {
                    "app.kubernetes.io/part-of": "understudy",
                    "environment": "twin",
                    "understudy.dev/incident": incident_id,
                    "understudy.dev/candidate": str(candidate_index),
                },
            },
        }
        rb = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {
                "name": "understudy-twin",
                "namespace": twin_namespace,
                "labels": {
                    "app.kubernetes.io/part-of": "understudy",
                    "environment": "twin",
                    "understudy.dev/incident": incident_id,
                    "understudy.dev/candidate": str(candidate_index),
                },
            },
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "understudy-twin",
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": "understudy-twin",
                    "namespace": twin_namespace,
                }
            ],
        }
        return [sa, rb]

    def _render_config_maps(
        self,
        config_maps: dict[str, dict[str, str]],
        twin_namespace: str,
        incident_id: str,
        candidate_index: int,
        database_dsn: str,
        known_workloads: set[str],
    ) -> list[dict[str, Any]]:
        """Render mirrored ConfigMaps with rewritten environment/database/URLs."""
        manifests: list[dict[str, Any]] = []
        for name, data in sorted(config_maps.items()):
            rewritten_data: dict[str, str] = {}
            for k, v in data.items():
                if k == "ENVIRONMENT" and v == "production":
                    rewritten_data[k] = "twin"
                elif k in ("DATABASE_URL", "POSTGRES_URL", "DB_URL") or v.startswith(
                    ("postgresql://", "postgres://")
                ):
                    rewritten_data[k] = database_dsn
                elif v.startswith(("http://", "https://")):
                    rewritten_data[k] = rewrite_url(
                        v,
                        target_namespace=twin_namespace,
                        prod_namespace=self.prod_namespace,
                        known_workloads=known_workloads,
                        egress_stub_url=self.egress_stub_url,
                    )
                else:
                    rewritten_data[k] = v

            manifests.append(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": name,
                        "namespace": twin_namespace,
                        "labels": {
                            "app.kubernetes.io/part-of": "understudy",
                            "environment": "twin",
                            "understudy.dev/incident": incident_id,
                            "understudy.dev/candidate": str(candidate_index),
                        },
                    },
                    "data": rewritten_data,
                }
            )
        return manifests

    def _rewrite_container_env(
        self,
        env_vars: list[EnvVar],
        database_dsn: str,
        twin_namespace: str,
        known_workloads: set[str],
    ) -> list[dict[str, Any]]:
        """Rewrite container environment variables ensuring twin role, cloned DB, and egress."""
        result: list[dict[str, Any]] = []
        role_set = False

        for ev in env_vars:
            if ev.name == "UNDERSTUDY_ROLE":
                result.append({"name": "UNDERSTUDY_ROLE", "value": "twin"})
                role_set = True
            elif ev.name in ("DATABASE_URL", "POSTGRES_URL", "DB_URL") or (
                ev.value and ev.value.startswith(("postgresql://", "postgres://"))
            ):
                result.append({"name": ev.name, "value": database_dsn})
            elif ev.value and (ev.value.startswith("http://") or ev.value.startswith("https://")):
                rewritten = rewrite_url(
                    ev.value,
                    target_namespace=twin_namespace,
                    prod_namespace=self.prod_namespace,
                    known_workloads=known_workloads,
                    egress_stub_url=self.egress_stub_url,
                )
                entry: dict[str, Any] = {"name": ev.name, "value": rewritten}
                if ev.value_from:
                    entry["valueFrom"] = ev.value_from
                result.append(entry)
            else:
                entry = {"name": ev.name}
                if ev.value is not None:
                    entry["value"] = ev.value
                if ev.value_from:
                    entry["valueFrom"] = ev.value_from
                result.append(entry)

        if not role_set:
            result.append({"name": "UNDERSTUDY_ROLE", "value": "twin"})

        return result

    def _render_services(
        self,
        workloads: dict[str, WorkloadSnapshot],
        twin_namespace: str,
        incident_id: str,
        candidate_index: int,
    ) -> list[dict[str, Any]]:
        """Generate Kubernetes Service manifests for workloads to enable intra-twin DNS."""
        services: list[dict[str, Any]] = []

        for name, wl in sorted(workloads.items()):
            ports: list[dict[str, Any]] = []
            seen_ports: set[int] = set()

            # For edge-gateway, provide both 8000 and 8080
            if name == "edge-gateway":
                ports.extend(
                    [
                        {"name": "http", "port": 8000, "targetPort": 8000, "protocol": "TCP"},
                        {"name": "gateway", "port": 8080, "targetPort": 8000, "protocol": "TCP"},
                    ]
                )
                seen_ports.update([8000, 8080])

            # For data-service, provide both 8000 and 8081
            if name == "data-service":
                ports.extend(
                    [
                        {"name": "http", "port": 8000, "targetPort": 8000, "protocol": "TCP"},
                        {
                            "name": "admin-fault",
                            "port": 8081,
                            "targetPort": 8000,
                            "protocol": "TCP",
                        },
                    ]
                )
                seen_ports.update([8000, 8081])

            for c in wl.containers:
                for p in c.ports:
                    cp = p.get("containerPort")
                    if cp and cp not in seen_ports:
                        p_name = p.get("name") or f"port-{cp}"
                        ports.append(
                            {
                                "name": p_name,
                                "port": cp,
                                "targetPort": cp,
                                "protocol": p.get("protocol", "TCP"),
                            }
                        )
                        seen_ports.add(cp)

            if ports:
                services.append(
                    {
                        "apiVersion": "v1",
                        "kind": "Service",
                        "metadata": {
                            "name": name,
                            "namespace": twin_namespace,
                            "labels": {
                                "app": name,
                                "app.kubernetes.io/name": name,
                                "app.kubernetes.io/part-of": "understudy",
                                "environment": "twin",
                                "understudy.dev/incident": incident_id,
                                "understudy.dev/candidate": str(candidate_index),
                            },
                        },
                        "spec": {
                            "type": "ClusterIP",
                            "selector": {"app": name},
                            "ports": ports,
                        },
                    }
                )

        return services

    def _render_deployments(
        self,
        workloads: dict[str, WorkloadSnapshot],
        twin_namespace: str,
        incident_id: str,
        candidate_index: int,
        database_dsn: str,
        known_workloads: set[str],
    ) -> list[dict[str, Any]]:
        """Render twin Deployment manifests using pinned image digests."""
        deployments: list[dict[str, Any]] = []

        for name, wl in sorted(workloads.items()):
            container_specs: list[dict[str, Any]] = []

            for c in wl.containers:
                if not c.pinned_image:
                    raise FleetError(
                        f"Workload '{name}' container '{c.name}' has no pinned image digest"
                    )

                env_specs = self._rewrite_container_env(
                    env_vars=c.env,
                    database_dsn=database_dsn,
                    twin_namespace=twin_namespace,
                    known_workloads=known_workloads,
                )

                cs: dict[str, Any] = {
                    "name": c.name,
                    "image": c.pinned_image,
                    "imagePullPolicy": "IfNotPresent",
                    "resources": {
                        "requests": dict(c.resources.requests),
                        "limits": dict(c.resources.limits),
                    },
                    "env": env_specs,
                }
                if c.ports:
                    cs["ports"] = list(c.ports)
                if c.liveness_probe:
                    cs["livenessProbe"] = dict(c.liveness_probe)
                if c.readiness_probe:
                    cs["readinessProbe"] = dict(c.readiness_probe)

                container_specs.append(cs)

            pod_spec: dict[str, Any] = {
                "serviceAccountName": "understudy-twin",
                "containers": container_specs,
            }
            if wl.volumes:
                pod_spec["volumes"] = list(wl.volumes)

            dep: dict[str, Any] = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": name,
                    "namespace": twin_namespace,
                    "labels": {
                        **wl.labels,
                        "app": name,
                        "app.kubernetes.io/name": name,
                        "app.kubernetes.io/part-of": "understudy",
                        "environment": "twin",
                        "understudy.dev/incident": incident_id,
                        "understudy.dev/candidate": str(candidate_index),
                    },
                    "annotations": dict(wl.annotations),
                },
                "spec": {
                    "replicas": wl.replicas,
                    "selector": {
                        "matchLabels": {"app": name},
                    },
                    "template": {
                        "metadata": {
                            "labels": {
                                "app": name,
                                "app.kubernetes.io/name": name,
                                "app.kubernetes.io/part-of": "understudy",
                                "environment": "twin",
                                "understudy.dev/scrape": "true",
                                "understudy.dev/incident": incident_id,
                                "understudy.dev/candidate": str(candidate_index),
                            },
                            "annotations": {
                                "prometheus.io/scrape": "true",
                                "prometheus.io/port": "8000",
                                "prometheus.io/path": "/metrics",
                                **wl.annotations,
                            },
                        },
                        "spec": pod_spec,
                    },
                },
            }
            deployments.append(dep)

        return deployments

    def render(
        self,
        snapshot: ClusterWorkloadSnapshot,
        incident_id: str,
        candidate_index: int,
        database_name: str | None = None,
    ) -> TwinManifestBundle:
        """Render all isolated twin manifests for a given candidate."""
        if not incident_id or not incident_id.strip():
            raise FleetError("incident_id cannot be empty")
        if candidate_index < 0:
            raise FleetError(f"candidate_index must be >= 0, got {candidate_index}")

        twin_id = f"twin_{incident_id}_{candidate_index}"
        twin_namespace = build_twin_namespace(
            self.twin_namespace_prefix, incident_id, candidate_index
        )
        db_name = database_name or sanitize_database_name(incident_id, candidate_index)
        db_dsn = rewrite_database_dsn(
            original_dsn=None,
            database_name=db_name,
            host=self.twin_postgres_host,
            port=self.twin_postgres_port,
            user=self.twin_postgres_user,
        )

        known_workloads = set(snapshot.workloads.keys())

        # Ordered Kubernetes manifests:
        # 1. Namespace
        # 2. RBAC (ServiceAccount, RoleBinding)
        # 3. NetworkPolicy
        # 4. ConfigMaps
        # 5. Services
        # 6. Deployments
        all_manifests: list[dict[str, Any]] = []

        all_manifests.append(self._render_namespace(twin_namespace, incident_id, candidate_index))
        all_manifests.extend(self._render_rbac(twin_namespace, incident_id, candidate_index))
        all_manifests.append(
            self._render_network_policy(twin_namespace, incident_id, candidate_index)
        )
        all_manifests.extend(
            self._render_config_maps(
                config_maps=snapshot.config_maps,
                twin_namespace=twin_namespace,
                incident_id=incident_id,
                candidate_index=candidate_index,
                database_dsn=db_dsn,
                known_workloads=known_workloads,
            )
        )
        all_manifests.extend(
            self._render_services(
                workloads=snapshot.workloads,
                twin_namespace=twin_namespace,
                incident_id=incident_id,
                candidate_index=candidate_index,
            )
        )
        all_manifests.extend(
            self._render_deployments(
                workloads=snapshot.workloads,
                twin_namespace=twin_namespace,
                incident_id=incident_id,
                candidate_index=candidate_index,
                database_dsn=db_dsn,
                known_workloads=known_workloads,
            )
        )

        return TwinManifestBundle(
            twin_id=twin_id,
            incident_id=incident_id,
            candidate_index=candidate_index,
            namespace=twin_namespace,
            database_name=db_name,
            database_dsn=db_dsn,
            manifests=all_manifests,
        )


def render_twin_manifests(
    snapshot: ClusterWorkloadSnapshot,
    incident_id: str,
    candidate_index: int,
    database_name: str | None = None,
    settings: Settings | None = None,
    policies_dir: Path | None = None,
) -> TwinManifestBundle:
    """Convenience function to render twin manifests using application configuration."""
    resolved_settings = settings or get_settings()
    renderer = TwinManifestRenderer(
        twin_namespace_prefix=resolved_settings.cluster.twin_namespace_prefix,
        prod_namespace=resolved_settings.cluster.prod_namespace,
        system_namespace=resolved_settings.cluster.system_namespace,
        policies_dir=policies_dir,
    )
    return renderer.render(
        snapshot=snapshot,
        incident_id=incident_id,
        candidate_index=candidate_index,
        database_name=database_name,
    )


__all__ = [
    "DEFAULT_EGRESS_STUB_URL",
    "DEFAULT_TWIN_POSTGRES_HOST",
    "DEFAULT_TWIN_POSTGRES_PORT",
    "DEFAULT_TWIN_POSTGRES_USER",
    "TwinManifestRenderer",
    "build_twin_namespace",
    "render_twin_manifests",
    "rewrite_database_dsn",
    "rewrite_url",
    "sanitize_database_name",
]
