"""Unit tests for Kubernetes workload reader and digest resolver.

Enforces 100% line and branch coverage on fleet/k8s.py and fleet/models.py.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from understudy.common.clock import FrozenClock
from understudy.common.errors import FleetError
from understudy.fleet.k8s import (
    K8sWorkloadReader,
    _camelize_dict,
    _camelize_key,
    _current_pod_template_hash,
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
    # Probes must be emitted in the manifest's camelCase spelling: the API server rejects
    # the client's snake_case attribute names outright.
    assert lp == {
        "httpGet": {"path": "/healthz", "port": 8080, "scheme": "HTTP"},
        "initialDelaySeconds": 2,
        "periodSeconds": 5,
        "timeoutSeconds": 1,
        "failureThreshold": 3,
        "successThreshold": 1,
    }
    assert rp is not None
    assert rp["exec"]["command"] == ["echo", "ready"]
    assert rp["initialDelaySeconds"] == 3

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
    # Unset nested fields (host) are dropped rather than serialized as null.
    assert lp_tcp == {
        "periodSeconds": 5,
        "tcpSocket": {"port": 8000},
    }
    assert rp_tcp is not None

    ports = _extract_ports(c_http)
    assert ports == [{"containerPort": 8080, "name": "http", "protocol": "TCP"}]

    pod_spec = client.V1PodSpec(
        containers=[c_http],
        volumes=[
            client.V1Volume(
                name="v1",
                config_map=client.V1ConfigMapVolumeSource(
                    name="cm1",
                    default_mode=420,
                    items=[client.V1KeyToPath(key="app.yaml", path="conf/app.yaml")],
                ),
            ),
            client.V1Volume(name="v2", secret=client.V1SecretVolumeSource(secret_name="sec1")),
            client.V1Volume(name="v3", empty_dir=client.V1EmptyDirVolumeSource()),
            client.V1Volume(
                name="v4",
                downward_api=client.V1DownwardAPIVolumeSource(
                    items=[
                        client.V1DownwardAPIVolumeFile(
                            path="labels",
                            field_ref=client.V1ObjectFieldSelector(field_path="metadata.labels"),
                        )
                    ]
                ),
            ),
            client.V1Volume(
                name="v5",
                projected=client.V1ProjectedVolumeSource(
                    sources=[
                        client.V1VolumeProjection(
                            config_map=client.V1ConfigMapProjection(name="cm2")
                        )
                    ]
                ),
            ),
        ],
    )
    vols = _extract_volumes(pod_spec)
    assert len(vols) == 5
    # items/defaultMode must survive: a workload mounting one key must not get all of them.
    assert vols[0]["configMap"] == {
        "name": "cm1",
        "defaultMode": 420,
        "items": [{"key": "app.yaml", "path": "conf/app.yaml"}],
    }
    assert vols[1]["secret"]["secretName"] == "sec1"  # pragma: allowlist secret
    assert vols[2]["emptyDir"] == {}
    assert vols[3]["downwardAPI"]["items"][0]["fieldRef"]["fieldPath"] == "metadata.labels"
    assert vols[4]["projected"]["sources"][0]["configMap"]["name"] == "cm2"

    empty_spec = client.V1PodSpec(containers=[c_http], volumes=None)
    assert _extract_volumes(empty_spec) == []


def test_camelize_dict_and_keys() -> None:
    """Validate the camelCase conversion every serialized manifest fragment relies on."""
    assert _camelize_key("simple") == "simple"
    assert _camelize_key("initial_delay_seconds") == "initialDelaySeconds"
    assert _camelize_key("http_get") == "httpGet"
    # The client spells the probe's exec handler '_exec'.
    assert _camelize_key("_exec") == "exec"

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
    assert _camelize_dict(data) == {
        "httpGet": {
            "path": "/healthz",
            "port": 8000,
            "httpHeaders": [{"name": "Accept", "value": "json"}],
        },
        "initialDelaySeconds": 5,
        "scalarNumber": 42,
    }
    assert _camelize_dict("raw_string") == "raw_string"


def test_extract_volumes_rejects_unsupported_source() -> None:
    """A volume a twin cannot honestly reproduce must fail the fork, not emit a bad manifest."""
    pvc_spec = client.V1PodSpec(
        containers=[client.V1Container(name="c1")],
        volumes=[
            client.V1Volume(
                name="data",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name="prod-data"
                ),
            )
        ],
    )
    with pytest.raises(FleetError, match="unsupported volume source"):
        _extract_volumes(pvc_spec)

    host_path_spec = client.V1PodSpec(
        containers=[client.V1Container(name="c1")],
        volumes=[
            client.V1Volume(name="hp", host_path=client.V1HostPathVolumeSource(path="/var/run"))
        ],
    )
    with pytest.raises(FleetError, match="hostPath"):
        _extract_volumes(host_path_spec)


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
async def test_k8s_workload_reader_unpinnable_and_invalid_workloads() -> None:
    """Test degradation when no pod can pin a digest, and failure on an empty pod template."""
    apps_mock = MagicMock()
    core_mock = MagicMock()
    apps_mock.list_namespaced_replica_set.return_value = client.V1ReplicaSetList(items=[])

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
    reader = K8sWorkloadReader(apps_api=apps_mock, core_api=core_mock)

    # Case 1: no pods match the selector at all. A prod deployment whose pods are all
    # unschedulable is the incident Understudy exists to remediate, so the snapshot degrades
    # to the spec's tag and records that the image is not digest-pinned.
    core_mock.list_namespaced_pod.return_value = client.V1PodList(items=[])
    snap = await reader.read_workloads("ust-prod")
    container = snap.workloads["svc-a"].containers[0]
    assert container.digest_pinned is False
    assert container.image_digest == ""
    assert container.pinned_image == "svc-a:good"

    # Case 2: the matching pod is Pending (ImagePullBackOff), so it has no imageID to read.
    pod_pending = client.V1Pod(
        metadata=client.V1ObjectMeta(name="svc-a-pod", labels={"app": "svc-a"}),
        status=client.V1PodStatus(phase="Pending", container_statuses=None),
    )
    core_mock.list_namespaced_pod.return_value = client.V1PodList(items=[pod_pending])
    snap = await reader.read_workloads("ust-prod")
    assert snap.workloads["svc-a"].containers[0].digest_pinned is False

    # Case 3: pod spec has no containers -- a genuinely unusable workload
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

    # Case 4: the only running pod reports a different image than the spec, so its digest
    # cannot be trusted for this deployment.
    apps_mock.list_namespaced_deployment.return_value = client.V1DeploymentList(items=[dep])
    snap = await reader.read_workloads("ust-prod")
    assert snap.workloads["svc-a"].containers[0].digest_pinned is False


def _running_pod(name: str, image: str, image_id: str, template_hash: str, start: int) -> Any:
    """Build a Running pod carrying a container status for the 'svc-a' container."""
    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=name,
            labels={"app": "svc-a", "pod-template-hash": template_hash},
        ),
        status=client.V1PodStatus(
            phase="Running",
            start_time=datetime(2026, 3, 1, 12, start, tzinfo=UTC),
            container_statuses=[
                client.V1ContainerStatus(
                    name="svc-a",
                    image=image,
                    image_id=image_id,
                    ready=True,
                    restart_count=0,
                )
            ],
        ),
    )


@pytest.mark.asyncio
async def test_k8s_workload_reader_pins_current_replica_set_during_rollout() -> None:
    """Mid-rollout, the digest must come from the Deployment's current ReplicaSet."""
    apps_mock = MagicMock()
    core_mock = MagicMock()

    dep = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name="svc-a",
            uid="dep-uid-1",
            labels={"app": "svc-a"},
            annotations={"deployment.kubernetes.io/revision": "2"},
        ),
        spec=client.V1DeploymentSpec(
            selector=client.V1LabelSelector(match_labels={"app": "svc-a"}),
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    containers=[client.V1Container(name="svc-a", image="svc-a:v2")]
                )
            ),
        ),
    )
    owner = client.V1OwnerReference(
        api_version="apps/v1", kind="Deployment", name="svc-a", uid="dep-uid-1", controller=True
    )
    old_rs = client.V1ReplicaSet(
        metadata=client.V1ObjectMeta(
            name="svc-a-old",
            labels={"pod-template-hash": "old"},
            annotations={"deployment.kubernetes.io/revision": "1"},
            owner_references=[owner],
        )
    )
    new_rs = client.V1ReplicaSet(
        metadata=client.V1ObjectMeta(
            name="svc-a-new",
            labels={"pod-template-hash": "new"},
            annotations={"deployment.kubernetes.io/revision": "2"},
            owner_references=[owner],
        )
    )
    unrelated_rs = client.V1ReplicaSet(metadata=client.V1ObjectMeta(name="other"))

    old_digest = "sha256:" + "a" * 64
    new_digest = "sha256:" + "b" * 64
    apps_mock.list_namespaced_deployment.return_value = client.V1DeploymentList(items=[dep])
    apps_mock.list_namespaced_replica_set.return_value = client.V1ReplicaSetList(
        items=[unrelated_rs, old_rs, new_rs]
    )
    # The old pod is listed first and started earlier; neither must win.
    core_mock.list_namespaced_pod.return_value = client.V1PodList(
        items=[
            _running_pod("svc-a-old-1", "svc-a:v1", f"svc-a@{old_digest}", "old", 0),
            _running_pod("svc-a-new-1", "svc-a:v2", f"svc-a@{new_digest}", "new", 5),
        ]
    )

    reader = K8sWorkloadReader(apps_api=apps_mock, core_api=core_mock)
    snap = await reader.read_workloads("ust-prod")
    container = snap.workloads["svc-a"].containers[0]
    assert container.image_digest == new_digest
    assert container.digest_pinned is True


