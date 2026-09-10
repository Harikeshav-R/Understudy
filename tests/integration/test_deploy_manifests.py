"""Integration tests for deploy/prod and deploy/system manifests."""

import shutil
import subprocess

import pytest


def _cluster_available() -> bool:
    """Check if kubectl is installed and a cluster is reachable."""
    if not shutil.which("kubectl"):
        return False
    try:
        res = subprocess.run(
            ["kubectl", "cluster-info", "--request-timeout=2s"],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


@pytest.mark.integration
def test_kubectl_dry_run_prod_manifests() -> None:
    """Verify that all deploy/prod Kubernetes manifests are accepted by kubectl dry-run."""
    if not _cluster_available():
        pytest.skip("Kubernetes cluster is not reachable (requires running cluster / make up)")

    # Exclude non-k8s dependency declaration file dependencies.yaml
    prod_files = [
        f"deploy/prod/{name}"
        for name in [
            "namespace.yaml",
            "configmap.yaml",
            "auth-service.yaml",
            "data-service.yaml",
            "edge-gateway.yaml",
            "worker.yaml",
        ]
    ]
    args = ["kubectl", "apply", "--dry-run=client"]
    for f in prod_files:
        args.extend(["-f", f])

    res = subprocess.run(args, capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"kubectl dry-run on deploy/prod/ failed:\n{res.stderr}"


@pytest.mark.integration
def test_kubectl_dry_run_system_manifests() -> None:
    """Verify that all deploy/system manifests are accepted by kubectl dry-run."""
    if not _cluster_available():
        pytest.skip("Kubernetes cluster is not reachable (requires running cluster / make up)")

    res = subprocess.run(
        ["kubectl", "apply", "--dry-run=client", "-f", "deploy/system/"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"kubectl dry-run on deploy/system/ failed:\n{res.stderr}"
