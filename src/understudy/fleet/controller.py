"""Twin fleet lifecycle controller.

Implements build-plan step A2.4:
- fork(incident_id, n) -> list[TwinHandle] running N forks concurrently
- waiting for readiness with a 120s timeout
- marking each twin ready only after its NetworkPolicy is confirmed present (K6 precondition)
- idempotent teardown and teardown_all support
"""

import asyncio
import time
from typing import Any

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import Settings, get_settings
from understudy.common.errors import FleetError
from understudy.common.logging import get_logger
from understudy.contracts.twin import TwinHandle
from understudy.fleet.api import DatabaseCloner, FleetController, ManifestRenderer, WorkloadReader
from understudy.fleet.database import KubectlDatabaseExecutor, PostgresDatabaseCloner
from understudy.fleet.k8s import (
    K8sWorkloadReader,
    get_k8s_apps_client,
    get_k8s_core_client,
    get_k8s_networking_client,
    get_k8s_rbac_client,
)
from understudy.fleet.models import ClusterWorkloadSnapshot
from understudy.fleet.render import TwinManifestRenderer
from understudy.fleet.teardown import (
    FleetTeardownManager,
    register_active_incident,
    register_teardown_handlers,
)

logger = get_logger(__name__)

DEFAULT_READINESS_TIMEOUT_SECONDS = 120.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_API_TIMEOUT_SECONDS = 10.0


