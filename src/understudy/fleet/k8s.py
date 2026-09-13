"""Kubernetes API workload reader and image digest resolver.

Implements build-plan step A2.1:
Read ust-prod workloads via the Kubernetes API, extracting image digests
(pinned to digests, not tags), resource specs, env, and ConfigMap references.
"""

import asyncio
from typing import Any, cast

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import get_settings
from understudy.common.errors import FleetError
from understudy.common.logging import get_logger
from understudy.fleet.api import WorkloadReader
from understudy.fleet.models import (
    ClusterWorkloadSnapshot,
    ContainerSnapshot,
    EnvVar,
    ResourceSpec,
    WorkloadSnapshot,
)

logger = get_logger(__name__)

DEFAULT_K8S_TIMEOUT_SECONDS = 10.0
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
POD_TEMPLATE_HASH_LABEL = "pod-template-hash"

# Volume sources a twin can reproduce faithfully, mapped to their manifest field names.
_SUPPORTED_VOLUME_SOURCES: tuple[tuple[str, str], ...] = (
    ("config_map", "configMap"),
    ("secret", "secret"),
    ("empty_dir", "emptyDir"),
    ("downward_api", "downwardAPI"),
    ("projected", "projected"),
)


def _camelize_key(key: str) -> str:
    """Convert a snake_case Kubernetes client attribute name to its manifest spelling."""
    parts = key.lstrip("_").split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _camelize_dict(obj: Any) -> Any:
    """Recursively convert dictionary keys to camelCase, dropping None values."""
    if isinstance(obj, dict):
        return {_camelize_key(k): _camelize_dict(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_camelize_dict(elem) for elem in obj]
    return obj


def _serialize_k8s_obj(obj: Any) -> dict[str, Any]:
    """Serialize a Kubernetes client model into apply-ready manifest form.

    `to_dict()` yields snake_case attribute names (`http_get`, `initial_delay_seconds`)
    which the API server does not recognize, so keys are camelCased recursively and unset
    fields dropped.
    """
    return cast("dict[str, Any]", _camelize_dict(obj.to_dict()))


def _parse_image_repo_and_tag(image_spec: str) -> tuple[str, str | None]:
    """Split an image specification into repository base and tag/digest.

    Correctly handles host:port in registry URLs (e.g. localhost:5001/data-service:good).
    """
    if "@" in image_spec:
        repo, digest = image_spec.split("@", 1)
        return repo, digest

    if ":" in image_spec:
        parts = image_spec.rsplit(":", 1)
        if "/" not in parts[1]:
            return parts[0], parts[1]

    return image_spec, None


def resolve_image_digest(
    image_spec: str,
    container_status_image_id: str | None,
    allow_unpinned: bool = False,
) -> tuple[str, str]:
    """Resolve an image spec and container status imageID to a pinned image and digest.

    Args:
        image_spec: The image reference from the Deployment's pod template.
        container_status_image_id: The running container's resolved imageID, if any.
        allow_unpinned: When True, degrade to the spec's tag instead of raising if no
            digest can be derived. A prod Deployment whose pods are all Pending
            (ImagePullBackOff, unschedulable) has no imageID to read, and that is exactly
            the incident class Understudy exists to remediate, so the fork must proceed.

    Returns:
        tuple[str, str]: (pinned_image_reference, raw_sha256_digest). The digest is an
        empty string when the image could not be pinned and `allow_unpinned` is True.

    Raises:
        FleetError: If no valid digest can be resolved and `allow_unpinned` is False.
    """
    try:
        return _resolve_pinned_image(image_spec, container_status_image_id)
    except FleetError:
        if not allow_unpinned:
            raise
        logger.warning(
            "image_digest_unpinned",
            image=image_spec,
            image_id=container_status_image_id,
        )
        return image_spec, ""


def _resolve_pinned_image(
    image_spec: str, container_status_image_id: str | None
) -> tuple[str, str]:
    """Derive the digest-pinned image reference, raising if no digest is available."""
    repo, tag_or_digest = _parse_image_repo_and_tag(image_spec)

    # If the deployment spec itself is already pinned with a sha256 digest
    if tag_or_digest and tag_or_digest.startswith("sha256:"):
        return f"{repo}@{tag_or_digest}", tag_or_digest

    if not container_status_image_id:
        raise FleetError(
            f"Cannot resolve image digest for {image_spec}: container status imageID is missing"
        )

    clean_id = container_status_image_id.strip()
    for prefix in ("docker-pullable://", "containerd://", "docker://"):
        if clean_id.startswith(prefix):
            clean_id = clean_id[len(prefix) :]
            break

    if "@" in clean_id:
        id_repo, digest_part = clean_id.split("@", 1)
        if not digest_part.startswith("sha256:"):
            raise FleetError(
                f"Invalid image digest algorithm for {image_spec} in {container_status_image_id}"
            )
        # Prefer the deployment spec's repository name if registry was local/relative
        target_repo = repo if repo else id_repo
        return f"{target_repo}@{digest_part}", digest_part

    if clean_id.startswith("sha256:"):
        return f"{repo}@{clean_id}", clean_id

    # If container status imageID is a 64-char hex string without prefix
    if len(clean_id) == 64 and all(c in "0123456789abcdefABCDEF" for c in clean_id):
        digest = f"sha256:{clean_id.lower()}"
        return f"{repo}@{digest}", digest

    raise FleetError(
        f"Unable to extract sha256 digest from container status imageID: "
        f"{container_status_image_id}"
    )


def extract_resource_spec(resources: client.V1ResourceRequirements | None) -> ResourceSpec:
    """Extract CPU/memory requests and limits from Kubernetes resource requirements."""
    if resources is None:
        return ResourceSpec()

    requests = {k: str(v) for k, v in (resources.requests or {}).items()}
    limits = {k: str(v) for k, v in (resources.limits or {}).items()}
    return ResourceSpec(requests=requests, limits=limits)


def extract_env_vars(env_list: list[client.V1EnvVar] | None) -> list[EnvVar]:
    """Extract environment variables including literal values and valueFrom sources."""
    if not env_list:
        return []

    result: list[EnvVar] = []
    for item in env_list:
        value_from: dict[str, Any] | None = None
        if item.value_from:
            vf = item.value_from
            vf_dict: dict[str, Any] = {}
            if vf.config_map_key_ref:
                cm = vf.config_map_key_ref
                vf_dict["configMapKeyRef"] = {
                    "name": cm.name,
                    "key": cm.key,
                    "optional": cm.optional,
                }
            if vf.secret_key_ref:
                sec = vf.secret_key_ref
                vf_dict["secretKeyRef"] = {
                    "name": sec.name,
                    "key": sec.key,
                    "optional": sec.optional,
                }
            if vf.field_ref:
                fr = vf.field_ref
                vf_dict["fieldRef"] = {
                    "fieldPath": fr.field_path,
                    "apiVersion": fr.api_version,
                }
            if vf_dict:
                value_from = vf_dict

        result.append(EnvVar(name=item.name, value=item.value, value_from=value_from))
    return result


def extract_config_map_refs(pod_spec: client.V1PodSpec) -> list[str]:
    """Identify all ConfigMap names referenced across container env, envFrom, and volumes."""
    names: set[str] = set()

    containers = list(pod_spec.containers or [])
    if pod_spec.init_containers:
        containers.extend(pod_spec.init_containers)

    for c in containers:
        for env in c.env or []:
            if env.value_from and env.value_from.config_map_key_ref:
                cm_name = env.value_from.config_map_key_ref.name
                if cm_name:
                    names.add(cm_name)

        for env_from in c.env_from or []:
            if env_from.config_map_ref and env_from.config_map_ref.name:
                names.add(env_from.config_map_ref.name)

    for vol in pod_spec.volumes or []:
        if vol.config_map and vol.config_map.name:
            names.add(vol.config_map.name)
        if vol.projected and vol.projected.sources:
            for source in vol.projected.sources:
                if source.config_map and source.config_map.name:
                    names.add(source.config_map.name)

    return sorted(names)


def _current_pod_template_hash(
    dep: client.V1Deployment, replica_sets: list[client.V1ReplicaSet]
) -> str | None:
    """Find the pod-template-hash of the ReplicaSet holding the Deployment's current revision.

    Mid-rollout both the old and the new ReplicaSet's pods satisfy the Deployment's
    `matchLabels`, so selecting on labels alone can pair the new spec's image with an old
    pod's digest. Returns None when the revision cannot be determined, leaving the caller to
    fall back to label matching.
    """
    if not dep.metadata:
        return None

    dep_uid = dep.metadata.uid
    dep_revision = (dep.metadata.annotations or {}).get(REVISION_ANNOTATION)
    if not dep_uid or not dep_revision:
        return None

    for rs in replica_sets:
        if not rs.metadata:
            continue
        owners = rs.metadata.owner_references or []
        if not any(o.kind == "Deployment" and o.uid == dep_uid for o in owners):
            continue
        if (rs.metadata.annotations or {}).get(REVISION_ANNOTATION) != dep_revision:
            continue
        return (rs.metadata.labels or {}).get(POD_TEMPLATE_HASH_LABEL)

    return None


def _image_name(image_spec: str) -> str:
    """Return the bare image name, ignoring registry host and tag or digest."""
    repo, _ = _parse_image_repo_and_tag(image_spec)
    return repo.rsplit("/", 1)[-1]


def _pod_images_match_spec(pod: client.V1Pod, spec_containers: list[client.V1Container]) -> bool:
    """Check that every spec container has a running status for the same image name."""
    statuses = {cs.name: cs for cs in (pod.status.container_statuses or [])} if pod.status else {}
    for c in spec_containers:
        status = statuses.get(c.name)
        if status is None or not status.image:
            return False
        if _image_name(status.image) != _image_name(c.image):
            return False
    return True


def _select_digest_source_pod(
    dep: client.V1Deployment,
    pods: list[client.V1Pod],
    replica_sets: list[client.V1ReplicaSet],
    match_labels: dict[str, str],
    spec_containers: list[client.V1Container],
) -> client.V1Pod | None:
    """Pick the pod whose container statuses may be trusted to pin the Deployment's digests.

    Candidates must be Running with container statuses, belong to the Deployment's current
    ReplicaSet when that can be established, and report images corresponding to the current
    spec. The newest candidate wins. Returns None when no pod can be trusted.
    """
    candidates = [
        pod
        for pod in pods
        if pod.metadata
        and pod.metadata.labels
        and all(pod.metadata.labels.get(k) == v for k, v in match_labels.items())
        and pod.status
        and pod.status.phase == "Running"
        and pod.status.container_statuses
    ]

    template_hash = _current_pod_template_hash(dep, replica_sets)
    if template_hash:
        current_revision = [
            pod
            for pod in candidates
            if (pod.metadata.labels or {}).get(POD_TEMPLATE_HASH_LABEL) == template_hash
        ]
        if current_revision:
            candidates = current_revision

    candidates = [pod for pod in candidates if _pod_images_match_spec(pod, spec_containers)]
    if not candidates:
        return None

    return max(candidates, key=lambda p: (p.status.start_time is not None, p.status.start_time))


def _extract_probe(probe: client.V1Probe | None) -> dict[str, Any] | None:
    """Extract probe configuration in apply-ready camelCase manifest form."""
    if probe is None:
        return None
    return _serialize_k8s_obj(probe)


def _extract_probes(
    container: client.V1Container,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Extract liveness and readiness probe configurations as serialized dictionaries."""
    return _extract_probe(container.liveness_probe), _extract_probe(container.readiness_probe)


def _extract_ports(container: client.V1Container) -> list[dict[str, Any]]:
    """Extract container port specifications."""
    if not container.ports:
        return []
    return [
        {
            "containerPort": p.container_port,
            "name": p.name,
            "protocol": p.protocol,
        }
        for p in container.ports
    ]


def _extract_volumes(pod_spec: client.V1PodSpec) -> list[dict[str, Any]]:
    """Extract pod volume specifications in serializable form."""
    if not pod_spec.volumes:
        return []

    volumes: list[dict[str, Any]] = []
    for vol in pod_spec.volumes:
        vol_dict: dict[str, Any] = {"name": vol.name}
        for attr, manifest_key in _SUPPORTED_VOLUME_SOURCES:
            source = getattr(vol, attr, None)
            if source is not None:
                vol_dict[manifest_key] = _serialize_k8s_obj(source)

        if len(vol_dict) == 1:
            present = sorted(
                _camelize_key(k) for k, v in vol.to_dict().items() if v is not None and k != "name"
            )
            raise FleetError(
                f"Volume {vol.name!r} uses an unsupported volume source {present}: a twin "
                "cannot honestly reproduce this source, so the fork is refused rather than "
                "substituting an empty volume"
            )
        volumes.append(vol_dict)
    return volumes


def get_k8s_apps_client(context: str | None = None) -> client.AppsV1Api:
    """Create a Kubernetes AppsV1Api client with local kubeconfig or incluster fallback."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config(context=context)
    return client.AppsV1Api()


def get_k8s_core_client(context: str | None = None) -> client.CoreV1Api:
    """Create a Kubernetes CoreV1Api client with local kubeconfig or incluster fallback."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config(context=context)
    return client.CoreV1Api()


def get_k8s_networking_client(context: str | None = None) -> client.NetworkingV1Api:
    """Create a Kubernetes NetworkingV1Api client with local kubeconfig or incluster fallback."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config(context=context)
    return client.NetworkingV1Api()


def get_k8s_rbac_client(context: str | None = None) -> client.RbacAuthorizationV1Api:
    """Create a Kubernetes RbacAuthorizationV1Api client with kubeconfig or fallback."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config(context=context)
    return client.RbacAuthorizationV1Api()


class K8sWorkloadReader(WorkloadReader):
    """Kubernetes API workload reader and digest resolver."""

    def __init__(
        self,
        apps_api: client.AppsV1Api | None = None,
        core_api: client.CoreV1Api | None = None,
        timeout_seconds: float = DEFAULT_K8S_TIMEOUT_SECONDS,
        clock: Clock | None = None,
        context: str | None = None,
    ) -> None:
        self._context = context
        self._apps_api = apps_api
        self._core_api = core_api
        self.timeout_seconds = timeout_seconds
        self.clock = resolve_clock(clock)

    @property
    def apps_api(self) -> client.AppsV1Api:
        """Lazily initialize AppsV1Api if not provided."""
        if self._apps_api is None:
            self._apps_api = get_k8s_apps_client(context=self._context)
        return self._apps_api

    @property
    def core_api(self) -> client.CoreV1Api:
        """Lazily initialize CoreV1Api if not provided."""
        if self._core_api is None:
            self._core_api = get_k8s_core_client(context=self._context)
        return self._core_api

    async def read_workloads(
        self,
        namespace: str,
        exclude_components: set[str] | None = None,
    ) -> ClusterWorkloadSnapshot:
        """Read all workloads in a namespace, resolving images to digests.

        Args:
            namespace: Kubernetes namespace to read (e.g. 'ust-prod').
            exclude_components: Optional set of component names to skip (e.g. {'database'}).

        Returns:
            ClusterWorkloadSnapshot: Full snapshot of workloads, pinned digests, and config.

        Raises:
            FleetError: If the Kubernetes API call fails or an image cannot be resolved.
        """
        try:
            deployments_resp, pods_resp, replica_sets_resp = await asyncio.gather(
                asyncio.to_thread(
                    self.apps_api.list_namespaced_deployment,
                    namespace=namespace,
                    _request_timeout=self.timeout_seconds,
                ),
                asyncio.to_thread(
                    self.core_api.list_namespaced_pod,
                    namespace=namespace,
                    _request_timeout=self.timeout_seconds,
                ),
                asyncio.to_thread(
                    self.apps_api.list_namespaced_replica_set,
                    namespace=namespace,
                    _request_timeout=self.timeout_seconds,
                ),
            )
        except ApiException as exc:
            raise FleetError(
                f"Failed to query Kubernetes API for namespace '{namespace}': {exc.reason}",
                details={"status": exc.status, "body": exc.body},
            ) from exc
        except Exception as exc:
            raise FleetError(
                f"Unexpected error querying Kubernetes API for namespace '{namespace}': {exc}"
            ) from exc

        deployments: list[client.V1Deployment] = deployments_resp.items or []
        pods: list[client.V1Pod] = pods_resp.items or []
        replica_sets: list[client.V1ReplicaSet] = replica_sets_resp.items or []

        workload_snapshots: dict[str, WorkloadSnapshot] = {}
        all_referenced_cm_names: set[str] = set()

        for dep in deployments:
            dep_name = dep.metadata.name if dep.metadata else "unknown"
            labels = dep.metadata.labels or {} if dep.metadata else {}
            annotations = dep.metadata.annotations or {} if dep.metadata else {}
            component = labels.get("app.kubernetes.io/component")

            if exclude_components and component in exclude_components:
                continue

            match_labels = (
                dep.spec.selector.match_labels or {} if dep.spec and dep.spec.selector else {}
            )

            container_snapshots: list[ContainerSnapshot] = []
            pod_spec = dep.spec.template.spec if dep.spec and dep.spec.template else None
            if not pod_spec or not pod_spec.containers:
                raise FleetError(f"Deployment '{dep_name}' has no containers in pod template")

            selected_pod = _select_digest_source_pod(
                dep=dep,
                pods=pods,
                replica_sets=replica_sets,
                match_labels=match_labels,
                spec_containers=pod_spec.containers,
            )
            container_status_by_name = (
                {cs.name: cs for cs in (selected_pod.status.container_statuses or [])}
                if selected_pod and selected_pod.status
                else {}
            )
            if selected_pod is None:
                # Every matching pod is Pending or reports images that do not correspond to
                # the current spec (ImagePullBackOff, unschedulable, mid-rollout). Degrade to
                # the spec's tag rather than failing the whole fork; the snapshot records
                # digest_pinned=False so the twin's provenance stays honest.
                logger.warning(
                    "workload_digest_unpinned",
                    namespace=namespace,
                    deployment=dep_name,
                )

            for c in pod_spec.containers:
                c_status = container_status_by_name.get(c.name)
                pinned_image, digest = resolve_image_digest(
                    c.image,
                    c_status.image_id if c_status else None,
                    allow_unpinned=True,
                )
                resources = extract_resource_spec(c.resources)
                env = extract_env_vars(c.env)
                ports = _extract_ports(c)
                liveness_probe, readiness_probe = _extract_probes(c)

                container_snapshots.append(
                    ContainerSnapshot(
                        name=c.name,
                        image_tag=c.image,
                        image_digest=digest,
                        pinned_image=pinned_image,
                        digest_pinned=bool(digest),
                        resources=resources,
                        env=env,
                        ports=ports,
                        liveness_probe=liveness_probe,
                        readiness_probe=readiness_probe,
                    )
                )

            cm_refs = extract_config_map_refs(pod_spec)
            all_referenced_cm_names.update(cm_refs)
            volumes = _extract_volumes(pod_spec)
            replicas = dep.spec.replicas if dep.spec and dep.spec.replicas is not None else 1

            workload_snapshots[dep_name] = WorkloadSnapshot(
                name=dep_name,
                namespace=namespace,
                component=component,
                labels=labels,
                annotations=annotations,
                replicas=replicas,
                containers=container_snapshots,
                config_map_refs=cm_refs,
                volumes=volumes,
            )

        # Read live data for all referenced ConfigMaps
        config_maps_data = await self.read_config_maps(namespace, all_referenced_cm_names)

        return ClusterWorkloadSnapshot(
            namespace=namespace,
            workloads=workload_snapshots,
            config_maps=config_maps_data,
            captured_at=self.clock.now(),
        )

    async def read_config_maps(self, namespace: str, names: set[str]) -> dict[str, dict[str, str]]:
        """Read data of referenced ConfigMaps from the cluster namespace."""
        if not names:
            return {}

        results: dict[str, dict[str, str]] = {}
        for name in sorted(names):
            try:
                cm = await asyncio.to_thread(
                    self.core_api.read_namespaced_config_map,
                    name=name,
                    namespace=namespace,
                    _request_timeout=self.timeout_seconds,
                )
                results[name] = dict(cm.data or {})
            except ApiException as exc:
                if exc.status == 404:
                    raise FleetError(
                        f"Referenced ConfigMap '{name}' not found in namespace '{namespace}'"
                    ) from exc
                raise FleetError(
                    f"Failed reading ConfigMap '{name}' in namespace '{namespace}': {exc.reason}"
                ) from exc
            except Exception as exc:
                raise FleetError(
                    f"Unexpected error reading ConfigMap '{name}' in namespace '{namespace}': {exc}"
                ) from exc

        return results


async def read_prod_workloads(
    exclude_components: set[str] | None = None,
    timeout_seconds: float = DEFAULT_K8S_TIMEOUT_SECONDS,
    clock: Clock | None = None,
) -> ClusterWorkloadSnapshot:
    """Convenience function to read production workloads using application settings."""
    settings = get_settings()
    reader = K8sWorkloadReader(
        timeout_seconds=timeout_seconds,
        clock=clock,
        context=settings.cluster.context,
    )
    return await reader.read_workloads(
        namespace=settings.cluster.prod_namespace,
        exclude_components=exclude_components,
    )
