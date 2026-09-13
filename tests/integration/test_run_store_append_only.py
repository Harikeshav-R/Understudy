"""Integration tests verifying append-only semantics on real PostgreSQL.

Implements build-plan step B2.1 and Checkpoint B2:
- Runs against live system-postgres on localhost:5434.
- Asserts that UPDATE queries against the `runs` table are no-ops (runs_no_update rule).
- Asserts that DELETE queries against the `runs` table are no-ops (runs_no_delete rule).
- Asserts primary key collisions raise StoreError (append-only immutability).
- Verifies PostgresPlaybookStore pgvector cosine search and atomic counters.
- Verifies PostgresEvalStore scenario execution recording.
"""

from datetime import UTC, datetime

import psycopg
import pytest
from sqlalchemy import text

from understudy.common.config import get_settings
from understudy.common.errors import StoreError
from understudy.contracts.enums import ActionType, FailureClass, RunOutcome
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.contracts.run import RunRecord
from understudy.store.database import StoreDatabase
from understudy.store.migrations import apply_migrations
from understudy.store.postgres import (
    PostgresEvalStore,
    PostgresPlaybookStore,
    PostgresRunStore,
)


def _postgres_system_available() -> bool:
    """Check whether system-postgres on localhost:5434 is reachable."""
    dsn = get_settings().endpoints.postgres_system
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
            row = cur.fetchone()
            return bool(row and row[0] == 1)
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    """Ensure database schema and migrations are applied before integration tests."""
    if not _postgres_system_available():
        pytest.skip("system-postgres on localhost:5434 is not reachable (requires make up)")
    apply_migrations()