def _camelize_key(key: str) -> str:
    """Convert snake_case key to camelCase."""
    parts = key.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _camelize_dict(obj: Any) -> Any:
    """Recursively convert dictionary keys to camelCase."""
    if isinstance(obj, dict):
        return {_camelize_key(k): _camelize_dict(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_camelize_dict(elem) for elem in obj]
    return obj


class K8sFleetController(FleetController):
    """Twin namespace and database lifecycle controller."""

    def __init__(
        self,
        workload_reader: WorkloadReader | None = None,
        manifest_renderer: ManifestRenderer | None = None,
        database_cloner: DatabaseCloner | None = None,
        teardown_manager: FleetTeardownManager | None = None,
        core_api: client.CoreV1Api | None = None,
        apps_api: client.AppsV1Api | None = None,
        networking_api: client.NetworkingV1Api | None = None,
        rbac_api: client.RbacAuthorizationV1Api | None = None,
        settings: Settings | None = None,
        clock: Clock | None = None,
        readiness_timeout_seconds: float | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        api_timeout_seconds: float = DEFAULT_API_TIMEOUT_SECONDS,
    ) -> None:
        self.settings = settings or get_settings()
        self.clock = resolve_clock(clock)
        self.readiness_timeout_seconds = (
            readiness_timeout_seconds
            if readiness_timeout_seconds is not None
            else float(self.settings.timeouts.fork_seconds or DEFAULT_READINESS_TIMEOUT_SECONDS)
        )
        self.poll_interval_seconds = max(0.001, poll_interval_seconds)
        self.api_timeout_seconds = api_timeout_seconds

        self._workload_reader = workload_reader
        self._manifest_renderer = manifest_renderer
        self._database_cloner = database_cloner
        self._teardown_manager = teardown_manager
        self._core_api = core_api
        self._apps_api = apps_api
        self._networking_api = networking_api
        self._rbac_api = rbac_api

        register_teardown_handlers(self._teardown_manager)

    @property
    def workload_reader(self) -> WorkloadReader:
        """Lazily initialize workload reader."""
        if self._workload_reader is None:
            self._workload_reader = K8sWorkloadReader(
                timeout_seconds=self.api_timeout_seconds,
                clock=self.clock,
                context=self.settings.cluster.context,
            )
        return self._workload_reader

    @property
    def manifest_renderer(self) -> ManifestRenderer:
        """Lazily initialize manifest renderer."""
        if self._manifest_renderer is None:
            self._manifest_renderer = TwinManifestRenderer(
                twin_namespace_prefix=self.settings.cluster.twin_namespace_prefix,
                prod_namespace=self.settings.cluster.prod_namespace,
                system_namespace=self.settings.cluster.system_namespace,
            )
        return self._manifest_renderer

    @property
    def database_cloner(self) -> DatabaseCloner:
        """Lazily initialize database cloner."""
        if self._database_cloner is None:
            executor = KubectlDatabaseExecutor(
                namespace=self.settings.cluster.system_namespace,
                deployment="twin-postgres",
                context=self.settings.cluster.context,
            )
            self._database_cloner = PostgresDatabaseCloner(
                executor=executor,
                clock=self.clock,
            )
        return self._database_cloner

    @property
    def teardown_manager(self) -> FleetTeardownManager:
        """Lazily initialize teardown manager."""
        if self._teardown_manager is None:
            self._teardown_manager = FleetTeardownManager(
                core_api=self.core_api,
                database_cloner=self.database_cloner,
                settings=self.settings,
                clock=self.clock,
                api_timeout_seconds=self.api_timeout_seconds,
            )
        return self._teardown_manager

    @property
    def core_api(self) -> client.CoreV1Api:
        """Lazily initialize CoreV1Api."""
        if self._core_api is None:
            self._core_api = get_k8s_core_client(context=self.settings.cluster.context)
        return self._core_api

    @property
    def apps_api(self) -> client.AppsV1Api:
        """Lazily initialize AppsV1Api."""
        if self._apps_api is None:
            self._apps_api = get_k8s_apps_client(context=self.settings.cluster.context)
        return self._apps_api

    @property
    def networking_api(self) -> client.NetworkingV1Api:
        """Lazily initialize NetworkingV1Api."""
        if self._networking_api is None:
            self._networking_api = get_k8s_networking_client(context=self.settings.cluster.context)
        return self._networking_api

    @property
    def rbac_api(self) -> client.RbacAuthorizationV1Api:
        """Lazily initialize RbacAuthorizationV1Api."""
        if self._rbac_api is None:
            self._rbac_api = get_k8s_rbac_client(context=self.settings.cluster.context)
        return self._rbac_api

    async def fork(self, incident_id: str, n: int) -> list[TwinHandle]:
        """Concurrently fork N isolated twin environments for an incident.

        Args:
            incident_id: Incident identifier (e.g. 'inc_test').
            n: Number of twin candidates to fork (must be >= 1).

        Returns:
            list[TwinHandle]: List of twin handles in 'ready' state.

        Raises:
            FleetError: If validation fails, K6 precondition fails, or readiness times out.
        """
        if not incident_id or not incident_id.strip():
            raise FleetError(f"Incident ID cannot be empty, got: {incident_id!r}")
        if n < 1:
            raise FleetError(f"Twin count must be >= 1, got: {n}")

        log = get_logger(incident_id=incident_id)
        log.info("fleet_fork_started", incident_id=incident_id, count=n)
        register_active_incident(incident_id)

        # 1. Read production workloads once for all twins to ensure identical base state
        snapshot = await self.workload_reader.read_workloads(
            namespace=self.settings.cluster.prod_namespace,
            exclude_components={"database"},
        )

        # 2. Concurrently fork all N twins
        tasks = [self._fork_one_twin(snapshot, incident_id, i) for i in range(n)]
        twins = await asyncio.gather(*tasks)

        log.info("fleet_fork_completed", incident_id=incident_id, count=len(twins))
        return sorted(twins, key=lambda t: t.candidate_index)

    async def _fork_one_twin(
        self,
        snapshot: ClusterWorkloadSnapshot,
        incident_id: str,
        candidate_index: int,
    ) -> TwinHandle:
        """Fork a single isolated twin environment."""
        log = get_logger(incident_id=incident_id)
        log.info("twin_fork_started", candidate_index=candidate_index)

        # 1. Clone database with concurrency retry (ADR-009)
        clone_result = await self.database_cloner.clone_twin_database(
            incident_id=incident_id,
            candidate_index=candidate_index,
        )

        # 2. Render twin manifests
        bundle = self.manifest_renderer.render(
            snapshot=snapshot,
            incident_id=incident_id,
            candidate_index=candidate_index,
            database_name=clone_result.database_name,
        )

        # 3. Apply manifests in dependency order to Kubernetes
        for manifest in bundle.manifests:
            await self._apply_manifest(manifest, bundle.namespace)
        log.info("twin_manifests_applied", namespace=bundle.namespace)

        # 4. K6 invariant precondition: verify NetworkPolicy is present before marking ready
        await self._confirm_k6_network_policy(bundle.namespace)
        log.info("twin_k6_policy_confirmed", namespace=bundle.namespace)

        # 5. Wait for all twin workloads to reach readiness within timeout
        await self._wait_for_readiness(
            namespace=bundle.namespace,
            deployment_manifests=bundle.deployment_manifests,
            timeout=self.readiness_timeout_seconds,
        )
        ready_time = self.clock.now()
        log.info("twin_ready", namespace=bundle.namespace, ready_at=ready_time.isoformat())

        return TwinHandle(
            twin_id=bundle.twin_id,
            incident_id=incident_id,
            candidate_index=candidate_index,
            namespace=bundle.namespace,
            database=bundle.database_name,
            forked_from_snapshot_at=clone_result.forked_from_snapshot_at,
            ready_at=ready_time,
            state="ready",
        )

    async def _apply_manifest(self, manifest: dict[str, Any], namespace: str) -> None:
        """Apply a single rendered Kubernetes manifest handling conflicts idempotently."""
        kind = manifest.get("kind")
        metadata = manifest.get("metadata", {})
        name = metadata.get("name", "unknown")

        try:
            if kind == "Namespace":
                await self._apply_namespace(manifest)
            elif kind == "ServiceAccount":
                await self._apply_service_account(manifest, namespace)
            elif kind == "RoleBinding":
                await self._apply_role_binding(manifest, namespace)
            elif kind == "NetworkPolicy":
                await self._apply_network_policy(manifest, namespace)
            elif kind == "ConfigMap":
                await self._apply_config_map(manifest, namespace)
            elif kind == "Service":
                await self._apply_service(manifest, namespace)
            elif kind == "Deployment":
                await self._apply_deployment(manifest, namespace)
            else:
                raise FleetError(
                    f"Unsupported Kubernetes manifest kind {kind!r} for resource {name!r}"
                )
        except ApiException as exc:
            raise FleetError(
                f"Failed to apply {kind} {name!r} in namespace {namespace!r}: {exc.reason}",
                details={"status": exc.status, "body": exc.body},
            ) from exc
        except Exception as exc:
            if isinstance(exc, FleetError):
                raise
            raise FleetError(
                f"Unexpected error applying {kind} {name!r} in namespace {namespace!r}: {exc}"
            ) from exc

    async def _apply_namespace(self, manifest: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(
                self.core_api.create_namespace,
                body=manifest,
                _request_timeout=self.api_timeout_seconds,
            )
        except ApiException as exc:
            if exc.status != 409:
                raise

    async def _apply_service_account(self, manifest: dict[str, Any], namespace: str) -> None:
        try:
            await asyncio.to_thread(
                self.core_api.create_namespaced_service_account,
                namespace=namespace,
                body=manifest,
                _request_timeout=self.api_timeout_seconds,
            )
        except ApiException as exc:
            if exc.status != 409:
                raise

    async def _apply_role_binding(self, manifest: dict[str, Any], namespace: str) -> None:
        try:
            await asyncio.to_thread(
                self.rbac_api.create_namespaced_role_binding,
                namespace=namespace,
                body=manifest,
                _request_timeout=self.api_timeout_seconds,
            )
        except ApiException as exc:
            if exc.status != 409:
                raise

    async def _apply_network_policy(self, manifest: dict[str, Any], namespace: str) -> None:
        name = manifest.get("metadata", {}).get("name", "twin-egress-containment")
        try:
            await asyncio.to_thread(
                self.networking_api.create_namespaced_network_policy,
                namespace=namespace,
                body=manifest,
                _request_timeout=self.api_timeout_seconds,
            )
        except ApiException as exc:
            if exc.status == 409:
                await asyncio.to_thread(
                    self.networking_api.replace_namespaced_network_policy,
                    name=name,
                    namespace=namespace,
                    body=manifest,
                    _request_timeout=self.api_timeout_seconds,
                )
            else:
                raise

    async def _apply_config_map(self, manifest: dict[str, Any], namespace: str) -> None:
        name = manifest.get("metadata", {}).get("name", "unknown")
        try:
            await asyncio.to_thread(
                self.core_api.create_namespaced_config_map,
                namespace=namespace,
                body=manifest,
                _request_timeout=self.api_timeout_seconds,
            )
        except ApiException as exc:
            if exc.status == 409:
                await asyncio.to_thread(
                    self.core_api.replace_namespaced_config_map,
                    name=name,
                    namespace=namespace,
                    body=manifest,
                    _request_timeout=self.api_timeout_seconds,
                )
            else:
                raise

    async def _apply_service(self, manifest: dict[str, Any], namespace: str) -> None:
        try:
            await asyncio.to_thread(
                self.core_api.create_namespaced_service,
                namespace=namespace,
                body=manifest,
                _request_timeout=self.api_timeout_seconds,
            )
        except ApiException as exc:
            if exc.status != 409:
                raise

    async def _apply_deployment(self, manifest: dict[str, Any], namespace: str) -> None:
        name = manifest.get("metadata", {}).get("name", "unknown")
        spec = manifest.get("spec", {}).get("template", {}).get("spec", {})
        for container in spec.get("containers", []):
            if "livenessProbe" in container and isinstance(container["livenessProbe"], dict):
                container["livenessProbe"] = _camelize_dict(container["livenessProbe"])
            if "readinessProbe" in container and isinstance(container["readinessProbe"], dict):
                container["readinessProbe"] = _camelize_dict(container["readinessProbe"])

        try:
            await asyncio.to_thread(
                self.apps_api.create_namespaced_deployment,
                namespace=namespace,
                body=manifest,
                _request_timeout=self.api_timeout_seconds,
            )
        except ApiException as exc:
            if exc.status == 409:
                await asyncio.to_thread(
                    self.apps_api.replace_namespaced_deployment,
                    name=name,
                    namespace=namespace,
                    body=manifest,
                    _request_timeout=self.api_timeout_seconds,
                )
            else:
                raise

    async def _confirm_k6_network_policy(
        self,
        namespace: str,
        policy_name: str = "twin-egress-containment",
    ) -> None:
        """Confirm the K6 egress NetworkPolicy is present before marking twin ready.

        Invariant K6 specifies that no twin may reach ust-prod or the public internet.
        The fleet controller refuses to mark a twin ready without the egress NetworkPolicy
        present in the cluster namespace.
        """
        try:
            policy = await asyncio.to_thread(
                self.networking_api.read_namespaced_network_policy,
                name=policy_name,
                namespace=namespace,
                _request_timeout=self.api_timeout_seconds,
            )
        except ApiException as exc:
            if exc.status == 404:
                raise FleetError(
                    f"Twin NetworkPolicy {policy_name!r} not found in namespace {namespace!r}. "
                    "K6 egress containment invariant precondition failed."
                ) from exc
            raise FleetError(
                f"Failed to verify NetworkPolicy {policy_name!r} in {namespace!r}: {exc.reason}"
            ) from exc
        except Exception as exc:
            raise FleetError(
                f"Unexpected error verifying NetworkPolicy in {namespace!r}: {exc}"
            ) from exc

        spec = policy.spec
        policy_types = list(spec.policy_types or []) if spec else []
        if "Egress" not in policy_types:
            raise FleetError(
                f"Twin NetworkPolicy {policy_name!r} in {namespace!r} does not enforce Egress. "
                f"Policy types found: {policy_types}. K6 invariant violated."
            )

    async def _wait_for_readiness(
        self,
        namespace: str,
        deployment_manifests: list[dict[str, Any]],
        timeout: float,
    ) -> None:
        """Wait for all twin deployments and pods to reach Ready state within timeout."""
        expected_deployments = {
            dep["metadata"]["name"]: dep.get("spec", {}).get("replicas", 1)
            for dep in deployment_manifests
            if dep.get("metadata", {}).get("name")
        }
        if not expected_deployments:
            return

        start_time = time.monotonic()
        last_unready_info: list[str] = []

        while True:
            try:
                deployments_resp, pods_resp = await asyncio.gather(
                    asyncio.to_thread(
                        self.apps_api.list_namespaced_deployment,
                        namespace=namespace,
                        _request_timeout=self.api_timeout_seconds,
                    ),
                    asyncio.to_thread(
                        self.core_api.list_namespaced_pod,
                        namespace=namespace,
                        _request_timeout=self.api_timeout_seconds,
                    ),
                )
            except ApiException as exc:
                raise FleetError(
                    f"Error polling readiness in namespace {namespace!r}: {exc.reason}"
                ) from exc

            live_deps = {d.metadata.name: d for d in (deployments_resp.items or []) if d.metadata}
            live_pods = list(pods_resp.items or [])

            all_ready = True
            unready_deps: list[str] = []

            for name, expected_replicas in expected_deployments.items():
                dep = live_deps.get(name)
                if dep is None or dep.status is None:
                    all_ready = False
                    unready_deps.append(f"{name} (missing)")
                    continue

                ready_reps = dep.status.ready_replicas or 0
                avail_reps = dep.status.available_replicas or 0

                if ready_reps < expected_replicas or avail_reps < expected_replicas:
                    all_ready = False
                    unready_deps.append(
                        f"{name} (ready {ready_reps}/{expected_replicas}, avail {avail_reps})"
                    )

            # Check that pods for these deployments are running and ready
            if all_ready:
                for pod in live_pods:
                    phase = pod.status.phase if pod.status else None
                    if phase != "Running":
                        all_ready = False
                        pod_name = pod.metadata.name if pod.metadata else "unknown"
                        unready_deps.append(f"pod/{pod_name} ({phase})")
                        break

                    container_statuses = pod.status.container_statuses or [] if pod.status else []
                    if not container_statuses or not all(cs.ready for cs in container_statuses):
                        all_ready = False
                        pod_name = pod.metadata.name if pod.metadata else "unknown"
                        unready_deps.append(f"pod/{pod_name} (containers unready)")
                        break

            if all_ready:
                return

            last_unready_info = unready_deps
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                unready_summary = ", ".join(last_unready_info)
                raise FleetError(
                    f"Twin environment {namespace!r} timed out waiting for readiness after "
                    f"{timeout:.1f}s. Unready workloads: [{unready_summary}]"
                )

            await asyncio.sleep(self.poll_interval_seconds)

    async def teardown(self, twin_handle: TwinHandle) -> None:
        """Tear down a specific twin namespace and its cloned database."""
        await self.teardown_manager.teardown_twin(twin_handle)

    async def teardown_all(self, incident_id: str) -> None:
        """Idempotently tear down all twin environments associated with an incident."""
        await self.teardown_manager.teardown_incident(incident_id)


__all__ = [
    "DEFAULT_API_TIMEOUT_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_READINESS_TIMEOUT_SECONDS",
    "K8sFleetController",
]
