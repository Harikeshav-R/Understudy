"""Unit tests for Kubernetes twin fleet controller.

Enforces 100% line and branch coverage on understudy/fleet/controller.py.
"""

from collections.abc import Generator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from understudy.common.clock import FrozenClock
from understudy.common.config import ClusterSettings, Settings, TimeoutSettings
from understudy.common.errors import FleetError
from understudy.contracts.twin import TwinHandle
from understudy.fleet.controller import (
    DEFAULT_API_TIMEOUT_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    K8sFleetController,
)
from understudy.fleet.models import (
    ClusterWorkloadSnapshot,
    ContainerSnapshot,
    DatabaseCloneResult,
    ResourceSpec,
    TwinManifestBundle,
    WorkloadSnapshot,
)
from understudy.fleet.teardown import clear_active_incidents, unregister_teardown_handlers


@pytest.fixture(autouse=True)
def reset_lifecycle_state() -> Generator[None, None, None]:
    """Ensure clean active incident state and unregister handlers for every test."""
    clear_active_incidents()
    unregister_teardown_handlers()
    yield
    clear_active_incidents()
    unregister_teardown_handlers()


def _make_snapshot(clock: FrozenClock) -> ClusterWorkloadSnapshot:
    """Helper to create a deterministic cluster workload snapshot."""
    c = ContainerSnapshot(
        name="edge-gateway",
        image_tag="localhost:5001/edge-gateway:good",
        image_digest="sha256:1111111111111111111111111111111111111111111111111111111111111111",
        pinned_image="localhost:5001/edge-gateway@sha256:1111111111111111111111111111111111111111111111111111111111111111",
        resources=ResourceSpec(requests={"cpu": "50m"}, limits={"cpu": "200m"}),
    )
    wl = WorkloadSnapshot(
        name="edge-gateway",
        namespace="ust-prod",
        component="gateway",
        containers=[c],
        replicas=1,
    )
    return ClusterWorkloadSnapshot(
        namespace="ust-prod",
        workloads={"edge-gateway": wl},
        config_maps={"app-config": {"ENV": "prod"}},
        captured_at=clock.now(),
    )


def test_controller_initialization_defaults() -> None:
    """Verify default initialization and lazy property instantiations."""
    with (
        patch("understudy.fleet.controller.get_settings") as mock_settings,
        patch("understudy.fleet.controller.K8sWorkloadReader") as mock_reader_cls,
        patch("understudy.fleet.controller.TwinManifestRenderer") as mock_renderer_cls,
        patch("understudy.fleet.controller.KubectlDatabaseExecutor") as mock_exec_cls,
        patch("understudy.fleet.controller.PostgresDatabaseCloner") as mock_cloner_cls,
        patch("understudy.fleet.controller.get_k8s_core_client") as mock_core_fn,
        patch("understudy.fleet.controller.get_k8s_apps_client") as mock_apps_fn,
        patch("understudy.fleet.controller.get_k8s_networking_client") as mock_net_fn,
        patch("understudy.fleet.controller.get_k8s_rbac_client") as mock_rbac_fn,
    ):
        settings = Settings(
            timeouts=TimeoutSettings(fork_seconds=90),
            cluster=ClusterSettings(context="k3d-custom"),
        )
        mock_settings.return_value = settings

        controller = K8sFleetController()
        assert controller.readiness_timeout_seconds == 90.0
        assert controller.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS
        assert controller.api_timeout_seconds == DEFAULT_API_TIMEOUT_SECONDS

        # Test lazy properties
        _ = controller.workload_reader
        mock_reader_cls.assert_called_once()

        _ = controller.manifest_renderer
        mock_renderer_cls.assert_called_once()

        _ = controller.database_cloner
        mock_exec_cls.assert_called_once()
        mock_cloner_cls.assert_called_once()

        _ = controller.core_api
        mock_core_fn.assert_called_once_with(context="k3d-custom")

        _ = controller.apps_api
        mock_apps_fn.assert_called_once_with(context="k3d-custom")

        _ = controller.networking_api
        mock_net_fn.assert_called_once_with(context="k3d-custom")

        _ = controller.rbac_api
        mock_rbac_fn.assert_called_once_with(context="k3d-custom")

        _ = controller.teardown_manager
        assert controller.teardown_manager is not None


