"""Unit tests for Kubernetes workload reader and digest resolver.

Enforces 100% line and branch coverage on fleet/k8s.py and fleet/models.py.
"""

from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from understudy.common.clock import FrozenClock
from understudy.common.errors import FleetError
from understudy.fleet.k8s import (
    K8sWorkloadReader,
    _extract_ports,
    _extract_probes,
    _extract_volumes,
    _parse_image_repo_and_tag,
    extract_config_map_refs,
    extract_env_vars,
    extract_resource_spec,
    get_k8s_apps_client,
    get_k8s_core_client,
    get_k8s_networking_client,
    get_k8s_rbac_client,
    read_prod_workloads,
    resolve_image_digest,
)
from understudy.fleet.models import (
    ClusterWorkloadSnapshot,
    ContainerSnapshot,
    EnvVar,
    ResourceSpec,
    WorkloadSnapshot,
)


def test_models_instantiation() -> None:
    """Verify immutability and attributes of fleet models."""
    res = ResourceSpec(requests={"cpu": "50m"}, limits={"cpu": "200m"})
    assert res.requests == {"cpu": "50m"}

    env = EnvVar(name="TEST_ENV", value="val")
    assert env.name == "TEST_ENV"

    cont = ContainerSnapshot(
        name="test-cont",
        image_tag="test:tag",
        image_digest="sha256:123",
        pinned_image="test@sha256:123",
        resources=res,
    )
    assert cont.name == "test-cont"

    clock = FrozenClock()
    wl = WorkloadSnapshot(
        name="test-wl",
        namespace="test-ns",
        component="data",
        containers=[cont],
    )
    assert wl.component == "data"

    cluster_snap = ClusterWorkloadSnapshot(
        namespace="test-ns",
        workloads={"test-wl": wl},
        config_maps={"app-config": {"K": "V"}},
        captured_at=clock.now(),
    )
    assert cluster_snap.namespace == "test-ns"
    assert cluster_snap.captured_at == clock.now()


def test_parse_image_repo_and_tag() -> None:
    """Test splitting image strings into base repo and tag/digest."""
    assert _parse_image_repo_and_tag("repo@sha256:abc") == ("repo", "sha256:abc")
    assert _parse_image_repo_and_tag("localhost:5001/data-service:good") == (
        "localhost:5001/data-service",
        "good",
    )
    assert _parse_image_repo_and_tag("localhost:5001/data-service") == (
        "localhost:5001/data-service",
        None,
    )
    assert _parse_image_repo_and_tag("nginx") == ("nginx", None)


def test_resolve_image_digest_already_pinned() -> None:
    """Test resolution when the deployment template already contains a pinned digest."""
    pinned, digest = resolve_image_digest("service@sha256:abc123def456", None)
    assert pinned == "service@sha256:abc123def456"
    assert digest == "sha256:abc123def456"


def test_resolve_image_digest_missing_status() -> None:
    """Test error when container status imageID is missing and image is not pinned."""
    with pytest.raises(FleetError, match="imageID is missing"):
        resolve_image_digest("service:latest", None)
    with pytest.raises(FleetError, match="imageID is missing"):
        resolve_image_digest("service:latest", "")


def test_resolve_image_digest_formats() -> None:
    """Test parsing various container runtime imageID formats."""
    hex_digest = (
        "11112222333344445555666677778888"  # pragma: allowlist secret
        "99990000aaaabbbbccccddddeeeeffff"
    )
    # Standard containerd / k3s format
    p, d = resolve_image_digest(
        "localhost:5001/svc:good",
        f"localhost:5001/svc@sha256:{hex_digest}",
    )
    assert p == f"localhost:5001/svc@sha256:{hex_digest}"
    assert d == f"sha256:{hex_digest}"

    # docker-pullable prefix
    p2, d2 = resolve_image_digest(
        "localhost:5001/svc:good",
        f"docker-pullable://localhost:5001/svc@sha256:{hex_digest}",
    )
    assert p2 == p
    assert d2 == d

    # containerd digest format
    p3, d3 = resolve_image_digest(
        "svc:good",
        f"containerd://sha256:{hex_digest}",
    )
    assert p3 == f"svc@sha256:{hex_digest}"
    assert d3 == f"sha256:{hex_digest}"

    # docker:// prefix with sha256
    p4, d4 = resolve_image_digest(
        "svc:good",
        f"docker://sha256:{hex_digest}",
    )
    assert p4 == f"svc@sha256:{hex_digest}"
    assert d4 == f"sha256:{hex_digest}"

    # 64-character raw hex string
    p5, d5 = resolve_image_digest("svc:good", hex_digest)
    assert p5 == f"svc@sha256:{hex_digest}"
    assert d5 == f"sha256:{hex_digest}"

    # Target repo fallback when spec repo is empty
    p6, d6 = resolve_image_digest("", "remote.io/repo@sha256:abc")
    assert p6 == "remote.io/repo@sha256:abc"
    assert d6 == "sha256:abc"


