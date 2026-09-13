"""Live cluster integration test for fleet/database.py (build-plan step A2.3 / A2.3a).

Verifies against the live k3d-ust cluster:
1. Snapshot refresher: executes pg_dump from ust-prod/prod-postgres, restores into
   snapshot_staging on ust-system/twin-postgres, and swaps to snapshot_template by rename.
2. Cloner: executes CREATE DATABASE ... TEMPLATE snapshot_template for twin databases.
3. Checkpoint A2 verification:
   - Twin item count equals prod item count at fork time.
   - A write to a twin database is strictly isolated and invisible in production.
   - Teardown cleanly removes the twin databases.
"""

import shutil
import subprocess

import pytest

from understudy.fleet.database import (
    KubectlDatabaseExecutor,
    PostgresDatabaseCloner,
    PostgresSnapshotRefresher,
)


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
async def test_database_refresher_and_cloner_live() -> None:
    """Verify live snapshot refresh, template cloning, item fidelity, and write isolation."""
    if not _cluster_available():
        pytest.skip("k3d-ust cluster is not reachable (requires make up)")

    executor = KubectlDatabaseExecutor(
        namespace="ust-system",
        deployment="twin-postgres",
        context="k3d-ust",
    )

    # 1. Run live snapshot refresher
    refresher = PostgresSnapshotRefresher(
        executor=executor,
        source_host="prod-postgres.ust-prod",
        source_port=5432,
        source_user="postgres",
        source_db="ust_prod",
        target_template_db="snapshot_template",
        target_staging_db="snapshot_staging",
    )
    refreshed_at = await refresher.refresh_snapshot()
    assert refreshed_at is not None

    # Query prod item count directly via kubectl exec
    code, stdout, _ = await executor.run_sql(
        ["SELECT count(*) FROM items;"], database="snapshot_template"
    )
    assert code == 0
    prod_count = int(stdout.strip())
    assert prod_count > 0, "Production items table must not be empty"

    # 2. Clone three twin databases (Checkpoint A2 assertion: three twin databases exist)
    incident_id = "inc_live_test"
    cloner = PostgresDatabaseCloner(
        executor=executor,
        template_db="snapshot_template",
        refresher=refresher,
    )

    cloned_twins = []
    try:
        for idx in range(3):
            clone_res = await cloner.clone_twin_database(incident_id, idx)
            assert clone_res.database_name == f"twin_{incident_id}_{idx}"
            assert clone_res.candidate_index == idx
            assert clone_res.forked_from_snapshot_at == refreshed_at
            cloned_twins.append(clone_res.database_name)

            # Checkpoint A2 assertion: Twin item count equals prod item count at fork time
            twin_count = await cloner.get_item_count(clone_res.database_name)
            assert twin_count == prod_count

        # 3. Checkpoint A2 assertion: A write to a twin is invisible in production
        target_twin = cloned_twins[0]
        sibling_twin = cloned_twins[1]

        # Insert item into target twin
        insert_code, _, _ = await executor.run_sql(
            [
                "INSERT INTO items (name, description) VALUES "
                "('twin-isolation-probe', 'asserting prod isolation');"
            ],
            database=target_twin,
        )
        assert insert_code == 0

        # Verify target twin has +1 item
        target_twin_count = await cloner.get_item_count(target_twin)
        assert target_twin_count == prod_count + 1

        # Verify sibling twin remains unchanged
        sibling_twin_count = await cloner.get_item_count(sibling_twin)
        assert sibling_twin_count == prod_count

        # Verify production template remains unchanged
        template_count = int(
            (await executor.run_sql(["SELECT count(*) FROM items;"], database="snapshot_template"))[
                1
            ].strip()
        )
        assert template_count == prod_count

    finally:
        # 4. Teardown: removes all twin databases for this incident
        dropped = await cloner.drop_all_incident_databases(incident_id)
        assert sorted(dropped) == sorted(cloned_twins)

        # Confirm all dropped
        remaining = await cloner.list_twin_databases(incident_id)
        assert remaining == []