def test_controller_initialization_explicit() -> None:
    """Verify explicit dependency injection into controller."""
    reader = MagicMock()
    renderer = MagicMock()
    cloner = MagicMock()
    teardown_mgr = MagicMock()
    core = MagicMock()
    apps = MagicMock()
    net = MagicMock()
    rbac = MagicMock()
    clock = FrozenClock()
    settings = Settings(timeouts=TimeoutSettings(fork_seconds=0))

    controller = K8sFleetController(
        workload_reader=reader,
        manifest_renderer=renderer,
        database_cloner=cloner,
        teardown_manager=teardown_mgr,
        core_api=core,
        apps_api=apps,
        networking_api=net,
        rbac_api=rbac,
        settings=settings,
        clock=clock,
        readiness_timeout_seconds=45.0,
        poll_interval_seconds=0.005,
        api_timeout_seconds=5.0,
    )

    assert controller.workload_reader is reader
    assert controller.manifest_renderer is renderer
    assert controller.database_cloner is cloner
    assert controller.teardown_manager is teardown_mgr
    assert controller.core_api is core
    assert controller.apps_api is apps
    assert controller.networking_api is net
    assert controller.rbac_api is rbac
    assert controller.readiness_timeout_seconds == 45.0
    assert controller.poll_interval_seconds == 0.005
    assert controller.api_timeout_seconds == 5.0


@pytest.mark.asyncio
async def test_fork_validation() -> None:
    """Verify input validation on fork arguments."""
    controller = K8sFleetController(
        workload_reader=MagicMock(),
        manifest_renderer=MagicMock(),
        database_cloner=MagicMock(),
    )

    with pytest.raises(FleetError, match="Incident ID cannot be empty"):
        await controller.fork("", 3)

    with pytest.raises(FleetError, match="Incident ID cannot be empty"):
        await controller.fork("   ", 3)

    with pytest.raises(FleetError, match="Twin count must be >= 1"):
        await controller.fork("inc_test", 0)

    with pytest.raises(FleetError, match="Twin count must be >= 1"):
        await controller.fork("inc_test", -1)


