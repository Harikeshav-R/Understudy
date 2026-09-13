"""Live cluster integration test for fleet/controller.py (build-plan step A2.4).

Verifies against the live k3d-ust cluster:
1. Concurrent fork of N=3 twins:
   - Three namespaces ust-twin-<incident>-0..2 created and Ready within 120s.
   - Three twin databases cloned from snapshot_template.
   - Invariant K6 NetworkPolicy present and enforced in each twin namespace.
2. Twin item fidelity:
   - Twin item count equals production item count, read from ust_prod on prod-postgres.
3. Write isolation:
   - A write to a twin database is strictly isolated from production.
4. Egress containment (K6):
   - A twin pod cannot reach ust-prod, proven against an in-namespace positive control.
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


_CONNECT_PROBE = """
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    with socket.create_connection((host, port), timeout=3):
        print("OK")
except Exception as exc:
    print(f"BLOCKED:{type(exc).__name__}")
"""


def _connect_probe(namespace: str, workload: str, host: str, port: int) -> str:
    """Attempt a TCP connection from a pod, returning 'OK' or 'BLOCKED:<exception>'.

    Uses the stdlib rather than curl (absent from python:3.12-slim) and always exits 0, so a
    broken `kubectl exec` fails the assertion instead of masquerading as containment.
    """
    result = subprocess.run(
        [
            "kubectl",
            "-n",
            namespace,
            "exec",
            f"deploy/{workload}",
            "--context=k3d-ust",
            "--",
            "python",
            "-c",
            _CONNECT_PROBE,
            host,
            str(port),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"connection probe failed to run in {namespace}/{workload}: "
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    output = result.stdout.strip()
    assert output == "OK" or output.startswith("BLOCKED:"), (
        f"unexpected probe output from {namespace}/{workload}: {result.stdout!r}"
    )
    return output


async def _prod_item_count(db_executor: KubectlDatabaseExecutor) -> int:
    """Count rows in production's items table, read from prod-postgres in ust-prod."""
    code, out, err = await db_executor.run_pipeline(
        "psql -v ON_ERROR_STOP=1 -h prod-postgres.ust-prod -U postgres -d ust_prod "
        "-tAc 'SELECT count(*) FROM items;'"
    )
    assert code == 0, f"failed to read production item count: {err or out}"
    return int(out.strip())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fleet_controller_live_fork_and_teardown() -> None:
    """Verify concurrent fork, K6 policy, readiness, data isolation, and teardown."""
    if not _cluster_available():
        pytest.skip("k3d-ust cluster is not reachable (requires make up)")

    # Underscored, like the real new_incident_id(): the namespace renderer must sanitize it.
    incident_id = f"inc_ctrl_{uuid.uuid4().hex[:6]}"
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

        # Assert twin item count equals PRODUCTION item count. The baseline must come from
        # ust_prod itself: reading snapshot_template instead would make this tautological with
        # respect to the refresher and could not detect an empty or stale template.
        twin_0_db = sanitize_database_name(incident_id, 0)
        twin_count = await cloner.get_item_count(twin_0_db)
        prod_count = await _prod_item_count(db_executor)
        assert twin_count == prod_count

        # 3. Checkpoint A2 assertion: A write to a twin is invisible in production
        write_sql = (
            "INSERT INTO items (name, description) VALUES ('twin-live-probe', 'live-write-check');"
        )
        write_code, _, _ = await db_executor.run_sql([write_sql], database=twin_0_db)
        assert write_code == 0
        assert await cloner.get_item_count(twin_0_db) == prod_count + 1

        # Production must not see the twin's write
        assert await _prod_item_count(db_executor) == prod_count

        # And the template the twins were cloned from is untouched as well
        _, template_count_out, _ = await db_executor.run_sql(
            ["SELECT count(*) FROM items;"], database="snapshot_template"
        )
        assert int(template_count_out.strip()) == prod_count

        # 4. Checkpoint A2 assertion: Twin cannot reach ust-prod (NetworkPolicy egress denial).
        # Two positive controls guard against a vacuous pass, because "the connection failed"
        # is also what a missing binary, a wrong pod, or a target nobody can reach looks like:
        #   a) the same probe must succeed inside the twin namespace (the probe works), and
        #   b) the same target must be reachable from a ust-prod pod (the target is live).
        # Both targets are ClusterIP services listening on 8000; note prod's edge-gateway
        # Service publishes 8080, so probing it on 8000 would fail for everyone.
        twin_ns = twins[0].namespace
        assert _connect_probe(twin_ns, "edge-gateway", "auth-service", 8000) == "OK", (
            "in-namespace egress is permitted by the twin NetworkPolicy; if this fails the "
            "probe itself cannot be trusted to detect containment"
        )
        assert _connect_probe("ust-prod", "worker", "auth-service.ust-prod", 8000) == "OK", (
            "auth-service.ust-prod must be reachable from inside ust-prod, otherwise a "
            "blocked twin proves nothing about the NetworkPolicy"
        )

        egress_check = _connect_probe(twin_ns, "edge-gateway", "auth-service.ust-prod", 8000)
        assert egress_check.startswith("BLOCKED:"), (
            f"K6 Invariant violated: twin pod reached ust-prod! Output: {egress_check!r}"
        )

        # The path that matters most: a twin must never reach production's database.
        db_egress = _connect_probe(twin_ns, "edge-gateway", "prod-postgres.ust-prod", 5432)
        assert db_egress.startswith("BLOCKED:"), (
            f"K6 Invariant violated: twin pod reached prod-postgres! Output: {db_egress!r}"
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
