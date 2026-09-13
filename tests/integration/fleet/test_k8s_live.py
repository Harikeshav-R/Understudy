"""Live cluster integration test for fleet/k8s.py.

Verifies reading workloads, resolving digests, and extracting config
from the live ust-prod namespace on k3d-ust.
"""

import shutil
import subprocess

import pytest

from understudy.fleet.k8s import read_prod_workloads


def _cluster_available() -> bool:
    """Check if kubectl is installed and k3d-ust cluster is reachable."""
    if not shutil.which("kubectl"):
        return False
    try:
        res = subprocess.run(
            ["kubectl", "cluster-info", "--context=k3d-ust", "--request-timeout=2s"],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_prod_workloads_live() -> None:
    """Verify reading workloads from live ust-prod namespace with pinned digests."""
    if not _cluster_available():
        pytest.skip("k3d-ust cluster is not reachable (requires make up)")

    # Read the 4 demo services (excluding database)
    snapshot = await read_prod_workloads(exclude_components={"database"})

    assert snapshot.namespace == "ust-prod"
    expected_services = {"edge-gateway", "auth-service", "data-service", "worker"}
    assert set(snapshot.workloads.keys()) == expected_services

    for svc_name in expected_services:
        wl = snapshot.workloads[svc_name]
        assert wl.name == svc_name
        assert wl.namespace == "ust-prod"
        assert len(wl.containers) == 1

        c = wl.containers[0]
        assert c.name == svc_name
        assert c.image_tag == f"localhost:5001/{svc_name}:good"

        # Assert pinned image has @sha256: and valid 64-hex digest
        assert c.pinned_image.startswith(f"localhost:5001/{svc_name}@sha256:")
        assert c.image_digest.startswith("sha256:")
        assert len(c.image_digest) == 71  # 'sha256:' (7 chars) + 64 hex chars

        # Assert resource limits conform to architecture §2.10 (150Mi limit)
        assert c.resources.limits.get("memory") == "150Mi"
        assert c.resources.requests.get("memory") == "64Mi"

        # Assert app-config ConfigMap is referenced
        assert "app-config" in wl.config_map_refs

    # Verify ConfigMaps live data captured
    assert "app-config" in snapshot.config_maps
    assert snapshot.config_maps["app-config"].get("ENVIRONMENT") == "production"
    assert snapshot.config_maps["app-config"].get("FAULT_INJECTION_SEED") == "1337"

    # Also verify full snapshot without component exclusions includes prod-postgres
    full_snapshot = await read_prod_workloads(exclude_components=None)
    assert len(full_snapshot.workloads) == 5
    assert "prod-postgres" in full_snapshot.workloads
    pg_wl = full_snapshot.workloads["prod-postgres"]
    assert pg_wl.component == "database"
    assert pg_wl.containers[0].resources.limits.get("memory") == "400Mi"