@pytest.mark.asyncio
async def test_fork_successful_concurrent() -> None:
    """Verify concurrent fork of N twins with manifest application and readiness."""
    clock = FrozenClock()
    snapshot = _make_snapshot(clock)

    workload_reader = MagicMock()
    workload_reader.read_workloads = AsyncMock(return_value=snapshot)

    database_cloner = MagicMock()

    async def mock_clone(incident_id: str, candidate_index: int) -> DatabaseCloneResult:
        return DatabaseCloneResult(
            database_name=f"twin_{incident_id}_{candidate_index}",
            incident_id=incident_id,
            candidate_index=candidate_index,
            forked_from_snapshot_at=clock.now(),
            cloned_at=clock.now(),
        )

    database_cloner.clone_twin_database = AsyncMock(side_effect=mock_clone)

    manifest_renderer = MagicMock()

    def mock_render(
        snapshot: ClusterWorkloadSnapshot,
        incident_id: str,
        candidate_index: int,
        database_name: str | None = None,
    ) -> TwinManifestBundle:
        _ = snapshot.namespace
        ns_name = f"ust-twin-{incident_id}-{candidate_index}"
        manifests = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns_name}},
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "understudy-twin"}},
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": "understudy-twin"},
            },
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "twin-egress-containment"},
                "spec": {"policyTypes": ["Egress"]},
            },
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "app-config"}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "edge-gateway"}},
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "edge-gateway"},
                "spec": {"replicas": 1},
            },
        ]
        return TwinManifestBundle(
            twin_id=f"twin_{incident_id}_{candidate_index}",
            incident_id=incident_id,
            candidate_index=candidate_index,
            namespace=ns_name,
            database_name=database_name or f"twin_{incident_id}_{candidate_index}",
            database_dsn="postgresql://postgres@twin-postgres:5432/twin_db",
            manifests=manifests,
        )

    manifest_renderer.render = MagicMock(side_effect=mock_render)

    core_api = MagicMock()
    apps_api = MagicMock()
    networking_api = MagicMock()
    rbac_api = MagicMock()

    # Mock K6 policy read
    mock_policy = MagicMock()
    mock_policy.spec.policy_types = ["Egress"]
    networking_api.read_namespaced_network_policy.return_value = mock_policy

    # Mock readiness responses: deployment ready with 1 replica and pod running/ready
    mock_dep = MagicMock()
    mock_dep.metadata.name = "edge-gateway"
    mock_dep.status.ready_replicas = 1
    mock_dep.status.available_replicas = 1
    dep_list = MagicMock()
    dep_list.items = [mock_dep]
    apps_api.list_namespaced_deployment.return_value = dep_list

    mock_pod = MagicMock()
    mock_pod.metadata.name = "edge-gateway-pod-1"
    mock_pod.status.phase = "Running"
    cs = MagicMock()
    cs.ready = True
    mock_pod.status.container_statuses = [cs]
    pod_list = MagicMock()
    pod_list.items = [mock_pod]
    core_api.list_namespaced_pod.return_value = pod_list

    controller = K8sFleetController(
        workload_reader=workload_reader,
        manifest_renderer=manifest_renderer,
        database_cloner=database_cloner,
        core_api=core_api,
        apps_api=apps_api,
        networking_api=networking_api,
        rbac_api=rbac_api,
        clock=clock,
        poll_interval_seconds=0.001,
        readiness_timeout_seconds=5.0,
    )

    twins = await controller.fork("inc_test", 3)

    assert len(twins) == 3
    for idx, twin in enumerate(twins):
        assert isinstance(twin, TwinHandle)
        assert twin.twin_id == f"twin_inc_test_{idx}"
        assert twin.candidate_index == idx
        assert twin.namespace == f"ust-twin-inc_test-{idx}"
        assert twin.database == f"twin_inc_test_{idx}"
        assert twin.state == "ready"
        assert twin.ready_at == clock.now()

    # Assert workload_reader called exactly once for prod namespace
    workload_reader.read_workloads.assert_called_once_with(
        namespace="ust-prod",
        exclude_components={"database"},
    )
    assert database_cloner.clone_twin_database.call_count == 3
    assert manifest_renderer.render.call_count == 3
    assert networking_api.read_namespaced_network_policy.call_count == 3


