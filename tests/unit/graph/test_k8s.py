"""Unit tests for Kubernetes cluster workload verification."""

from unittest.mock import Mock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from understudy.common.errors import GraphError
from understudy.graph.k8s import verify_cluster_workloads


def test_verify_cluster_workloads_success() -> None:
    """Verify that matching active Deployments passes validation."""
    mock_meta_a = Mock()
    mock_meta_a.name = "svc-a"
    mock_item_a = Mock(metadata=mock_meta_a)

    mock_meta_b = Mock()
    mock_meta_b.name = "svc-b"
    mock_item_b = Mock(metadata=mock_meta_b)
    mock_deployments = Mock(items=[mock_item_a, mock_item_b])

    with (
        patch("understudy.graph.k8s.config.load_kube_config") as mock_load,
        patch("understudy.graph.k8s.client.AppsV1Api") as mock_apps_cls,
    ):
        mock_apps = Mock()
        mock_apps.list_namespaced_deployment.return_value = mock_deployments
        mock_apps_cls.return_value = mock_apps

        assert verify_cluster_workloads({"svc-a", "svc-b"}, namespace="test-ns") is True
        mock_load.assert_called_once_with(context="k3d-ust")
        mock_apps.list_namespaced_deployment.assert_called_once_with(namespace="test-ns")


def test_verify_cluster_workloads_missing_service() -> None:
    """Declared services missing Deployments raises GraphError."""
    mock_meta_a = Mock()
    mock_meta_a.name = "svc-a"
    mock_item_a = Mock(metadata=mock_meta_a)
    mock_deployments = Mock(items=[mock_item_a])

    with (
        patch("understudy.graph.k8s.config.load_kube_config"),
        patch("understudy.graph.k8s.client.AppsV1Api") as mock_apps_cls,
    ):
        mock_apps = Mock()
        mock_apps.list_namespaced_deployment.return_value = mock_deployments
        mock_apps_cls.return_value = mock_apps

        with pytest.raises(GraphError, match="Declared services missing active Deployments"):
            verify_cluster_workloads({"svc-a", "missing-svc"}, namespace="test-ns")


def test_verify_cluster_workloads_kubeconfig_failure() -> None:
    """Failure to load kubeconfig raises GraphError."""
    with (
        patch(
            "understudy.graph.k8s.config.load_kube_config", side_effect=RuntimeError("no config")
        ),
        pytest.raises(GraphError, match="Failed to load kubeconfig"),
    ):
        verify_cluster_workloads({"svc-a"})


def test_verify_cluster_workloads_api_exception() -> None:
    """Kubernetes ApiException during deployment listing raises GraphError."""
    with (
        patch("understudy.graph.k8s.config.load_kube_config"),
        patch("understudy.graph.k8s.client.AppsV1Api") as mock_apps_cls,
    ):
        mock_apps = Mock()
        mock_apps.list_namespaced_deployment.side_effect = ApiException(
            status=403, reason="Forbidden"
        )
        mock_apps_cls.return_value = mock_apps

        with pytest.raises(GraphError, match="Failed to list deployments"):
            verify_cluster_workloads({"svc-a"})
