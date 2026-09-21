"""Fault injection utility for scenario preparation on the cluster.

Implements build-plan step 5.7:
- Patches target deployment container images to specified tags (e.g. :regression).
- Waits for deployment rollout readiness.
- Enforces scenario settle window to generate genuine baseline telemetry.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any

from kubernetes.client.exceptions import ApiException

from understudy.actuator.apply import create_k8s_clients
from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import get_settings
from understudy.common.errors import ActuationError
from understudy.common.logging import get_logger

if TYPE_CHECKING:
    from kubernetes import client

logger = get_logger(__name__)


def _derive_injected_image(current_image: str, new_tag: str) -> str:
    """Derive container image string replacing or appending the given tag."""
    clean = current_image.strip()
    if "@" in clean:
        base = clean.split("@", 1)[0]
    elif ":" in clean:
        parts = clean.rsplit(":", 1)
        base = parts[0] if "/" not in parts[1] else clean
    else:
        base = clean
    return f"{base}:{new_tag.strip()}"


async def inject_scenario_fault(
    target: str,
    image_tag: str,
    namespace: str = "ust-prod",
    settle_seconds: float = 15.0,
    context: str | None = None,
    apps_api: client.AppsV1Api | None = None,
    core_api: client.CoreV1Api | None = None,
    clock: Clock | None = None,
    poll_interval: float = 1.0,
    rollout_timeout: float = 60.0,
) -> str:
    """Inject a fault into a cluster deployment by patching image tag and waiting for rollout.

    Args:
        target: Workload/deployment name (e.g. 'data-service').
        image_tag: Container image tag to deploy (e.g. 'regression').
        namespace: Target namespace (default 'ust-prod').
        settle_seconds: Seconds to wait after rollout for traffic and metrics to settle.
        context: Optional Kubernetes context name.
        apps_api: Optional injected AppsV1Api client.
        core_api: Optional injected CoreV1Api client.
        clock: Optional Clock.
        poll_interval: Rollout status check interval in seconds.
        rollout_timeout: Maximum seconds to wait for deployment rollout.

    Returns:
        The newly injected container image spec.
    """
    resolved_clock = resolve_clock(clock)
    settings = get_settings()
    ctx = context or settings.cluster.context

    resolved_apps, _ = (
        (apps_api, core_api) if apps_api is not None else create_k8s_clients(context=ctx)
    )

    try:
        deployment: Any = await asyncio.to_thread(
            resolved_apps.read_namespaced_deployment,
            name=target,
            namespace=namespace,
        )
    except ApiException as exc:
        msg = f"Failed to read deployment '{target}' in namespace '{namespace}': {exc}"
        raise ActuationError(msg, details={"target": target, "namespace": namespace}) from exc

    containers = deployment.spec.template.spec.containers
    if not containers:
        raise ActuationError(f"Deployment '{target}' has no containers defined")

    # Match target container by name or pick the first container
    target_container = next((c for c in containers if c.name == target), containers[0])
    current_image = target_container.image or ""
    injected_image = _derive_injected_image(current_image, image_tag)

    container_patch: dict[str, Any] = {
        "name": target_container.name,
        "image": injected_image,
    }
    if target == "data-service" or target_container.name == "data-service":
        container_patch["env"] = [{"name": "DATA_SERVICE_VARIANT", "value": image_tag.strip()}]

    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [container_patch],
                }
            }
        }
    }

    try:
        await asyncio.to_thread(
            resolved_apps.patch_namespaced_deployment,
            name=target,
            namespace=namespace,
            body=patch_body,
        )
    except ApiException as exc:
        msg = f"Failed to patch deployment '{target}' with image '{injected_image}': {exc}"
        raise ActuationError(msg, details={"target": target, "namespace": namespace}) from exc

    # Await rollout readiness
    deadline = resolved_clock.now().timestamp() + rollout_timeout
    rollout_ready = False
    while resolved_clock.now().timestamp() < deadline:
        try:
            dep_status: Any = await asyncio.to_thread(
                resolved_apps.read_namespaced_deployment,
                name=target,
                namespace=namespace,
            )
            spec_replicas = dep_status.spec.replicas or 1
            available = dep_status.status.available_replicas or 0
            updated = dep_status.status.updated_replicas or 0
            if available >= spec_replicas and updated >= spec_replicas:
                rollout_ready = True
                break
        except ApiException as exc:
            # During rollout transitions, deployment status queries may transiently fail
            # while pods cycle
            logger.debug("rollout_status_query_transient_error", target=target, error=str(exc))
        await resolved_clock.sleep(poll_interval)

    if not rollout_ready:
        logger.warning("rollout_readiness_timeout", target=target, timeout=rollout_timeout)

    # Settle window
    if settle_seconds > 0:
        await resolved_clock.sleep(settle_seconds)

    logger.info(
        "fault_injected",
        target=target,
        namespace=namespace,
        image=injected_image,
        settle_seconds=settle_seconds,
    )
    sys.stdout.write(
        f"[fault_injected] target={target} namespace={namespace} "
        f"image={injected_image} settled={settle_seconds:.1f}s\n"
    )
    sys.stdout.flush()

    return injected_image


__all__ = ["_derive_injected_image", "inject_scenario_fault"]