@pytest.mark.asyncio
async def test_apply_manifest_conflict_idempotence() -> None:
    """Verify 409 Conflict handling across all supported manifest types."""
    clock = FrozenClock()
    core_api = MagicMock()
    apps_api = MagicMock()
    networking_api = MagicMock()
    rbac_api = MagicMock()

    conflict_exc = ApiException(status=409, reason="Conflict")

    core_api.create_namespace.side_effect = conflict_exc
    core_api.create_namespaced_service_account.side_effect = conflict_exc
    rbac_api.create_namespaced_role_binding.side_effect = conflict_exc
    networking_api.create_namespaced_network_policy.side_effect = conflict_exc
    core_api.create_namespaced_config_map.side_effect = conflict_exc
    core_api.create_namespaced_service.side_effect = conflict_exc
    apps_api.create_namespaced_deployment.side_effect = conflict_exc

    controller = K8sFleetController(
        core_api=core_api,
        apps_api=apps_api,
        networking_api=networking_api,
        rbac_api=rbac_api,
        clock=clock,
    )

    ns = "ust-twin-test-0"

    # 1. Namespace 409 ignored
    await controller._apply_manifest({"kind": "Namespace", "metadata": {"name": ns}}, ns)
    core_api.create_namespace.assert_called_once()

    # 2. ServiceAccount 409 ignored
    await controller._apply_manifest({"kind": "ServiceAccount", "metadata": {"name": "sa"}}, ns)
    core_api.create_namespaced_service_account.assert_called_once()

    # 3. RoleBinding 409 ignored
    await controller._apply_manifest({"kind": "RoleBinding", "metadata": {"name": "rb"}}, ns)
    rbac_api.create_namespaced_role_binding.assert_called_once()

    # 4. NetworkPolicy 409 -> replaced
    await controller._apply_manifest({"kind": "NetworkPolicy", "metadata": {"name": "np"}}, ns)
    networking_api.replace_namespaced_network_policy.assert_called_once()

    # 5. ConfigMap 409 -> replaced
    await controller._apply_manifest({"kind": "ConfigMap", "metadata": {"name": "cm"}}, ns)
    core_api.replace_namespaced_config_map.assert_called_once()

    # 6. Service 409 ignored
    await controller._apply_manifest({"kind": "Service", "metadata": {"name": "svc"}}, ns)
    core_api.create_namespaced_service.assert_called_once()

    # 7. Deployment 409 -> replaced
    await controller._apply_manifest({"kind": "Deployment", "metadata": {"name": "dep"}}, ns)
    apps_api.replace_namespaced_deployment.assert_called_once()


@pytest.mark.asyncio
async def test_apply_manifest_non_409_exceptions() -> None:
    """Verify non-409 ApiExceptions for all resource handlers are re-raised as FleetError."""
    core_api = MagicMock()
    apps_api = MagicMock()
    networking_api = MagicMock()
    rbac_api = MagicMock()

    server_err = ApiException(status=500, reason="Internal Error")

    core_api.create_namespaced_service_account.side_effect = server_err
    rbac_api.create_namespaced_role_binding.side_effect = server_err
    networking_api.create_namespaced_network_policy.side_effect = server_err
    core_api.create_namespaced_config_map.side_effect = server_err
    core_api.create_namespaced_service.side_effect = server_err
    apps_api.create_namespaced_deployment.side_effect = server_err

    controller = K8sFleetController(
        core_api=core_api,
        apps_api=apps_api,
        networking_api=networking_api,
        rbac_api=rbac_api,
    )
    ns = "ust-twin-test-0"

    with pytest.raises(FleetError, match="Failed to apply ServiceAccount 'sa'"):
        await controller._apply_manifest({"kind": "ServiceAccount", "metadata": {"name": "sa"}}, ns)

    with pytest.raises(FleetError, match="Failed to apply RoleBinding 'rb'"):
        await controller._apply_manifest({"kind": "RoleBinding", "metadata": {"name": "rb"}}, ns)

    with pytest.raises(FleetError, match="Failed to apply NetworkPolicy 'np'"):
        await controller._apply_manifest({"kind": "NetworkPolicy", "metadata": {"name": "np"}}, ns)

    with pytest.raises(FleetError, match="Failed to apply ConfigMap 'cm'"):
        await controller._apply_manifest({"kind": "ConfigMap", "metadata": {"name": "cm"}}, ns)

    with pytest.raises(FleetError, match="Failed to apply Service 'svc'"):
        await controller._apply_manifest({"kind": "Service", "metadata": {"name": "svc"}}, ns)

    with pytest.raises(FleetError, match="Failed to apply Deployment 'dep'"):
        await controller._apply_manifest({"kind": "Deployment", "metadata": {"name": "dep"}}, ns)


