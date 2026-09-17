"""Unit tests for Kubernetes remediation plan application engine (actuator/apply.py).

Enforces 100% statement and branch test coverage.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from understudy.actuator.apply import (
    ANNOTATION_PLAN_ID,
    ANNOTATION_REPLICA_DELTA,
    ANNOTATION_RESTART_TIMESTAMP,
    K8sPlanApplier,
    _extract_base_repo,
    apply_plan,
    create_k8s_clients,
    resolve_service_account,
    revert_plan,
)
from understudy.common.clock import FrozenClock
from understudy.common.config import Settings
from understudy.common.errors import ActuationError
from understudy.contracts.enums import ActionType
from understudy.contracts.incident import DeployRef
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.signals.api import DeployHistory

FIXED_TIME = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


class FakeDeployHistory(DeployHistory):
    """Deterministic deploy history fixture for unit tests."""

    def __init__(self, deploys: list[DeployRef] | None = None, raise_error: bool = False) -> None:
        self.deploys = deploys or []
        self.raise_error = raise_error

    async def recent_deploys(self, limit: int = 5) -> list[DeployRef]:
        if self.raise_error:
            raise RuntimeError("GitHub API connection error")
        return self.deploys[:limit]


def _make_deployment(
    workload: str = "data-service",
    image: str = "localhost:5001/data-service:good",
    replicas: int = 1,
    annotations: dict[str, str] | None = None,
    container_name: str | None = None,
    empty_containers: bool = False,
) -> client.V1Deployment:
    """Build a mock V1Deployment object."""
    c_name = container_name or workload
    containers = (
        []
        if empty_containers
        else [
            client.V1Container(
                name=c_name,
                image=image,
            )
        ]
    )
    dep = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=workload,
            annotations=dict(annotations or {}),
        ),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels={"app": workload}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    annotations=dict(annotations or {}),
                ),
                spec=client.V1PodSpec(containers=containers),
            ),
        ),
    )
    return dep


def _make_config_map(
    name: str = "feature-flags",
    data: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> client.V1ConfigMap:
    """Build a mock V1ConfigMap object."""
    return client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=name,
            annotations=dict(annotations or {}),
        ),
        data=dict(data or {}),
    )


# ---------------------------------------------------------------------------
# Helpers and ServiceAccount Resolution Tests
# ---------------------------------------------------------------------------


def test_resolve_service_account() -> None:
    # Explicit SA override
    assert resolve_service_account("ust-prod", "custom-sa") == "custom-sa"
    assert resolve_service_account("ust-twin-1", "custom-sa") == "custom-sa"

    # Default prod
    assert resolve_service_account("ust-prod") == "understudy-prod"

    # Default twin
    assert resolve_service_account("ust-twin-inc-1") == "understudy-twin"

    # Custom settings with different prod namespace
    custom_settings = Settings()
    assert resolve_service_account("ust-prod", settings=custom_settings) == "understudy-prod"


def test_extract_base_repo() -> None:
    assert (
        _extract_base_repo("localhost:5001/data-service@sha256:1234")
        == "localhost:5001/data-service"
    )
    assert _extract_base_repo("localhost:5001/data-service:good") == "localhost:5001/data-service"
    assert _extract_base_repo("localhost:5001/data-service") == "localhost:5001/data-service"
    assert _extract_base_repo("redis:7.0-alpine") == "redis"
    assert _extract_base_repo("ubuntu") == "ubuntu"


def test_create_k8s_clients() -> None:
    # In-cluster succeeds
    with patch("kubernetes.config.load_incluster_config") as mock_incluster:
        apps, core = create_k8s_clients("understudy-prod", "ust-prod")
        assert mock_incluster.called
        assert isinstance(apps, client.AppsV1Api)
        assert isinstance(core, client.CoreV1Api)
        assert (
            apps.api_client.default_headers.get("Impersonate-User")
            == "system:serviceaccount:ust-prod:understudy-prod"
        )

    # In-cluster fails with ConfigException, fallback to kubeconfig
    with (
        patch(
            "kubernetes.config.load_incluster_config",
            side_effect=config.ConfigException("off-cluster"),
        ),
        patch("kubernetes.config.load_kube_config") as mock_kubeconfig,
    ):
        apps, core = create_k8s_clients()
        assert mock_kubeconfig.called
        assert "Impersonate-User" not in apps.api_client.default_headers


def test_lazy_api_client_instantiation() -> None:
    clock = FrozenClock(FIXED_TIME)
    applier = K8sPlanApplier(clock=clock)

    with (
        patch("kubernetes.config.load_incluster_config"),
        patch("understudy.actuator.apply.create_k8s_clients") as mock_create,
    ):
        fake_apps = MagicMock(spec=client.AppsV1Api)
        fake_core = MagicMock(spec=client.CoreV1Api)
        mock_create.return_value = (fake_apps, fake_core)

        apps = applier._get_apps_api("ust-prod", "understudy-prod")
        core = applier._get_core_api("ust-prod", "understudy-prod")
        assert apps is fake_apps
        assert core is fake_core


# ---------------------------------------------------------------------------
# ROLLBACK_DEPLOY Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_deploy_direct_image_success() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(image="localhost:5001/data-service:regression")
    apps_api.read_namespaced_deployment.return_value = dep

    applier = K8sPlanApplier(apps_api=apps_api)
    plan = RemediationPlan(
        plan_id="plan_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload="data-service",
            target_commit="localhost:5001/data-service:good",
        ),
        rationale="Roll back to known good image",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    assert apps_api.read_namespaced_deployment.called
    assert apps_api.patch_namespaced_deployment.called

    call_kwargs = apps_api.patch_namespaced_deployment.call_args.kwargs
    assert call_kwargs["name"] == "data-service"
    assert call_kwargs["namespace"] == "ust-prod"
    patch_body = call_kwargs["body"]
    assert (
        patch_body["spec"]["template"]["spec"]["containers"][0]["image"]
        == "localhost:5001/data-service:good"
    )
    assert patch_body["metadata"]["annotations"][ANNOTATION_PLAN_ID] == "plan_1"


@pytest.mark.asyncio
async def test_rollback_deploy_idempotent() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(image="localhost:5001/data-service:good")
    apps_api.read_namespaced_deployment.return_value = dep

    applier = K8sPlanApplier(apps_api=apps_api)
    plan = RemediationPlan(
        plan_id="plan_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload="data-service",
            target_commit="localhost:5001/data-service:good",
        ),
        rationale="Already rolled back",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    assert apps_api.read_namespaced_deployment.called
    assert not apps_api.patch_namespaced_deployment.called


@pytest.mark.asyncio
async def test_rollback_deploy_with_deploy_history() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(image="localhost:5001/data-service:regression")
    apps_api.read_namespaced_deployment.return_value = dep

    deploy_history = FakeDeployHistory(
        deploys=[
            DeployRef(
                commit_sha="c0ffee1234567890abcdef",
                image_digests={"data-service": "sha256:abcd000011112222"},
                deployed_at=FIXED_TIME,
                pr_number=1,
                contains_migration=False,
            )
        ]
    )

    applier = K8sPlanApplier(apps_api=apps_api, deploy_history=deploy_history)
    plan = RemediationPlan(
        plan_id="plan_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload="data-service",
            target_commit="c0ffee1234",
        ),
        rationale="Rollback via git commit",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    patch_body = apps_api.patch_namespaced_deployment.call_args.kwargs["body"]
    assert (
        patch_body["spec"]["template"]["spec"]["containers"][0]["image"]
        == "localhost:5001/data-service@sha256:abcd000011112222"
    )


@pytest.mark.asyncio
async def test_rollback_deploy_with_deploy_history_full_image_spec() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(image="localhost:5001/data-service:regression")
    apps_api.read_namespaced_deployment.return_value = dep

    deploy_history = FakeDeployHistory(
        deploys=[
            DeployRef(
                commit_sha="c0ffee1234567890abcdef",
                image_digests={"data-service": "localhost:5001/data-service:good"},
                deployed_at=FIXED_TIME,
                pr_number=1,
                contains_migration=False,
            )
        ]
    )

    applier = K8sPlanApplier(apps_api=apps_api, deploy_history=deploy_history)
    plan = RemediationPlan(
        plan_id="plan_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload="data-service",
            target_commit="c0ffee1234",
        ),
        rationale="Rollback via git commit with full image",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    patch_body = apps_api.patch_namespaced_deployment.call_args.kwargs["body"]
    assert (
        patch_body["spec"]["template"]["spec"]["containers"][0]["image"]
        == "localhost:5001/data-service:good"
    )


@pytest.mark.asyncio
async def test_rollback_deploy_raw_sha256_and_hex() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(image="localhost:5001/data-service:regression")
    apps_api.read_namespaced_deployment.return_value = dep

    applier = K8sPlanApplier(apps_api=apps_api)

    # Starts with sha256:
    plan1 = RemediationPlan(
        plan_id="plan_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload="data-service",
            target_commit="sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
        ),
        rationale="Rollback to digest prefix",
        origin="planner",
    )
    expected_image = (
        "localhost:5001/data-service@"
        "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff"
    )
    assert await applier.apply(plan1, "ust-prod") is True
    patch_body = apps_api.patch_namespaced_deployment.call_args.kwargs["body"]
    assert patch_body["spec"]["template"]["spec"]["containers"][0]["image"] == expected_image

    # 64 hex characters
    plan2 = RemediationPlan(
        plan_id="plan_2",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload="data-service",
            target_commit="1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
        ),
        rationale="Rollback to 64-char hex",
        origin="planner",
    )
    assert await applier.apply(plan2, "ust-prod") is True
    patch_body = apps_api.patch_namespaced_deployment.call_args.kwargs["body"]
    assert patch_body["spec"]["template"]["spec"]["containers"][0]["image"] == expected_image


@pytest.mark.asyncio
async def test_rollback_deploy_deploy_history_error_fallback() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(image="localhost:5001/data-service:regression")
    apps_api.read_namespaced_deployment.return_value = dep

    deploy_history = FakeDeployHistory(raise_error=True)
    applier = K8sPlanApplier(apps_api=apps_api, deploy_history=deploy_history)

    # When target_commit is a commit SHA and deploy_history raises, ActuationError is raised
    plan = RemediationPlan(
        plan_id="plan_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload="data-service",
            target_commit="c0ffee123456",
        ),
        rationale="Fallback test",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="Cannot resolve commit"):
        await applier.apply(plan, "ust-prod")


@pytest.mark.asyncio
async def test_rollback_deploy_deploy_history_workload_not_in_digests() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(image="localhost:5001/data-service:regression")
    apps_api.read_namespaced_deployment.return_value = dep

    deploy_history = FakeDeployHistory(
        deploys=[
            DeployRef(
                commit_sha="c0ffee1234567890abcdef",
                image_digests={"other-service": "sha256:abcd000011112222"},
                deployed_at=FIXED_TIME,
                pr_number=1,
                contains_migration=False,
            )
        ]
    )
    applier = K8sPlanApplier(apps_api=apps_api, deploy_history=deploy_history)

    plan = RemediationPlan(
        plan_id="plan_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload="data-service",
            target_commit="c0ffee123456",
        ),
        rationale="Workload missing from deploy digests",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="Cannot resolve commit"):
        await applier.apply(plan, "ust-prod")


@pytest.mark.asyncio
async def test_rollback_deploy_errors() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    applier = K8sPlanApplier(apps_api=apps_api)

    # Missing workload
    p_no_wl = RemediationPlan(
        plan_id="p1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="", target_commit="sha256:123"),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="workload name is required"):
        await applier.apply(p_no_wl, "ust-prod")

    # Missing target_commit
    p_no_commit = RemediationPlan(
        plan_id="p2",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit=None),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="target_commit is required"):
        await applier.apply(p_no_commit, "ust-prod")

    # Unresolvable commit SHA
    p_unresolvable = RemediationPlan(
        plan_id="p3",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit="unknown_commit"),
        rationale="r",
        origin="planner",
    )
    dep = _make_deployment(image="localhost:5001/data-service:regression")
    apps_api.read_namespaced_deployment.return_value = dep
    with pytest.raises(ActuationError, match="Cannot resolve commit"):
        await applier.apply(p_unresolvable, "ust-prod")

    # Read Deployment ApiException
    apps_api.read_namespaced_deployment.side_effect = ApiException(status=404, reason="Not Found")
    p_valid = RemediationPlan(
        plan_id="p4",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit="repo:tag"),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="Failed to read Deployment"):
        await applier.apply(p_valid, "ust-prod")

    # Empty containers
    apps_api.read_namespaced_deployment.side_effect = None
    apps_api.read_namespaced_deployment.return_value = _make_deployment(empty_containers=True)
    with pytest.raises(ActuationError, match="has no containers"):
        await applier.apply(p_valid, "ust-prod")

    # Patch ApiException
    apps_api.read_namespaced_deployment.return_value = _make_deployment(image="old:image")
    apps_api.patch_namespaced_deployment.side_effect = ApiException(
        status=500, reason="Internal Error"
    )
    with pytest.raises(ActuationError, match="Failed to patch Deployment"):
        await applier.apply(p_valid, "ust-prod")


# ---------------------------------------------------------------------------
# RESTART_WORKLOAD Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_workload_success() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment()
    apps_api.read_namespaced_deployment.return_value = dep

    clock = FrozenClock(FIXED_TIME)
    applier = K8sPlanApplier(apps_api=apps_api, clock=clock)
    plan = RemediationPlan(
        plan_id="restart_plan_1",
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload="data-service"),
        rationale="Restart workload",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    assert apps_api.patch_namespaced_deployment.called
    patch_body = apps_api.patch_namespaced_deployment.call_args.kwargs["body"]
    assert (
        patch_body["spec"]["template"]["metadata"]["annotations"][ANNOTATION_RESTART_TIMESTAMP]
        == FIXED_TIME.isoformat()
    )
    assert patch_body["metadata"]["annotations"][ANNOTATION_PLAN_ID] == "restart_plan_1"


@pytest.mark.asyncio
async def test_restart_workload_idempotent() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(annotations={ANNOTATION_PLAN_ID: "restart_plan_1"})
    apps_api.read_namespaced_deployment.return_value = dep

    applier = K8sPlanApplier(apps_api=apps_api)
    plan = RemediationPlan(
        plan_id="restart_plan_1",
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload="data-service"),
        rationale="Restart workload",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    assert not apps_api.patch_namespaced_deployment.called


@pytest.mark.asyncio
async def test_restart_workload_errors() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    applier = K8sPlanApplier(apps_api=apps_api)

    p_no_wl = RemediationPlan(
        plan_id="p1",
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload=""),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="workload name is required"):
        await applier.apply(p_no_wl, "ust-prod")

    apps_api.read_namespaced_deployment.side_effect = ApiException(status=404, reason="Not Found")
    p_valid = RemediationPlan(
        plan_id="p2",
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload="data-service"),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="Failed to read Deployment"):
        await applier.apply(p_valid, "ust-prod")

    apps_api.read_namespaced_deployment.side_effect = None
    apps_api.read_namespaced_deployment.return_value = _make_deployment()
    apps_api.patch_namespaced_deployment.side_effect = ApiException(
        status=500, reason="Server Error"
    )
    with pytest.raises(ActuationError, match="Failed to restart Deployment"):
        await applier.apply(p_valid, "ust-prod")


# ---------------------------------------------------------------------------
# SCALE_WORKLOAD Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_workload_success() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(replicas=2)
    apps_api.read_namespaced_deployment.return_value = dep

    applier = K8sPlanApplier(apps_api=apps_api)
    plan = RemediationPlan(
        plan_id="scale_plan_1",
        candidate_index=0,
        action=ActionType.SCALE_WORKLOAD,
        params=ActionParams(workload="data-service", replica_delta=2),
        rationale="Scale up 2 replicas",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    assert apps_api.patch_namespaced_deployment.called
    patch_body = apps_api.patch_namespaced_deployment.call_args.kwargs["body"]
    assert patch_body["spec"]["replicas"] == 4
    assert patch_body["metadata"]["annotations"][ANNOTATION_PLAN_ID] == "scale_plan_1"
    assert patch_body["metadata"]["annotations"][ANNOTATION_REPLICA_DELTA] == "2"


@pytest.mark.asyncio
async def test_scale_workload_idempotent() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(
        replicas=4,
        annotations={ANNOTATION_PLAN_ID: "scale_plan_1"},
    )
    apps_api.read_namespaced_deployment.return_value = dep

    applier = K8sPlanApplier(apps_api=apps_api)
    plan = RemediationPlan(
        plan_id="scale_plan_1",
        candidate_index=0,
        action=ActionType.SCALE_WORKLOAD,
        params=ActionParams(workload="data-service", replica_delta=2),
        rationale="Scale up 2 replicas",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    assert not apps_api.patch_namespaced_deployment.called


@pytest.mark.asyncio
async def test_scale_workload_errors() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    applier = K8sPlanApplier(apps_api=apps_api)

    # Missing workload
    p_no_wl = RemediationPlan(
        plan_id="p1",
        candidate_index=0,
        action=ActionType.SCALE_WORKLOAD,
        params=ActionParams(workload="", replica_delta=1),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="workload name is required"):
        await applier.apply(p_no_wl, "ust-prod")

    # Missing replica_delta
    p_no_delta = RemediationPlan(
        plan_id="p2",
        candidate_index=0,
        action=ActionType.SCALE_WORKLOAD,
        params=ActionParams(workload="data-service", replica_delta=None),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="replica_delta is required"):
        await applier.apply(p_no_delta, "ust-prod")

    # Read ApiException
    apps_api.read_namespaced_deployment.side_effect = ApiException(status=404, reason="Not Found")
    p_valid = RemediationPlan(
        plan_id="p3",
        candidate_index=0,
        action=ActionType.SCALE_WORKLOAD,
        params=ActionParams(workload="data-service", replica_delta=1),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="Failed to read Deployment"):
        await applier.apply(p_valid, "ust-prod")

    # Scale below 0
    apps_api.read_namespaced_deployment.side_effect = None
    apps_api.read_namespaced_deployment.return_value = _make_deployment(replicas=1)
    p_below_zero = RemediationPlan(
        plan_id="p4",
        candidate_index=0,
        action=ActionType.SCALE_WORKLOAD,
        params=ActionParams(workload="data-service", replica_delta=-2),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="below 0 replicas"):
        await applier.apply(p_below_zero, "ust-prod")

    # Patch ApiException
    apps_api.patch_namespaced_deployment.side_effect = ApiException(status=500, reason="Error")
    with pytest.raises(ActuationError, match="Failed to scale Deployment"):
        await applier.apply(p_valid, "ust-prod")


# ---------------------------------------------------------------------------
# DISABLE_FLAG Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_flag_success() -> None:
    core_api = MagicMock(spec=client.CoreV1Api)
    cm = _make_config_map("feature-flags", data={"enable_recommendations": "true"})
    core_api.read_namespaced_config_map.return_value = cm

    applier = K8sPlanApplier(core_api=core_api)
    plan = RemediationPlan(
        plan_id="flag_plan_1",
        candidate_index=0,
        action=ActionType.DISABLE_FLAG,
        params=ActionParams(workload="data-service", flag_name="enable_recommendations"),
        rationale="Disable recommendations flag",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    assert core_api.patch_namespaced_config_map.called
    patch_body = core_api.patch_namespaced_config_map.call_args.kwargs["body"]
    assert patch_body["data"]["enable_recommendations"] == "false"
    assert patch_body["metadata"]["annotations"][ANNOTATION_PLAN_ID] == "flag_plan_1"


@pytest.mark.asyncio
async def test_disable_flag_re_enable() -> None:
    core_api = MagicMock(spec=client.CoreV1Api)
    cm = _make_config_map("feature-flags", data={"enable_recommendations": "false"})
    core_api.read_namespaced_config_map.return_value = cm

    applier = K8sPlanApplier(core_api=core_api)
    plan = RemediationPlan(
        plan_id="flag_plan_inv",
        candidate_index=0,
        action=ActionType.DISABLE_FLAG,
        params=ActionParams(workload="data-service", flag_name="enable_recommendations"),
        rationale="Re-enable recommendations flag",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    patch_body = core_api.patch_namespaced_config_map.call_args.kwargs["body"]
    assert patch_body["data"]["enable_recommendations"] == "true"


@pytest.mark.asyncio
async def test_disable_flag_idempotent() -> None:
    core_api = MagicMock(spec=client.CoreV1Api)
    cm = _make_config_map("feature-flags", data={"enable_recommendations": "false"})
    core_api.read_namespaced_config_map.return_value = cm

    applier = K8sPlanApplier(core_api=core_api)
    plan = RemediationPlan(
        plan_id="flag_plan_1",
        candidate_index=0,
        action=ActionType.DISABLE_FLAG,
        params=ActionParams(workload="data-service", flag_name="enable_recommendations"),
        rationale="Disable recommendations flag",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    assert not core_api.patch_namespaced_config_map.called


@pytest.mark.asyncio
async def test_disable_flag_explicit_target_resource() -> None:
    core_api = MagicMock(spec=client.CoreV1Api)
    cm = _make_config_map("custom-flags", data={"flag_a": "true"})
    core_api.read_namespaced_config_map.return_value = cm

    applier = K8sPlanApplier(core_api=core_api)
    plan = RemediationPlan(
        plan_id="flag_plan_custom",
        candidate_index=0,
        action=ActionType.DISABLE_FLAG,
        params=ActionParams(workload="data-service", flag_name="flag_a"),
        target_resources=[ResourceRef(namespace="ust-prod", kind="ConfigMap", name="custom-flags")],
        rationale="Disable custom flag",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    call_kwargs = core_api.patch_namespaced_config_map.call_args.kwargs
    assert call_kwargs["name"] == "custom-flags"


@pytest.mark.asyncio
async def test_disable_flag_errors() -> None:
    core_api = MagicMock(spec=client.CoreV1Api)
    applier = K8sPlanApplier(core_api=core_api)

    # Missing flag_name
    p_no_flag = RemediationPlan(
        plan_id="p1",
        candidate_index=0,
        action=ActionType.DISABLE_FLAG,
        params=ActionParams(workload="data-service", flag_name=None),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="flag_name is required"):
        await applier.apply(p_no_flag, "ust-prod")

    # Read ApiException
    core_api.read_namespaced_config_map.side_effect = ApiException(status=404, reason="Not Found")
    p_valid = RemediationPlan(
        plan_id="p2",
        candidate_index=0,
        action=ActionType.DISABLE_FLAG,
        params=ActionParams(workload="data-service", flag_name="enable_recommendations"),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="Failed to read ConfigMap"):
        await applier.apply(p_valid, "ust-prod")

    # Patch ApiException
    core_api.read_namespaced_config_map.side_effect = None
    core_api.read_namespaced_config_map.return_value = _make_config_map(
        "feature-flags", data={"enable_recommendations": "true"}
    )
    core_api.patch_namespaced_config_map.side_effect = ApiException(status=500, reason="Error")
    with pytest.raises(ActuationError, match="Failed to patch ConfigMap"):
        await applier.apply(p_valid, "ust-prod")


# ---------------------------------------------------------------------------
# REVERT_CONFIG Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revert_config_success() -> None:
    core_api = MagicMock(spec=client.CoreV1Api)
    cm = _make_config_map("app-config", data={"LOG_LEVEL": "DEBUG"})
    core_api.read_namespaced_config_map.return_value = cm

    applier = K8sPlanApplier(core_api=core_api)
    plan = RemediationPlan(
        plan_id="config_plan_1",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="app-config", config_key="LOG_LEVEL", config_value="INFO"),
        rationale="Revert log level to INFO",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    assert core_api.patch_namespaced_config_map.called
    patch_body = core_api.patch_namespaced_config_map.call_args.kwargs["body"]
    assert patch_body["data"]["LOG_LEVEL"] == "INFO"
    assert patch_body["metadata"]["annotations"][ANNOTATION_PLAN_ID] == "config_plan_1"


@pytest.mark.asyncio
async def test_revert_config_idempotent() -> None:
    core_api = MagicMock(spec=client.CoreV1Api)
    cm = _make_config_map("app-config", data={"LOG_LEVEL": "INFO"})
    core_api.read_namespaced_config_map.return_value = cm

    applier = K8sPlanApplier(core_api=core_api)
    plan = RemediationPlan(
        plan_id="config_plan_1",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="app-config", config_key="LOG_LEVEL", config_value="INFO"),
        rationale="Revert log level to INFO",
        origin="planner",
    )

    success = await applier.apply(plan, "ust-prod")
    assert success is True
    assert not core_api.patch_namespaced_config_map.called


@pytest.mark.asyncio
async def test_revert_config_errors() -> None:
    core_api = MagicMock(spec=client.CoreV1Api)
    applier = K8sPlanApplier(core_api=core_api)

    # Missing config_key
    p_no_key = RemediationPlan(
        plan_id="p1",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="app-config", config_key="", config_value="val"),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="config_key is required"):
        await applier.apply(p_no_key, "ust-prod")

    # Missing config_value
    p_no_val = RemediationPlan(
        plan_id="p2",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="app-config", config_key="LOG_LEVEL", config_value=None),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="config_value is required"):
        await applier.apply(p_no_val, "ust-prod")

    # Read ApiException
    core_api.read_namespaced_config_map.side_effect = ApiException(status=404, reason="Not Found")
    p_valid = RemediationPlan(
        plan_id="p3",
        candidate_index=0,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="app-config", config_key="LOG_LEVEL", config_value="INFO"),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="Failed to read ConfigMap"):
        await applier.apply(p_valid, "ust-prod")

    # Patch ApiException
    core_api.read_namespaced_config_map.side_effect = None
    core_api.read_namespaced_config_map.return_value = _make_config_map(
        "app-config", data={"LOG_LEVEL": "DEBUG"}
    )
    core_api.patch_namespaced_config_map.side_effect = ApiException(status=500, reason="Error")
    with pytest.raises(ActuationError, match="Failed to patch ConfigMap"):
        await applier.apply(p_valid, "ust-prod")


# ---------------------------------------------------------------------------
# NO_ACTION, Revert, and Standalone Function Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_action_applied() -> None:
    applier = K8sPlanApplier()
    plan = RemediationPlan(
        plan_id="plan_none",
        candidate_index=3,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload=""),
        rationale="Do nothing",
        origin="planner",
    )
    assert await applier.apply(plan, "ust-prod") is True


@pytest.mark.asyncio
async def test_revert_plan_success_and_errors() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(image="localhost:5001/data-service:regression")
    apps_api.read_namespaced_deployment.return_value = dep

    applier = K8sPlanApplier(apps_api=apps_api)

    inverse_plan = RemediationPlan(
        plan_id="plan_1_inv",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload="data-service", target_commit="localhost:5001/data-service:good"
        ),
        rationale="Inverse rollback",
        origin="planner",
    )
    plan = RemediationPlan(
        plan_id="plan_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload="data-service", target_commit="localhost:5001/data-service:regression"
        ),
        inverse=inverse_plan,
        rationale="Original plan",
        origin="planner",
    )

    # Success
    assert await applier.revert(plan, "ust-prod") is True
    assert apps_api.patch_namespaced_deployment.called

    # Revert NO_ACTION raises
    no_action_plan = RemediationPlan(
        plan_id="p_none",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload=""),
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="no_action is explicitly non-reversible"):
        await applier.revert(no_action_plan, "ust-prod")

    # Revert plan missing inverse raises
    plan_no_inv = RemediationPlan(
        plan_id="p_no_inv",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit="tag"),
        inverse=None,
        rationale="r",
        origin="planner",
    )
    with pytest.raises(ActuationError, match="declares no inverse"):
        await applier.revert(plan_no_inv, "ust-prod")


@pytest.mark.asyncio
async def test_standalone_apply_and_revert_plan() -> None:
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(image="localhost:5001/data-service:regression")
    apps_api.read_namespaced_deployment.return_value = dep

    inverse_plan = RemediationPlan(
        plan_id="inv_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload="data-service", target_commit="localhost:5001/data-service:good"
        ),
        rationale="Inv",
        origin="planner",
    )
    plan = RemediationPlan(
        plan_id="p_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(
            workload="data-service", target_commit="localhost:5001/data-service:good"
        ),
        inverse=inverse_plan,
        rationale="Direct apply",
        origin="planner",
    )

    # apply_plan
    assert await apply_plan(plan, "ust-prod", apps_api=apps_api) is True

    # revert_plan
    assert await revert_plan(plan, "ust-prod", apps_api=apps_api) is True


@pytest.mark.asyncio
async def test_unsupported_action_type() -> None:
    applier = K8sPlanApplier()
    plan = RemediationPlan(
        plan_id="p_bad",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service"),
        rationale="r",
        origin="planner",
    )
    # Temporarily remove handler
    with (
        patch.dict(K8sPlanApplier._HANDLERS, {}, clear=True),
        pytest.raises(ActuationError, match="Unsupported action type"),
    ):
        await applier.apply(plan, "ust-prod")


def test_resolve_config_map_name_with_deployment_target() -> None:
    applier = K8sPlanApplier()
    plan = RemediationPlan(
        plan_id="p_cm",
        candidate_index=0,
        action=ActionType.DISABLE_FLAG,
        params=ActionParams(workload="data-service", flag_name="test_flag"),
        target_resources=[
            ResourceRef(namespace="ust-prod", kind="Deployment", name="data-service")
        ],
        rationale="r",
        origin="planner",
    )
    assert applier._resolve_config_map_name(plan, default_name="feature-flags") == "feature-flags"


@pytest.mark.asyncio
async def test_fake_actuator_package_coverage() -> None:
    from understudy.actuator.fakes import FakeActuator
    from understudy.contracts.enums import KernelVerdictType
    from understudy.contracts.kernel import KernelVerdict

    fake = FakeActuator()
    plan = RemediationPlan(
        plan_id="p0",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="data-service"),
        rationale="r",
        origin="planner",
    )
    assert await fake.apply(plan, "ust-twin-0") is True
    assert await fake.revert(plan, "ust-twin-0") is True

    verdict_pass = KernelVerdict(
        incident_id="inc1",
        plan_id="p0",
        verdict=KernelVerdictType.PASS,
        solver_ms=1.0,
        human_reason="ok",
    )
    assert await fake.apply_to_production(plan, verdict_pass) is True

    verdict_veto = KernelVerdict(
        incident_id="inc1",
        plan_id="p0",
        verdict=KernelVerdictType.VETO,
        solver_ms=1.0,
        human_reason="veto",
    )
    with pytest.raises(ActuationError, match="without PASS"):
        await fake.apply_to_production(plan, verdict_veto)