def test_resolve_image_digest_invalid() -> None:
    """Test errors for malformed digests."""
    with pytest.raises(FleetError, match="Invalid image digest algorithm"):
        resolve_image_digest("svc:good", "svc@md5:12345")

    with pytest.raises(FleetError, match="Unable to extract sha256 digest"):
        resolve_image_digest("svc:good", "not-a-valid-digest-string")


def test_extract_resource_spec() -> None:
    """Test extracting CPU and memory resource requirements."""
    assert extract_resource_spec(None) == ResourceSpec()

    reqs = client.V1ResourceRequirements(
        requests={"cpu": "100m", "memory": "128Mi"},
        limits={"cpu": "500m", "memory": "400Mi"},
    )
    spec = extract_resource_spec(reqs)
    assert spec.requests == {"cpu": "100m", "memory": "128Mi"}
    assert spec.limits == {"cpu": "500m", "memory": "400Mi"}


def test_extract_env_vars() -> None:
    """Test extracting plain and valueFrom environment variables."""
    assert extract_env_vars(None) == []

    env_items = [
        client.V1EnvVar(name="LITERAL", value="value1"),
        client.V1EnvVar(
            name="CM_KEY",
            value_from=client.V1EnvVarSource(
                config_map_key_ref=client.V1ConfigMapKeySelector(
                    name="app-config", key="SEED", optional=False
                )
            ),
        ),
        client.V1EnvVar(
            name="SEC_KEY",
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name="app-secret", key="TOKEN", optional=True
                )
            ),
        ),
        client.V1EnvVar(
            name="FIELD_REF",
            value_from=client.V1EnvVarSource(
                field_ref=client.V1ObjectFieldSelector(
                    field_path="metadata.namespace", api_version="v1"
                )
            ),
        ),
        client.V1EnvVar(
            name="EMPTY_VF",
            value_from=client.V1EnvVarSource(),
        ),
    ]
    extracted = extract_env_vars(env_items)
    assert len(extracted) == 5
    assert extracted[0].name == "LITERAL"
    assert extracted[0].value == "value1"
    assert extracted[0].value_from is None

    assert extracted[1].value_from is not None
    assert extracted[1].value_from["configMapKeyRef"]["name"] == "app-config"

    assert extracted[2].value_from is not None
    assert extracted[2].value_from["secretKeyRef"]["name"] == "app-secret"

    assert extracted[3].value_from is not None
    assert extracted[3].value_from["fieldRef"]["fieldPath"] == "metadata.namespace"

    assert extracted[4].name == "EMPTY_VF"
    assert extracted[4].value_from is None


def test_extract_config_map_refs() -> None:
    """Test extracting all ConfigMap references from pod spec."""
    pod_spec = client.V1PodSpec(
        containers=[
            client.V1Container(
                name="c1",
                env=[
                    client.V1EnvVar(
                        name="VAR1",
                        value_from=client.V1EnvVarSource(
                            config_map_key_ref=client.V1ConfigMapKeySelector(name="cm-env", key="k")
                        ),
                    ),
                    client.V1EnvVar(
                        name="VAR_NO_NAME",
                        value_from=client.V1EnvVarSource(
                            config_map_key_ref=client.V1ConfigMapKeySelector(name="", key="k")
                        ),
                    ),
                    client.V1EnvVar(name="VAR_LITERAL", value="abc"),
                ],
                env_from=[
                    client.V1EnvFromSource(
                        config_map_ref=client.V1ConfigMapEnvSource(name="cm-envfrom")
                    ),
                    client.V1EnvFromSource(config_map_ref=client.V1ConfigMapEnvSource(name="")),
                ],
            )
        ],
        init_containers=[
            client.V1Container(
                name="init-c",
                env=[
                    client.V1EnvVar(
                        name="INIT_VAR",
                        value_from=client.V1EnvVarSource(
                            config_map_key_ref=client.V1ConfigMapKeySelector(
                                name="cm-init", key="k"
                            )
                        ),
                    )
                ],
            )
        ],
        volumes=[
            client.V1Volume(
                name="vol-cm",
                config_map=client.V1ConfigMapVolumeSource(name="cm-volume"),
            ),
            client.V1Volume(
                name="vol-cm-empty",
                config_map=client.V1ConfigMapVolumeSource(name=""),
            ),
            client.V1Volume(
                name="vol-projected",
                projected=client.V1ProjectedVolumeSource(
                    sources=[
                        client.V1VolumeProjection(
                            config_map=client.V1ConfigMapProjection(name="cm-proj")
                        ),
                        client.V1VolumeProjection(config_map=client.V1ConfigMapProjection(name="")),
                        client.V1VolumeProjection(),
                    ]
                ),
            ),
        ],
    )
    refs = extract_config_map_refs(pod_spec)
    assert refs == ["cm-env", "cm-envfrom", "cm-init", "cm-proj", "cm-volume"]