@pytest.mark.asyncio
async def test_apply_manifest_errors() -> None:
    """Verify error wrapping during manifest application."""
    core_api = MagicMock()
    core_api.create_namespace.side_effect = ApiException(status=500, reason="Server Error")

    controller = K8sFleetController(core_api=core_api)

    # ApiException wrapped in FleetError
    with pytest.raises(FleetError, match="Failed to apply Namespace 'test-ns'"):
        await controller._apply_manifest(
            {"kind": "Namespace", "metadata": {"name": "test-ns"}}, "test-ns"
        )

    # Unsupported kind
    with pytest.raises(FleetError, match="Unsupported Kubernetes manifest kind 'DaemonSet'"):
        await controller._apply_manifest(
            {"kind": "DaemonSet", "metadata": {"name": "ds"}}, "test-ns"
        )

    # Unexpected non-ApiException
    core_api.create_namespace.side_effect = RuntimeError("Fatal socket break")
    with pytest.raises(FleetError, match="Unexpected error applying Namespace 'test-ns'"):
        await controller._apply_manifest(
            {"kind": "Namespace", "metadata": {"name": "test-ns"}}, "test-ns"
        )


@pytest.mark.asyncio
async def test_apply_manifest_replace_errors() -> None:
    """Verify errors when replacing 409 resources."""
    apps_api = MagicMock()
    apps_api.create_namespaced_deployment.side_effect = ApiException(status=409, reason="Conflict")
    apps_api.replace_namespaced_deployment.side_effect = ApiException(
        status=500, reason="Replace Failed"
    )

    controller = K8sFleetController(apps_api=apps_api)

    with pytest.raises(FleetError, match="Failed to apply Deployment 'my-dep'"):
        await controller._apply_manifest(
            {"kind": "Deployment", "metadata": {"name": "my-dep"}}, "test-ns"
        )


@pytest.mark.asyncio
async def test_k6_network_policy_confirmation() -> None:
    """Verify K6 invariant precondition assertions."""
    networking_api = MagicMock()
    controller = K8sFleetController(networking_api=networking_api)

    # Case 1: 404 policy missing
    networking_api.read_namespaced_network_policy.side_effect = ApiException(
        status=404, reason="Not Found"
    )
    with pytest.raises(FleetError, match="K6 egress containment invariant precondition failed"):
        await controller._confirm_k6_network_policy("ust-twin-test-0")

    # Case 2: Other ApiException
    networking_api.read_namespaced_network_policy.side_effect = ApiException(
        status=500, reason="Internal Error"
    )
    with pytest.raises(FleetError, match="Failed to verify NetworkPolicy"):
        await controller._confirm_k6_network_policy("ust-twin-test-0")

    # Case 3: Unexpected generic error
    networking_api.read_namespaced_network_policy.side_effect = RuntimeError("IO error")
    with pytest.raises(FleetError, match="Unexpected error verifying NetworkPolicy"):
        await controller._confirm_k6_network_policy("ust-twin-test-0")

    # Case 4: Policy present but missing Egress
    networking_api.read_namespaced_network_policy.side_effect = None
    mock_policy = MagicMock()
    mock_policy.spec.policy_types = ["Ingress"]
    networking_api.read_namespaced_network_policy.return_value = mock_policy

    with pytest.raises(FleetError, match=r"does not enforce Egress.*K6 invariant violated"):
        await controller._confirm_k6_network_policy("ust-twin-test-0")

    # Case 5: Policy present with spec=None
    mock_policy.spec = None
    with pytest.raises(FleetError, match=r"does not enforce Egress.*K6 invariant violated"):
        await controller._confirm_k6_network_policy("ust-twin-test-0")

    # Case 6: Policy present and valid
    mock_policy.spec = MagicMock()
    mock_policy.spec.policy_types = ["Egress"]
    await controller._confirm_k6_network_policy("ust-twin-test-0")


@pytest.mark.asyncio
async def test_readiness_polling_empty_manifests() -> None:
    """Verify _wait_for_readiness returns immediately when no deployments are expected."""
    apps_api = MagicMock()
    controller = K8sFleetController(apps_api=apps_api)
    await controller._wait_for_readiness("test-ns", [], timeout=5.0)
    apps_api.list_namespaced_deployment.assert_not_called()


