"""Live cluster integration test for fleet/controller.py (build-plan step A2.4).

Verifies against the live k3d-ust cluster:
1. Concurrent fork of N=3 twins:
   - Three namespaces ust-twin-<incident>-0..2 created and Ready within 120s.
   - Three twin databases cloned from snapshot_template.
   - Invariant K6 NetworkPolicy present and enforced in each twin namespace.
2. Twin item fidelity:
   - Twin item count equals production item count.
3. Write isolation:
   - A write to a twin database is strictly isolated from production.
4. Egress containment (K6):
   - A twin pod cannot reach ust-prod.
5. Teardown:
   - teardown_all cleanly removes all twin namespaces and twin databases.
"""

import asyncio
import shutil
import subprocess
import uuid

import pytest

from understudy.fleet.controller import K8sFleetController
from understudy.fleet.database import KubectlDatabaseExecutor, PostgresDatabaseCloner
from understudy.fleet.k8s import get_k8s_core_client, get_k8s_networking_client
from understudy.fleet.render import sanitize_database_name


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
async def test_fleet_controller_live_fork_and_teardown() -> None:
    """Verify concurrent fork, K6 policy, readiness, data isolation, and teardown."""
    if not _cluster_available():
        pytest.skip("k3d-ust cluster is not reachable (requires make up)")

    incident_id = f"inc-ctrl-{uuid.uuid4().hex[:6]}"
    controller = K8sFleetController(
        poll_interval_seconds=1.0,
        readiness_timeout_seconds=120.0,
    )

    core_api = get_k8s_core_client(context="k3d-ust")
    networking_api = get_k8s_networking_client(context="k3d-ust")
    db_executor = KubectlDatabaseExecutor(
        namespace="ust-system",
        deployment="twin-postgres",
        context="k3d-ust",
    )
    cloner = PostgresDatabaseCloner(executor=db_executor)

    try:
        # 1. Concurrently fork 3 isolated twins
        twins = await controller.fork(incident_id=incident_id, n=3)
        assert len(twins) == 3

        # 2. Checkpoint A2 assertion: Three twin namespaces Ready within 120s;
        # three twin databases exist
        for idx, twin in enumerate(twins):
            assert twin.incident_id == incident_id
            assert twin.candidate_index == idx
            assert twin.state == "ready"
            assert twin.ready_at is not None

            # Assert namespace exists in cluster
            ns = await asyncio.to_thread(core_api.read_namespace, name=twin.namespace)
            assert ns.metadata.name == twin.namespace

            # Assert K6 NetworkPolicy exists in cluster namespace and enforces Egress
            netpol = await asyncio.to_thread(
                networking_api.read_namespaced_network_policy,
                name="twin-egress-containment",
                namespace=twin.namespace,
            )
            assert netpol.metadata.name == "twin-egress-containment"
            assert "Egress" in (netpol.spec.policy_types or [])

        # Assert 3 twin databases exist on twin-postgres
        twin_dbs = await cloner.list_twin_databases(incident_id)
        assert len(twin_dbs) == 3

        # Assert twin item count equals prod item count
        twin_0_db = sanitize_database_name(incident_id, 0)
        twin_count = await cloner.get_item_count(twin_0_db)
        prod_count_code, prod_count_out, _ = await db_executor.run_sql(
            ["SELECT count(*) FROM items;"], database="snapshot_template"
        )
        assert prod_count_code == 0
        prod_count = int(prod_count_out.strip())
        assert twin_count == prod_count

        # 3. Checkpoint A2 assertion: A write to a twin is invisible in production
        write_sql = (
            "INSERT INTO items (name, description) VALUES ('twin-live-probe', 'live-write-check');"
        )
        write_code, _, _ = await db_executor.run_sql([write_sql], database=twin_0_db)
        assert write_code == 0
        assert await cloner.get_item_count(twin_0_db) == prod_count + 1

        # Check production template count remains unchanged
        _, prod_count_after, _ = await db_executor.run_sql(
            ["SELECT count(*) FROM items;"], database="snapshot_template"
        )
        assert int(prod_count_after.strip()) == prod_count

        # 4. Checkpoint A2 assertion: Twin cannot reach ust-prod (NetworkPolicy egress denial)
        egress_check = subprocess.run(
            [
                "kubectl",
                "-n",
                f"ust-twin-{incident_id}-0",
                "exec",
                "deploy/edge-gateway",
                "--context=k3d-ust",
                "--",
                "curl",
                "-s",
                "--max-time",
                "3",
                "http://edge-gateway.ust-prod:8000/healthz",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        # Egress to ust-prod must fail (timed out / connection refused)
        assert egress_check.returncode != 0, (
            f"K6 Invariant violated: twin pod reached ust-prod! Output: {egress_check.stdout}"
        )

    finally:
        # 5. Checkpoint A2 assertion: Teardown removes all namespaces and all twin databases
        await controller.teardown_all(incident_id)

        # Confirm all namespaces deleted or terminating
        ns_list = await asyncio.to_thread(
            core_api.list_namespace,
            label_selector=f"understudy.dev/incident={incident_id}",
        )
        active_ns = [
            n.metadata.name
            for n in (ns_list.items or [])
            if n.status and n.status.phase != "Terminating"
        ]
        assert active_ns == []

        # Confirm twin databases dropped
        remaining_dbs = await cloner.list_twin_databases(incident_id)
        assert remaining_dbs == []

        # 6. Verify fleet GC runs cleanly on live cluster
        gc_res = await controller.teardown_manager.gc(older_than_seconds=7200)
        assert isinstance(gc_res.scanned_namespaces, int)