def test_extract_probes_and_ports_and_volumes() -> None:
    """Test extracting container probe definitions, ports, and volumes."""
    c_http = client.V1Container(
        name="c1",
        ports=[client.V1ContainerPort(container_port=8080, name="http", protocol="TCP")],
        liveness_probe=client.V1Probe(
            http_get=client.V1HTTPGetAction(path="/healthz", port=8080, scheme="HTTP"),
            initial_delay_seconds=2,
            period_seconds=5,
            timeout_seconds=1,
            failure_threshold=3,
            success_threshold=1,
        ),
        readiness_probe=client.V1Probe(
            _exec=client.V1ExecAction(command=["echo", "ready"]),
            initial_delay_seconds=3,
            period_seconds=6,
            timeout_seconds=2,
            failure_threshold=4,
            success_threshold=1,
        ),
    )
    lp, rp = _extract_probes(c_http)
    assert lp is not None
    assert lp["http_get"]["path"] == "/healthz"
    assert rp is not None
    assert rp["exec"]["command"] == ["echo", "ready"]

    # Container with empty probes and ports
    c_empty = client.V1Container(name="empty")
    lp_empty, rp_empty = _extract_probes(c_empty)
    assert lp_empty is None
    assert rp_empty is None
    assert _extract_ports(c_empty) == []

    # Container with other probe type (e.g. tcp_socket)
    c_tcp = client.V1Container(
        name="tcp",
        liveness_probe=client.V1Probe(
            tcp_socket=client.V1TCPSocketAction(port=8000),
            period_seconds=5,
        ),
        readiness_probe=client.V1Probe(
            tcp_socket=client.V1TCPSocketAction(port=8000),
            period_seconds=5,
        ),
    )
    lp_tcp, rp_tcp = _extract_probes(c_tcp)
    assert lp_tcp == {
        "period_seconds": 5,
        "tcp_socket": {"host": None, "port": 8000},
    }
    assert rp_tcp is not None

    ports = _extract_ports(c_http)
    assert ports == [{"containerPort": 8080, "name": "http", "protocol": "TCP"}]

    pod_spec = client.V1PodSpec(
        containers=[c_http],
        volumes=[
            client.V1Volume(name="v1", config_map=client.V1ConfigMapVolumeSource(name="cm1")),
            client.V1Volume(name="v2", secret=client.V1SecretVolumeSource(secret_name="sec1")),
            client.V1Volume(name="v3", empty_dir=client.V1EmptyDirVolumeSource()),
        ],
    )
    vols = _extract_volumes(pod_spec)
    assert len(vols) == 3
    assert vols[0]["configMap"]["name"] == "cm1"
    assert vols[1]["secret"]["secretName"] == "sec1"  # pragma: allowlist secret
    assert vols[2]["emptyDir"] == {}

    empty_spec = client.V1PodSpec(containers=[c_http], volumes=None)
    assert _extract_volumes(empty_spec) == []


def test_get_k8s_clients() -> None:
    """Test client acquisition helpers with in-cluster and kubeconfig fallbacks."""
    with patch("kubernetes.config.load_incluster_config") as mock_incluster:
        apps = get_k8s_apps_client()
        assert isinstance(apps, client.AppsV1Api)
        core = get_k8s_core_client()
        assert isinstance(core, client.CoreV1Api)
        net = get_k8s_networking_client()
        assert isinstance(net, client.NetworkingV1Api)
        rbac = get_k8s_rbac_client()
        assert isinstance(rbac, client.RbacAuthorizationV1Api)
        mock_incluster.assert_called()

    with (
        patch("kubernetes.config.load_incluster_config", side_effect=config.ConfigException),
        patch("kubernetes.config.load_kube_config") as mock_kube,
    ):
        apps2 = get_k8s_apps_client(context="custom-context")
        assert isinstance(apps2, client.AppsV1Api)
        core2 = get_k8s_core_client(context="custom-context")
        assert isinstance(core2, client.CoreV1Api)
        net2 = get_k8s_networking_client(context="custom-context")
        assert isinstance(net2, client.NetworkingV1Api)
        rbac2 = get_k8s_rbac_client(context="custom-context")
        assert isinstance(rbac2, client.RbacAuthorizationV1Api)
        mock_kube.assert_called_with(context="custom-context")