@pytest.mark.asyncio
async def test_readiness_polling_api_error() -> None:
    """Verify ApiException during polling raises FleetError."""
    apps_api = MagicMock()
    apps_api.list_namespaced_deployment.side_effect = ApiException(
        status=500, reason="Kube API down"
    )
    controller = K8sFleetController(apps_api=apps_api)

    deps = [{"metadata": {"name": "app-dep"}, "spec": {"replicas": 1}}]
    with pytest.raises(FleetError, match="Error polling readiness in namespace 'test-ns'"):
        await controller._wait_for_readiness("test-ns", deps, timeout=5.0)


@pytest.mark.asyncio
async def test_readiness_polling_retry_then_success() -> None:
    """Verify polling loop retries when unready, sleeps, and returns on eventual ready state."""
    apps_api = MagicMock()
    core_api = MagicMock()

    dep_unready = MagicMock()
    dep_unready.metadata.name = "app"
    dep_unready.status.ready_replicas = 0
    dep_unready.status.available_replicas = 0

    dep_ready = MagicMock()
    dep_ready.metadata.name = "app"
    dep_ready.status.ready_replicas = 1
    dep_ready.status.available_replicas = 1

    # First call unready, second call ready
    apps_api.list_namespaced_deployment.side_effect = [
        MagicMock(items=[dep_unready]),
        MagicMock(items=[dep_ready]),
    ]

    pod = MagicMock()
    pod.metadata.name = "app-pod"
    pod.status.phase = "Running"
    cs = MagicMock()
    cs.ready = True
    pod.status.container_statuses = [cs]
    core_api.list_namespaced_pod.return_value = MagicMock(items=[pod])

    controller = K8sFleetController(
        apps_api=apps_api,
        core_api=core_api,
        poll_interval_seconds=0.001,
    )
    expected = [{"metadata": {"name": "app"}, "spec": {"replicas": 1}}]

    # Should succeed after 1 sleep
    await controller._wait_for_readiness("test-ns", expected, timeout=2.0)
    assert apps_api.list_namespaced_deployment.call_count == 2


@pytest.mark.asyncio
async def test_readiness_polling_timeout_reporting() -> None:
    """Verify readiness timeout reports diagnostic info about unready workloads."""
    apps_api = MagicMock()
    core_api = MagicMock()

    # Case: deployment status None, missing deployment, unready pods
    dep1 = MagicMock()
    dep1.metadata.name = "dep1"
    dep1.status = None

    dep2 = MagicMock()
    dep2.metadata.name = "dep2"
    dep2.status.ready_replicas = 0
    dep2.status.available_replicas = 0

    dep_list = MagicMock()
    dep_list.items = [dep1, dep2]
    apps_api.list_namespaced_deployment.return_value = dep_list

    pod_list = MagicMock()
    pod_list.items = []
    core_api.list_namespaced_pod.return_value = pod_list

    controller = K8sFleetController(
        apps_api=apps_api,
        core_api=core_api,
        poll_interval_seconds=0.001,
    )

    expected = [
        {"metadata": {"name": "dep1"}, "spec": {"replicas": 1}},
        {"metadata": {"name": "dep2"}, "spec": {"replicas": 1}},
        {"metadata": {"name": "dep3-missing"}, "spec": {"replicas": 1}},
    ]

    with pytest.raises(
        FleetError, match=r"timed out waiting for readiness after 0\.0s"
    ) as exc_info:
        await controller._wait_for_readiness("test-ns", expected, timeout=0.0)

    msg = str(exc_info.value)
    assert "dep1 (missing)" in msg
    assert "dep2 (ready 0/1, avail 0)" in msg
    assert "dep3-missing (missing)" in msg