@pytest.fixture
def unique_run_record() -> RunRecord:
    """Generate a unique RunRecord for testing."""
    now = datetime.now(UTC)
    unique_id = f"run_int_{int(now.timestamp() * 1000)}"
    alert = Alert(
        alert_id=f"alt_{unique_id}",
        source="synthetic",
        title="Integration test alert",
        service="data-service",
        severity="critical",
        fired_at=now,
    )
    metric_window = MetricWindow(
        service="data-service",
        start_time=now,
        end_time=now,
        p99_latency_ms=350.0,
        error_rate=0.01,
        request_count=500,
    )
    context = IncidentContext(
        incident_id=f"inc_{unique_id}",
        alert=alert,
        signatures=[],
        metrics_window=metric_window,
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(observed_at=now),
        gathered_at=now,
    )
    return RunRecord(
        run_id=unique_id,
        incident_id=f"inc_{unique_id}",
        scenario_id="scenario_append_only",
        started_at=now,
        finished_at=None,
        outcome=RunOutcome.EXECUTED,
        prod_applied_plan_id="plan_good",
        prod_outcome="resolved",
        escalation_reason=None,
        context=context,
        plans=[],
        evidence=[],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_store_append_only(unique_run_record: RunRecord) -> None:
    """Assert that runs table is strictly append-only: UPDATE and DELETE are no-ops."""
    db = StoreDatabase()
    store = PostgresRunStore(db=db)
    run_id = unique_run_record.run_id

    # 1. Record run record append-only
    await store.record_run(unique_run_record)

    fetched = await store.get_run(run_id)
    assert fetched is not None
    assert fetched.run_id == run_id
    assert fetched.outcome == RunOutcome.EXECUTED
    assert fetched.escalation_reason is None

    # 2. Attempt direct SQL UPDATE on the run record
    async with db.session() as session:
        await session.execute(
            text(
                "UPDATE runs SET outcome = 'failed', escalation_reason = 'tampered' "
                "WHERE run_id = :run_id;"
            ),
            {"run_id": run_id},
        )
        await session.commit()

    # Verify that the record was NOT updated (runs_no_update rule in effect)
    post_update = await store.get_run(run_id)
    assert post_update is not None
    assert post_update.outcome == RunOutcome.EXECUTED
    assert post_update.escalation_reason is None

    # 3. Attempt direct SQL DELETE on the run record
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM runs WHERE run_id = :run_id;"),
            {"run_id": run_id},
        )
        await session.commit()

    # Verify that the record was NOT deleted (runs_no_delete rule in effect)
    post_delete = await store.get_run(run_id)
    assert post_delete is not None
    assert post_delete.run_id == run_id

    # 4. Attempt duplicate primary key insert raises StoreError
    with pytest.raises(StoreError, match="already exists; runs table is append-only"):
        await store.record_run(unique_run_record)

    # 5. Verify query methods
    runs_list = await store.list_runs(incident_id=unique_run_record.incident_id)
    assert any(r.run_id == run_id for r in runs_list)

    active_runs = await store.get_active_runs()
    assert any(r.run_id == run_id for r in active_runs)

    await db.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_playbook_store_live() -> None:
    """Verify pgvector vector similarity search and mutable counter increments on live Postgres."""
    db = StoreDatabase()
    store = PostgresPlaybookStore(db=db)

    now = datetime.now(UTC)
    pb_id = f"pb_{int(now.timestamp() * 1000)}"
    plan = RemediationPlan(
        plan_id="plan_rollback",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service"),
        target_resources=[],
        declared_blast_set=[],
        rationale="Rollback to known good image",
        origin="playbook",
    )

    # Unit vector in 1024 dimensions
    embedding_a = [0.0] * 1024
    embedding_a[0] = 1.0

    await store.save_playbook(
        playbook_id=pb_id,
        failure_class=FailureClass.BAD_DEPLOY,
        signature_text="database connection pool exhaustion",
        embedding=embedding_a,
        plan=plan,
        evidence_refs=["run_ref_1"],
        origin="incident",
    )

    # Fetch by ID
    loaded_plan = await store.get_playbook(pb_id)
    assert loaded_plan is not None
    assert loaded_plan.plan_id == "plan_rollback"
    assert loaded_plan.action == ActionType.ROLLBACK_DEPLOY

    # Search with nearest vector
    search_results = await store.search_playbooks(embedding=embedding_a, limit=5)
    assert len(search_results) >= 1
    assert any(p.plan_id == "plan_rollback" for p in search_results)

    # Verify atomic counter increments
    await store.increment_success(pb_id)
    await store.increment_failure(pb_id)

    async with db.session() as session:
        res = await session.execute(
            text("SELECT successes, failures FROM playbooks WHERE playbook_id = :pb_id;"),
            {"pb_id": pb_id},
        )
        row = res.fetchone()
        assert row is not None
        assert row[0] == 1  # successes
        assert row[1] == 1  # failures

    await db.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_eval_store_live(unique_run_record: RunRecord) -> None:
    """Verify evaluation scenario result persistence and query retrieval."""
    db = StoreDatabase()
    run_store = PostgresRunStore(db=db)
    eval_store = PostgresEvalStore(db=db)

    # Store run record first to satisfy foreign key constraint
    await run_store.record_run(unique_run_record)

    scenario_id = f"eval_scenario_{unique_run_record.run_id}"
    await eval_store.record_scenario_result(
        scenario_id=scenario_id,
        run_id=unique_run_record.run_id,
        repeat_index=0,
        twin_predicted_success=True,
        prod_actual_success=True,
        expected_escalation=False,
        did_escalate=False,
        runner_up_plan_id="plan_runner_up",
        runner_up_prod_success=False,
    )

    results = await eval_store.get_scenario_results(scenario_id=scenario_id)
    assert len(results) == 1
    result = results[0]
    assert result["scenario_id"] == scenario_id
    assert result["run_id"] == unique_run_record.run_id
    assert result["twin_predicted_success"] is True
    assert result["prod_actual_success"] is True
    assert result["runner_up_plan_id"] == "plan_runner_up"

    await db.dispose()