@pytest.mark.asyncio
async def test_k8s_workload_reader_lazy_properties() -> None:
    """Test lazy initialization of apps_api and core_api properties."""
    with (
        patch("understudy.fleet.k8s.get_k8s_apps_client") as mock_apps,
        patch("understudy.fleet.k8s.get_k8s_core_client") as mock_core,
    ):
        reader = K8sWorkloadReader(context="test-ctx")
        _ = reader.apps_api
        _ = reader.core_api
        mock_apps.assert_called_once_with(context="test-ctx")
        mock_core.assert_called_once_with(context="test-ctx")


@pytest.mark.asyncio
async def test_k8s_workload_reader_api_error() -> None:
    """Test error handling when Kubernetes API raises ApiException."""
    apps_mock = MagicMock()
    core_mock = MagicMock()
    apps_mock.list_namespaced_deployment.side_effect = ApiException(
        status=500, reason="Cluster Down"
    )

    reader = K8sWorkloadReader(apps_api=apps_mock, core_api=core_mock)
    with pytest.raises(FleetError, match="Failed to query Kubernetes API"):
        await reader.read_workloads("ust-prod")

    # Generic exception handling
    apps_mock.list_namespaced_deployment.side_effect = RuntimeError("Connection dropped")
    with pytest.raises(FleetError, match="Unexpected error querying Kubernetes API"):
        await reader.read_workloads("ust-prod")


@pytest.mark.asyncio
async def test_k8s_workload_reader_success() -> None:
    """Test successful reading and parsing of deployments, pods, and config maps."""
    clock = FrozenClock()
    apps_mock = MagicMock()
    core_mock = MagicMock()

    dep = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name="data-service",
            labels={"app": "data-service", "app.kubernetes.io/component": "data"},
            annotations={"prometheus.io/scrape": "true"},
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"app": "data-service"}),
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name="data-service",
                            image="localhost:5001/data-service:good",
                            env=[
                                client.V1EnvVar(
                                    name="SEED",
                                    value_from=client.V1EnvVarSource(
                                        config_map_key_ref=client.V1ConfigMapKeySelector(
                                            name="app-config", key="SEED"
                                        )
                                    ),
                                )
                            ],
                        )
                    ]
                )
            ),
        ),
    )

    dep_db = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name="prod-postgres",
            labels={"app": "prod-postgres", "app.kubernetes.io/component": "database"},
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"app": "prod-postgres"}),
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name="postgres",
                            image="postgres@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685",
                        )
                    ]
                )
            ),
        ),
    )

    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name="data-service-abc",
            labels={"app": "data-service"},
        ),
        status=client.V1PodStatus(
            phase="Running",
            container_statuses=[
                client.V1ContainerStatus(
                    name="data-service",
                    image="localhost:5001/data-service:good",
                    image_id="localhost:5001/data-service@sha256:1ead9b47c5fe97d3551cc5001844061122dcc49388dcf77ebda51a3b6301f6b7",
                    ready=True,
                    restart_count=0,
                )
            ],
        ),
    )

    apps_mock.list_namespaced_deployment.return_value = client.V1DeploymentList(items=[dep, dep_db])
    core_mock.list_namespaced_pod.return_value = client.V1PodList(items=[pod])

    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name="app-config"),
        data={"SEED": "1337"},
    )
    core_mock.read_namespaced_config_map.return_value = cm

    reader = K8sWorkloadReader(apps_api=apps_mock, core_api=core_mock, clock=clock)
    snapshot = await reader.read_workloads("ust-prod", exclude_components={"database"})

    assert snapshot.namespace == "ust-prod"
    assert "data-service" in snapshot.workloads
    assert "prod-postgres" not in snapshot.workloads
    ds = snapshot.workloads["data-service"]
    assert ds.name == "data-service"
    assert ds.component == "data"
    assert len(ds.containers) == 1
    assert ds.containers[0].pinned_image.startswith("localhost:5001/data-service@sha256:")
    assert snapshot.config_maps == {"app-config": {"SEED": "1337"}}