@pytest.mark.asyncio
async def test_readiness_polling_pod_states() -> None:
    """Verify pod phase and container readiness checks in polling loop."""
    apps_api = MagicMock()
    core_api = MagicMock()

    dep = MagicMock()
    dep.metadata.name = "app"
    dep.status.ready_replicas = 1
    dep.status.available_replicas = 1
    apps_api.list_namespaced_deployment.return_value = MagicMock(items=[dep])

    controller = K8sFleetController(
        apps_api=apps_api,
        core_api=core_api,
        poll_interval_seconds=0.001,
    )
    expected = [{"metadata": {"name": "app"}, "spec": {"replicas": 1}}]

    # Subcase A: Pod in Pending phase
    pod_pending = MagicMock()
    pod_pending.metadata.name = "pod-pending"
    pod_pending.status.phase = "Pending"
    core_api.list_namespaced_pod.return_value = MagicMock(items=[pod_pending])

    with pytest.raises(FleetError, match=r"pod/pod-pending \(Pending\)"):
        await controller._wait_for_readiness("test-ns", expected, timeout=0.0)

    # Subcase B: Pod running but no container_statuses
    pod_no_cs = MagicMock()
    pod_no_cs.metadata = None
    pod_no_cs.status.phase = "Running"
    pod_no_cs.status.container_statuses = []
    core_api.list_namespaced_pod.return_value = MagicMock(items=[pod_no_cs])

    with pytest.raises(FleetError, match=r"pod/unknown \(containers unready\)"):
        await controller._wait_for_readiness("test-ns", expected, timeout=0.0)

    # Subcase C: Pod running but container not ready
    pod_not_ready = MagicMock()
    pod_not_ready.metadata.name = "pod-crashing"
    pod_not_ready.status.phase = "Running"
    cs_not_ready = MagicMock()
    cs_not_ready.ready = False
    pod_not_ready.status.container_statuses = [cs_not_ready]
    core_api.list_namespaced_pod.return_value = MagicMock(items=[pod_not_ready])

    with pytest.raises(FleetError, match=r"pod/pod-crashing \(containers unready\)"):
        await controller._wait_for_readiness("test-ns", expected, timeout=0.0)


@pytest.mark.asyncio
async def test_teardown_single_twin() -> None:
    """Verify single twin teardown drops DB and deletes namespace."""
    core_api = MagicMock()
    database_cloner = MagicMock()
    database_cloner.drop_twin_database = AsyncMock()

    controller = K8sFleetController(
        core_api=core_api,
        database_cloner=database_cloner,
    )

    handle = TwinHandle(
        twin_id="twin_inc_1_0",
        incident_id="inc_1",
        candidate_index=0,
        namespace="ust-twin-inc_1-0",
        database="twin_inc_1_0",
        forked_from_snapshot_at=datetime.now(),
        ready_at=datetime.now(),
        state="ready",
    )

    # Success case
    await controller.teardown(handle)
    database_cloner.drop_twin_database.assert_called_once_with("twin_inc_1_0")
    core_api.delete_namespace.assert_called_once_with(
        name="ust-twin-inc_1-0",
        _request_timeout=DEFAULT_API_TIMEOUT_SECONDS,
    )

    # Case: 404 ignored
    core_api.delete_namespace.side_effect = ApiException(status=404, reason="Not Found")
    await controller.teardown(handle)

    # Case: other ApiException raised
    core_api.delete_namespace.side_effect = ApiException(status=500, reason="Delete failed")
    with pytest.raises(FleetError, match="Failed to delete namespace 'ust-twin-inc_1-0'"):
        await controller.teardown(handle)


