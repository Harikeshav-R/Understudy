"""Unit tests for cluster fault injection utility."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from understudy.actuator.injection import _derive_injected_image, inject_scenario_fault
from understudy.common.clock import FrozenClock
from understudy.common.errors import ActuationError


def test_derive_injected_image() -> None:
    """Verify image tag replacement and derivation."""
    assert (
        _derive_injected_image("localhost:5001/data-service:good", "regression")
        == "localhost:5001/data-service:regression"
    )
    assert (
        _derive_injected_image("localhost:5001/data-service@sha256:abc123", "regression")
        == "localhost:5001/data-service:regression"
    )
    assert (
        _derive_injected_image("localhost:5001/data-service", "regression")
        == "localhost:5001/data-service:regression"
    )
    assert _derive_injected_image("data-service", "regression") == "data-service:regression"


def _make_deployment(
    name: str = "data-service",
    image: str = "localhost:5001/data-service:good",
    empty_containers: bool = False,
    available_replicas: int = 1,
    updated_replicas: int = 1,
    replicas: int = 1,
) -> client.V1Deployment:
    containers = [] if empty_containers else [client.V1Container(name=name, image=image)]
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, generation=1),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(containers=containers),
            ),
        ),
        status=client.V1DeploymentStatus(
            observed_generation=1,
            replicas=replicas,
            available_replicas=available_replicas,
            updated_replicas=updated_replicas,
        ),
    )


@pytest.mark.asyncio
async def test_inject_scenario_fault_success() -> None:
    """inject_scenario_fault successfully patches deployment image and waits for settle."""
    clock = FrozenClock(datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC))
    apps_api = MagicMock(spec=client.AppsV1Api)
    core_api = MagicMock(spec=client.CoreV1Api)

    dep = _make_deployment(name="data-service", image="localhost:5001/data-service:good")
    apps_api.read_namespaced_deployment.return_value = dep

    res = await inject_scenario_fault(
        target="data-service",
        image_tag="regression",
        namespace="ust-prod",
        settle_seconds=2.0,
        apps_api=apps_api,
        core_api=core_api,
        clock=clock,
        rollout_timeout=5.0,
    )

    assert res == "localhost:5001/data-service:regression"
    assert apps_api.patch_namespaced_deployment.called
    call_args = apps_api.patch_namespaced_deployment.call_args
    assert call_args.kwargs["name"] == "data-service"
    assert call_args.kwargs["namespace"] == "ust-prod"
    containers_patch = call_args.kwargs["body"]["spec"]["template"]["spec"]["containers"]
    assert containers_patch[0]["image"] == "localhost:5001/data-service:regression"
    assert containers_patch[0]["env"] == [{"name": "DATA_SERVICE_VARIANT", "value": "regression"}]


@pytest.mark.asyncio
async def test_inject_scenario_fault_non_data_service() -> None:
    """inject_scenario_fault patches non-data-service target without DATA_SERVICE_VARIANT."""
    clock = FrozenClock(datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC))
    apps_api = MagicMock(spec=client.AppsV1Api)
    core_api = MagicMock(spec=client.CoreV1Api)

    dep = _make_deployment(name="auth-service", image="localhost:5001/auth-service:v1")
    apps_api.read_namespaced_deployment.return_value = dep

    res = await inject_scenario_fault(
        target="auth-service",
        image_tag="fault",
        namespace="ust-prod",
        settle_seconds=0.0,
        apps_api=apps_api,
        core_api=core_api,
        clock=clock,
    )

    assert res == "localhost:5001/auth-service:fault"
    call_args = apps_api.patch_namespaced_deployment.call_args
    containers_patch = call_args.kwargs["body"]["spec"]["template"]["spec"]["containers"]
    assert "env" not in containers_patch[0]


@pytest.mark.asyncio
async def test_inject_scenario_fault_read_error() -> None:
    """ApiException during deployment read raises ActuationError."""
    apps_api = MagicMock(spec=client.AppsV1Api)
    apps_api.read_namespaced_deployment.side_effect = ApiException(status=404, reason="Not Found")

    with pytest.raises(ActuationError, match="Failed to read deployment"):
        await inject_scenario_fault(
            target="missing-svc",
            image_tag="regression",
            apps_api=apps_api,
        )


@pytest.mark.asyncio
async def test_inject_scenario_fault_empty_containers() -> None:
    """Deployment with no containers raises ActuationError."""
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment(empty_containers=True)
    apps_api.read_namespaced_deployment.return_value = dep

    with pytest.raises(ActuationError, match="has no containers defined"):
        await inject_scenario_fault(
            target="data-service",
            image_tag="regression",
            apps_api=apps_api,
        )


@pytest.mark.asyncio
async def test_inject_scenario_fault_patch_error() -> None:
    """ApiException during patch raises ActuationError."""
    apps_api = MagicMock(spec=client.AppsV1Api)
    dep = _make_deployment()
    apps_api.read_namespaced_deployment.return_value = dep
    apps_api.patch_namespaced_deployment.side_effect = ApiException(
        status=500, reason="Internal Error"
    )

    with pytest.raises(ActuationError, match="Failed to patch deployment"):
        await inject_scenario_fault(
            target="data-service",
            image_tag="regression",
            apps_api=apps_api,
        )


@pytest.mark.asyncio
async def test_inject_scenario_fault_rollout_timeout() -> None:
    """Rollout readiness timeout logs warning and proceeds."""
    clock = FrozenClock(datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC))
    apps_api = MagicMock(spec=client.AppsV1Api)

    dep_initial = _make_deployment()
    dep_not_ready = _make_deployment(available_replicas=0, updated_replicas=0)
    calls: list[Any] = [dep_initial, dep_not_ready, ApiException(status=500)]

    def _read_dep(*_: Any, **__: Any) -> Any:
        if calls:
            item = calls.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return dep_not_ready

    apps_api.read_namespaced_deployment.side_effect = _read_dep

    res = await inject_scenario_fault(
        target="data-service",
        image_tag="regression",
        apps_api=apps_api,
        core_api=MagicMock(),
        clock=clock,
        poll_interval=0.01,
        rollout_timeout=0.05,
        settle_seconds=0.01,
    )
    assert res == "localhost:5001/data-service:regression"