@pytest.mark.asyncio
async def test_k8s_workload_reader_pod_matching_errors() -> None:
    """Test errors when pods or container statuses cannot be matched."""
    apps_mock = MagicMock()
    core_mock = MagicMock()

    dep = client.V1Deployment(
        metadata=client.V1ObjectMeta(name="svc-a", labels={"app": "svc-a"}),
        spec=client.V1DeploymentSpec(
            selector=client.V1LabelSelector(match_labels={"app": "svc-a"}),
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    containers=[client.V1Container(name="svc-a", image="svc-a:good")]
                )
            ),
        ),
    )
    apps_mock.list_namespaced_deployment.return_value = client.V1DeploymentList(items=[dep])

    # Case 1: No pods match selector
    core_mock.list_namespaced_pod.return_value = client.V1PodList(items=[])
    reader = K8sWorkloadReader(apps_api=apps_mock, core_api=core_mock)
    with pytest.raises(FleetError, match="No running pod with container statuses found"):
        await reader.read_workloads("ust-prod")

    # Case 2: Matching pod has no container statuses
    pod_no_status = client.V1Pod(
        metadata=client.V1ObjectMeta(name="svc-a-pod", labels={"app": "svc-a"}),
        status=client.V1PodStatus(phase="Pending", container_statuses=None),
    )
    core_mock.list_namespaced_pod.return_value = client.V1PodList(items=[pod_no_status])
    with pytest.raises(FleetError, match="No running pod with container statuses found"):
        await reader.read_workloads("ust-prod")

    # Case 3: Pod spec has no containers
    dep_no_containers = client.V1Deployment(
        metadata=client.V1ObjectMeta(name="svc-a", labels={"app": "svc-a"}),
        spec=client.V1DeploymentSpec(
            selector=client.V1LabelSelector(match_labels={"app": "svc-a"}),
            template=client.V1PodTemplateSpec(spec=client.V1PodSpec(containers=[])),
        ),
    )
    pod_ok = client.V1Pod(
        metadata=client.V1ObjectMeta(name="svc-a-pod", labels={"app": "svc-a"}),
        status=client.V1PodStatus(
            phase="Running",
            container_statuses=[
                client.V1ContainerStatus(
                    name="other",
                    image="other:good",
                    image_id="other@sha256:123",
                    ready=True,
                    restart_count=0,
                )
            ],
        ),
    )
    apps_mock.list_namespaced_deployment.return_value = client.V1DeploymentList(
        items=[dep_no_containers]
    )
    core_mock.list_namespaced_pod.return_value = client.V1PodList(items=[pod_ok])
    with pytest.raises(FleetError, match="has no containers in pod template"):
        await reader.read_workloads("ust-prod")

    # Case 4: Container status name does not match container spec name
    apps_mock.list_namespaced_deployment.return_value = client.V1DeploymentList(items=[dep])
    with pytest.raises(FleetError, match="has no matching container status in pod"):
        await reader.read_workloads("ust-prod")


@pytest.mark.asyncio
async def test_k8s_read_config_maps_errors() -> None:
    """Test reading config maps with 404, 500, and generic exceptions."""
    core_mock = MagicMock()
    reader = K8sWorkloadReader(core_api=core_mock)

    # Empty names
    assert await reader.read_config_maps("ust-prod", set()) == {}

    # 404 Not Found
    core_mock.read_namespaced_config_map.side_effect = ApiException(status=404, reason="Not Found")
    with pytest.raises(FleetError, match="Referenced ConfigMap 'cm-missing' not found"):
        await reader.read_config_maps("ust-prod", {"cm-missing"})

    # 500 Internal Error
    core_mock.read_namespaced_config_map.side_effect = ApiException(
        status=500, reason="Internal Error"
    )
    with pytest.raises(FleetError, match="Failed reading ConfigMap 'cm-err'"):
        await reader.read_config_maps("ust-prod", {"cm-err"})

    # Generic Exception
    core_mock.read_namespaced_config_map.side_effect = RuntimeError("Network partition")
    with pytest.raises(FleetError, match="Unexpected error reading ConfigMap 'cm-fail'"):
        await reader.read_config_maps("ust-prod", {"cm-fail"})


@pytest.mark.asyncio
async def test_read_prod_workloads_convenience() -> None:
    """Test convenience function read_prod_workloads delegating to K8sWorkloadReader."""
    clock = FrozenClock()
    fake_snapshot = ClusterWorkloadSnapshot(
        namespace="ust-prod",
        workloads={},
        config_maps={},
        captured_at=clock.now(),
    )
    with patch.object(K8sWorkloadReader, "read_workloads", return_value=fake_snapshot) as mock_read:
        res = await read_prod_workloads(exclude_components={"database"}, clock=clock)
        assert res.namespace == "ust-prod"
        mock_read.assert_called_once_with(namespace="ust-prod", exclude_components={"database"})