@pytest.mark.asyncio
async def test_teardown_all_incident() -> None:
    """Verify teardown_all drops all incident DBs and deletes labeled namespaces."""
    core_api = MagicMock()
    database_cloner = MagicMock()
    database_cloner.drop_all_incident_databases = AsyncMock(return_value=["db1", "db2"])

    controller = K8sFleetController(
        core_api=core_api,
        database_cloner=database_cloner,
    )

    # Validation
    with pytest.raises(FleetError, match="Incident ID cannot be empty"):
        await controller.teardown_all("")

    # Setup namespaces matching selector
    ns1 = MagicMock()
    ns1.metadata.name = "ust-twin-inc_1-0"
    ns2 = MagicMock()
    ns2.metadata.name = "ust-twin-inc_1-1"
    ns_empty = MagicMock()
    ns_empty.metadata = None

    core_api.list_namespace.return_value = MagicMock(items=[ns1, ns2, ns_empty])

    await controller.teardown_all("inc_1")

    database_cloner.drop_all_incident_databases.assert_called_once_with("inc_1")
    assert core_api.delete_namespace.call_count == 2

    # Case: namespace delete 404 ignored
    core_api.delete_namespace.side_effect = ApiException(status=404, reason="Not Found")
    await controller.teardown_all("inc_1")

    # Case: namespace delete other ApiException raised
    core_api.delete_namespace.side_effect = ApiException(status=500, reason="Failed")
    with pytest.raises(
        FleetError, match="Failed to query or delete namespaces for incident 'inc_1'"
    ):
        await controller.teardown_all("inc_1")

    # Case: list_namespace ApiException 404 ignored
    core_api.list_namespace.side_effect = ApiException(status=404, reason="Not Found")
    await controller.teardown_all("inc_1")

    # Case: list_namespace ApiException (non-404) raised
    core_api.list_namespace.side_effect = ApiException(status=500, reason="List failed")
    with pytest.raises(
        FleetError, match="Failed to query or delete namespaces for incident 'inc_1'"
    ):
        await controller.teardown_all("inc_1")


def test_camelize_dict_and_keys() -> None:
    """Validate _camelize_dict and _camelize_key recursion."""
    from understudy.fleet.controller import _camelize_dict, _camelize_key

    assert _camelize_key("simple") == "simple"
    assert _camelize_key("initial_delay_seconds") == "initialDelaySeconds"
    assert _camelize_key("http_get") == "httpGet"

    data = {
        "http_get": {
            "path": "/healthz",
            "port": 8000,
            "http_headers": [{"name": "Accept", "value": "json"}],
        },
        "initial_delay_seconds": 5,
        "none_value": None,
        "scalar_number": 42,
    }
    camelized = _camelize_dict(data)
    assert camelized == {
        "httpGet": {
            "path": "/healthz",
            "port": 8000,
            "httpHeaders": [{"name": "Accept", "value": "json"}],
        },
        "initialDelaySeconds": 5,
        "scalarNumber": 42,
    }
    assert _camelize_dict("raw_string") == "raw_string"


@pytest.mark.asyncio
async def test_apply_deployment_camelizes_probes() -> None:
    """Verify _apply_deployment transforms snake_case probe dicts to camelCase."""
    apps_api = MagicMock()
    controller = K8sFleetController(apps_api=apps_api)

    manifest = {
        "metadata": {"name": "test-dep"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "c1",
                            "livenessProbe": {
                                "initial_delay_seconds": 5,
                                "http_get": {"path": "/"},
                            },
                            "readinessProbe": {"period_seconds": 10},
                        },
                        {
                            "name": "c2",
                            "livenessProbe": {"initial_delay_seconds": 3},
                        },
                        {
                            "name": "c3",
                            "readinessProbe": {"period_seconds": 5},
                        },
                        {
                            "name": "c4",
                            "livenessProbe": "non_dict",
                            "readinessProbe": None,
                        },
                        {
                            "name": "c5",
                        },
                    ]
                }
            }
        },
    }

    await controller._apply_deployment(manifest, "test-ns")
    apps_api.create_namespaced_deployment.assert_called_once()
    called_body = apps_api.create_namespaced_deployment.call_args[1]["body"]
    containers = called_body["spec"]["template"]["spec"]["containers"]
    assert "initialDelaySeconds" in containers[0]["livenessProbe"]
    assert "httpGet" in containers[0]["livenessProbe"]
    assert "periodSeconds" in containers[0]["readinessProbe"]
    assert "initialDelaySeconds" in containers[1]["livenessProbe"]
    assert "periodSeconds" in containers[2]["readinessProbe"]
    assert containers[3]["livenessProbe"] == "non_dict"
