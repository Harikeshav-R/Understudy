"""Integration tests for deploy/prod and deploy/system manifests."""

import shutil
import subprocess

import pytest


@pytest.mark.integration
def test_kubectl_dry_run_prod_manifests() -> None:
    """Verify that all deploy/prod Kubernetes manifests are accepted by kubectl dry-run."""
    if not shutil.which("kubectl"):
        pytest.skip("kubectl not installed in environment")

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
    if not shutil.which("kubectl"):
        pytest.skip("kubectl not installed in environment")

    res = subprocess.run(
        ["kubectl", "apply", "--dry-run=client", "-f", "deploy/system/"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"kubectl dry-run on deploy/system/ failed:\n{res.stderr}"
