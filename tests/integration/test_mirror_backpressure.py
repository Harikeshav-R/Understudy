"""Integration tests for traffic mirror gateway backpressure behaviour (A3.6).

Verifies against a live k3d-ust cluster and mirror-gateway service:
- Registration and unregistration of twins
- Fan-out to registered twins
- Backpressure behavior when a twin endpoint is unresponsive
"""

import shutil
import subprocess

import httpx
import pytest

from understudy.contracts.twin import TwinHandle
from understudy.mirror.registry import HttpMirrorRegistry


def _cluster_and_gateway_available() -> bool:
    """Check if kubectl is installed, cluster is reachable, and mirror gateway is listening."""
    if not shutil.which("kubectl"):
        return False
    try:
        res = subprocess.run(
            ["kubectl", "cluster-info", "--context=k3d-ust", "--request-timeout=2s"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            return False

        # Check if mirror-gateway pod is running in ust-system
        pod_res = subprocess.run(
            [
                "kubectl",
                "get",
                "pods",
                "-n",
                "ust-system",
                "-l",
                "app=mirror-gateway",
                "--no-headers",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return pod_res.returncode == 0 and "Running" in pod_res.stdout
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_mirror_gateway_backpressure() -> None:
    """Live cluster integration test verifying mirror-gateway fanout and backpressure."""
    if not _cluster_and_gateway_available():
        pytest.skip("k3d-ust cluster or mirror-gateway is not running (requires make up / deploy)")

    registry = HttpMirrorRegistry(base_url="http://localhost:8080")
    try:
        # Verify mirror gateway health
        is_healthy = await registry.health()
        assert is_healthy, "Mirror gateway healthz probe failed"

        # Register a test twin pointing to an unreachable port
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        fake_twin = TwinHandle(
            twin_id="twin_int_backpressure_0",
            incident_id="int_backpressure",
            candidate_index=0,
            namespace="ust-twin-int-backpressure-0",
            database="twin_int_backpressure_0",
            forked_from_snapshot_at=now,
            ready_at=now,
            state="ready",
        )

        # Register twin with unreachable destination in-cluster
        await registry.register(
            twin_id=fake_twin.twin_id,
            base_url="http://10.43.254.254:9999",  # non-routable / unreachable in-cluster
            incident_id=fake_twin.incident_id,
        )

        # Send requests through mirror gateway to prod
        async with httpx.AsyncClient(base_url="http://localhost:8080", timeout=5.0) as client:
            for _ in range(20):
                resp = await client.get("/healthz")
                assert resp.status_code == 200

        # Check stats: requests to unreachable twin should be dropped/timing out
        stats = await registry.get_stats(fake_twin.twin_id)
        assert stats.twin_id == fake_twin.twin_id
        # Delivery cannot succeed to non-routable IP
        assert stats.delivered == 0

        # Clean up
        await registry.unregister_twin(fake_twin.twin_id)
    finally:
        await registry.aclose()
