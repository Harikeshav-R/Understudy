"""Kubernetes cluster workload validation against declared dependency services."""

from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from understudy.common.errors import GraphError
from understudy.common.logging import get_logger

logger = get_logger(__name__)


def get_cluster_workloads(
    namespace: str = "ust-prod",
    context: str = "k3d-ust",
) -> set[str]:
    """Retrieve the set of active Deployment workload names in the Kubernetes namespace."""
    try:
        config.load_kube_config(context=context)
    except Exception as exc:
        raise GraphError(
            f"Failed to load kubeconfig for context {context!r}: {exc}",
            details={"context": context},
        ) from exc

    apps_v1 = client.AppsV1Api()
    try:
        deployments: Any = apps_v1.list_namespaced_deployment(namespace=namespace)
    except ApiException as exc:
        raise GraphError(
            f"Failed to list deployments in namespace {namespace!r}: {exc}",
            details={"namespace": namespace, "status": exc.status},
        ) from exc

    return {d.metadata.name for d in deployments.items if d.metadata and d.metadata.name}


def verify_cluster_workloads(
    declared_services: set[str],
    namespace: str = "ust-prod",
    context: str = "k3d-ust",
) -> bool:
    """Verify that all declared dependency graph services exist as Deployments in Kubernetes."""
    active_deployment_names = get_cluster_workloads(namespace=namespace, context=context)
    missing = declared_services - active_deployment_names

    if missing:
        raise GraphError(
            f"Declared services missing active Deployments in namespace {namespace!r}: "
            f"{sorted(missing)}",
            details={
                "missing_services": sorted(missing),
                "active": sorted(active_deployment_names),
            },
        )

    return True