@pytest.mark.asyncio
async def test_k8s_workload_reader_falls_back_to_image_matching_pod() -> None:
    """Without ReplicaSet information, only a pod running the spec's image may be trusted."""
    apps_mock = MagicMock()
    core_mock = MagicMock()

    dep = client.V1Deployment(
        metadata=client.V1ObjectMeta(name="svc-a", labels={"app": "svc-a"}),
        spec=client.V1DeploymentSpec(
            selector=client.V1LabelSelector(match_labels={"app": "svc-a"}),
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    containers=[client.V1Container(name="svc-a", image="svc-a:v2")]
                )
            ),
        ),
    )
    stale_digest = "sha256:" + "c" * 64
    current_digest = "sha256:" + "d" * 64
    apps_mock.list_namespaced_deployment.return_value = client.V1DeploymentList(items=[dep])
    apps_mock.list_namespaced_replica_set.return_value = client.V1ReplicaSetList(items=[])
    core_mock.list_namespaced_pod.return_value = client.V1PodList(
        items=[
            _running_pod("other-1", "sidecar:v9", f"sidecar@{stale_digest}", "x", 0),
            _running_pod("svc-a-1", "svc-a:v2", f"svc-a@{current_digest}", "y", 1),
        ]
    )

    reader = K8sWorkloadReader(apps_api=apps_mock, core_api=core_mock)
    snap = await reader.read_workloads("ust-prod")
    assert snap.workloads["svc-a"].containers[0].image_digest == current_digest


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_k8s_workload_reader_replica_set_resolution_edge_cases() -> None:
    """ReplicaSet lookups that cannot identify the current revision fall back to labels."""
    apps_mock = MagicMock()
    core_mock = MagicMock()

    digest = "sha256:" + "e" * 64
    pod = _running_pod("svc-a-1", "svc-a:v2", f"svc-a@{digest}", "hash-1", 0)
    core_mock.list_namespaced_pod.return_value = client.V1PodList(items=[pod])

    def _dep(uid: str | None, revision: str | None) -> client.V1Deployment:
        annotations = {"deployment.kubernetes.io/revision": revision} if revision else {}
        return client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name="svc-a", uid=uid, labels={"app": "svc-a"}, annotations=annotations
            ),
            spec=client.V1DeploymentSpec(
                selector=client.V1LabelSelector(match_labels={"app": "svc-a"}),
                template=client.V1PodTemplateSpec(
                    spec=client.V1PodSpec(
                        containers=[client.V1Container(name="svc-a", image="svc-a:v2")]
                    )
                ),
            ),
        )

    owner = client.V1OwnerReference(
        api_version="apps/v1", kind="Deployment", name="svc-a", uid="dep-uid-1", controller=True
    )
    reader = K8sWorkloadReader(apps_api=apps_mock, core_api=core_mock)

    # 1. Deployment carries no revision annotation, and 2. no uid: nothing to match a RS on.
    for dep in (_dep("dep-uid-1", None), _dep(None, "2")):
        apps_mock.list_namespaced_deployment.return_value = client.V1DeploymentList(items=[dep])
        apps_mock.list_namespaced_replica_set.return_value = client.V1ReplicaSetList(items=[])
        snap = await reader.read_workloads("ust-prod")
        assert snap.workloads["svc-a"].containers[0].image_digest == digest

    apps_mock.list_namespaced_deployment.return_value = client.V1DeploymentList(
        items=[_dep("dep-uid-1", "2")]
    )

    # 3. A ReplicaSet without metadata, one owned by another Deployment, and one stuck on an
    # older revision: none identifies the current revision.
    apps_mock.list_namespaced_replica_set.return_value = client.V1ReplicaSetList(
        items=[
            client.V1ReplicaSet(metadata=None),
            client.V1ReplicaSet(
                metadata=client.V1ObjectMeta(
                    name="other-rs",
                    owner_references=[
                        client.V1OwnerReference(
                            api_version="apps/v1",
                            kind="Deployment",
                            name="other",
                            uid="dep-uid-other",
                        )
                    ],
                    annotations={"deployment.kubernetes.io/revision": "2"},
                )
            ),
            client.V1ReplicaSet(
                metadata=client.V1ObjectMeta(
                    name="svc-a-old",
                    owner_references=[owner],
                    annotations={"deployment.kubernetes.io/revision": "1"},
                    labels={"pod-template-hash": "hash-0"},
                )
            ),
        ]
    )
    snap = await reader.read_workloads("ust-prod")
    assert snap.workloads["svc-a"].containers[0].image_digest == digest

    # 4. The current ReplicaSet is known but none of its pods are running yet, so the
    # label-matched pods are used rather than discarding the only digest available.
    apps_mock.list_namespaced_replica_set.return_value = client.V1ReplicaSetList(
        items=[
            client.V1ReplicaSet(
                metadata=client.V1ObjectMeta(
                    name="svc-a-new",
                    owner_references=[owner],
                    annotations={"deployment.kubernetes.io/revision": "2"},
                    labels={"pod-template-hash": "hash-new"},
                )
            )
        ]
    )
    snap = await reader.read_workloads("ust-prod")
    assert snap.workloads["svc-a"].containers[0].image_digest == digest


def test_current_pod_template_hash_without_metadata() -> None:
    """A Deployment with no metadata yields no revision to match ReplicaSets against."""
    assert _current_pod_template_hash(client.V1Deployment(metadata=None), []) is None
