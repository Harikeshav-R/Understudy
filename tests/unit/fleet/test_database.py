"""Unit tests for fleet/database.py (build-plan steps A2.3 and A2.3a).

Tests:
- Snapshot refresher: staging + swap by rename, zero persistent template connection (ADR-009).
- Database cloner: CREATE DATABASE ... TEMPLATE snapshot_template.
- Automatic retry on PostgreSQL error 55006 ("source database is being accessed by other users").
- Background refresh loop start, tick, cancel, and error handling.
- Idempotent teardown and safe database drops.
- Item count query for fidelity verification.
- KubectlDatabaseExecutor subprocess execution, timeout, and OSError handling.
- 100% branch and line coverage with zero external network or cluster dependencies.
"""

import asyncio
import shlex
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from understudy.common.clock import FrozenClock
from understudy.common.errors import FleetError
from understudy.fleet.database import (
    CREATED_AT_COMMENT_PREFIX,
    SNAPSHOT_DUMP_PATH,
    DatabaseCommandExecutor,
    KubectlDatabaseExecutor,
    PostgresDatabaseCloner,
    PostgresSnapshotRefresher,
)
from understudy.fleet.fakes import FakeDatabaseCommandExecutor, FakeSnapshotRefresher


class ScriptedDatabaseExecutor(DatabaseCommandExecutor):
    """Configurable mock executor for exercising exact return codes and errors."""

    def __init__(self) -> None:
        self.sql_responses: list[tuple[int, str, str]] = []
        self.pipeline_responses: list[tuple[int, str, str]] = []
        self.sql_calls: list[tuple[str, list[str]]] = []
        self.pipeline_calls: list[str] = []

    async def run_sql(
        self,
        sql_commands: Any,
        database: str = "postgres",
        timeout: float = 30.0,
    ) -> tuple[int, str, str]:
        _ = timeout
        self.sql_calls.append((database, list(sql_commands)))
        if self.sql_responses:
            return self.sql_responses.pop(0)
        return 0, "", ""

    async def run_pipeline(
        self,
        shell_script: str,
        timeout: float = 120.0,
    ) -> tuple[int, str, str]:
        _ = timeout
        self.pipeline_calls.append(shell_script)
        if self.pipeline_responses:
            return self.pipeline_responses.pop(0)
        return 0, "", ""


# ---------------------------------------------------------------------------
# Snapshot Refresher Tests (A2.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_refresher_pipeline_building() -> None:
    executor = FakeDatabaseCommandExecutor()
    refresher = PostgresSnapshotRefresher(executor=executor)
    pipeline = refresher.build_refresh_pipeline()

    assert "DROP DATABASE IF EXISTS snapshot_staging" in pipeline
    assert "CREATE DATABASE snapshot_staging" in pipeline
    assert "pg_dump -h prod-postgres.ust-prod" in pipeline
    assert "psql -v ON_ERROR_STOP=1 -U postgres -d snapshot_staging" in pipeline
    assert "pg_terminate_backend" in pipeline
    assert "DROP DATABASE IF EXISTS snapshot_template" in pipeline
    assert "ALTER DATABASE snapshot_staging RENAME TO snapshot_template" in pipeline

    # Every psql must abort on the first error; otherwise psql exits 0 even when every
    # statement of the restore failed and an empty database is published as the template.
    assert pipeline.count("psql -v ON_ERROR_STOP=1") == pipeline.count("psql ")

    # pg_dump must write to a file, not a pipe: /bin/sh is dash and has no pipefail, so a
    # piped pg_dump failure would be masked by psql's exit status.
    assert f"-d ust_prod > {SNAPSHOT_DUMP_PATH} &&" in pipeline
    assert f"-q -f {SNAPSHOT_DUMP_PATH}" in pipeline
    assert "|" not in pipeline

    # The restored staging database is probed before it is renamed over the template.
    assert (
        "psql -v ON_ERROR_STOP=1 -U postgres -d snapshot_staging "
        "-c 'SELECT 1 FROM items LIMIT 1;'" in pipeline
    )

    # Tokenize the way /bin/sh will: the datname must reach psql as a single-quoted SQL
    # string literal. Double quotes would make it an identifier, and the pg_terminate_backend
    # safety net would then error on every refresh cycle without failing the pipeline.
    tokens = shlex.split(pipeline)
    terminate_sql = next(tok for tok in tokens if "pg_terminate_backend(pid)" in tok)
    assert "WHERE datname = 'snapshot_template'" in terminate_sql
    assert 'datname = "snapshot_template"' not in pipeline


@pytest.mark.asyncio
async def test_snapshot_refresher_success() -> None:
    clock = FrozenClock(datetime(2026, 9, 12, 20, 0, 0, tzinfo=UTC))
    executor = FakeDatabaseCommandExecutor()
    refresher = PostgresSnapshotRefresher(executor=executor, clock=clock)

    assert await refresher.get_last_snapshot_time() is None

    refreshed_at = await refresher.refresh_snapshot()
    assert refreshed_at == clock.now()
    assert await refresher.get_last_snapshot_time() == clock.now()
    assert len(executor.pipeline_history) == 1
    assert "snapshot_template" in executor.databases


@pytest.mark.asyncio
async def test_snapshot_refresher_pipeline_failure() -> None:
    executor = FakeDatabaseCommandExecutor()
    executor.fail_pipeline = True
    refresher = PostgresSnapshotRefresher(executor=executor)

    with pytest.raises(FleetError, match="Snapshot refresh pipeline failed with exit code 1"):
        await refresher.refresh_snapshot()


@pytest.mark.asyncio
async def test_snapshot_refresher_loop_start_stop() -> None:
    executor = FakeDatabaseCommandExecutor()
    refresher = PostgresSnapshotRefresher(executor=executor, refresh_interval_seconds=0.01)

    await refresher.start()
    assert refresher._loop_task is not None
    # Starting again is idempotent
    await refresher.start()

    # Allow loop to tick at least once
    await asyncio.sleep(0.03)
    assert await refresher.get_last_snapshot_time() is not None

    await refresher.stop()

    # Stopping again when already stopped is safe
    refresher_idle = PostgresSnapshotRefresher(executor=executor)
    await refresher_idle.stop()


@pytest.mark.asyncio
async def test_snapshot_refresher_loop_exception_resilience() -> None:
    scripted = ScriptedDatabaseExecutor()
    # First tick fails, second succeeds
    scripted.pipeline_responses = [
        (1, "", "temporary connection error"),
        (0, "success", ""),
    ]

    refresher = PostgresSnapshotRefresher(executor=scripted, refresh_interval_seconds=0.01)
    await refresher.start()
    await asyncio.sleep(0.04)
    await refresher.stop()

    assert len(scripted.pipeline_calls) >= 2


# ---------------------------------------------------------------------------
# Database Cloner Tests (A2.3a)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_database_cloner_success() -> None:
    clock = FrozenClock(datetime(2026, 9, 12, 20, 30, 0, tzinfo=UTC))
    executor = FakeDatabaseCommandExecutor()
    refresher = FakeSnapshotRefresher(clock=clock)
    await refresher.refresh_snapshot()

    cloner = PostgresDatabaseCloner(executor=executor, refresher=refresher, clock=clock)

    result = await cloner.clone_twin_database("inc-123", 0)
    assert result.database_name == "twin_inc_123_0"
    assert result.incident_id == "inc-123"
    assert result.candidate_index == 0
    assert result.forked_from_snapshot_at == clock.now()
    assert result.cloned_at == clock.now()
    assert "twin_inc_123_0" in executor.databases


@pytest.mark.asyncio
async def test_database_cloner_without_refresher() -> None:
    clock = FrozenClock(datetime(2026, 9, 12, 20, 30, 0, tzinfo=UTC))
    executor = FakeDatabaseCommandExecutor()
    cloner = PostgresDatabaseCloner(executor=executor, refresher=None, clock=clock)

    result = await cloner.clone_twin_database("inc-123", 1)
    assert result.database_name == "twin_inc_123_1"
    assert result.forked_from_snapshot_at == clock.now()


@pytest.mark.asyncio
async def test_database_cloner_missing_template() -> None:
    executor = FakeDatabaseCommandExecutor()
    executor.databases.remove("snapshot_template")
    cloner = PostgresDatabaseCloner(executor=executor)

    with pytest.raises(
        FleetError, match="Snapshot template database 'snapshot_template' does not exist"
    ):
        await cloner.clone_twin_database("inc-123", 0)


@pytest.mark.asyncio
async def test_database_cloner_retry_on_in_use_success() -> None:
    executor = FakeDatabaseCommandExecutor()
    # Simulate 2 in-use failures before success
    executor.in_use_counter = 2
    cloner = PostgresDatabaseCloner(
        executor=executor,
        max_retries=3,
        base_backoff_seconds=0.001,
        max_backoff_seconds=0.01,
    )

    result = await cloner.clone_twin_database("inc-retry", 0)
    assert result.database_name == "twin_inc_retry_0"
    assert "twin_inc_retry_0" in executor.databases


@pytest.mark.asyncio
async def test_database_cloner_retry_exhausted() -> None:
    executor = FakeDatabaseCommandExecutor()
    executor.in_use_counter = 5
    cloner = PostgresDatabaseCloner(
        executor=executor,
        max_retries=2,
        base_backoff_seconds=0.001,
        max_backoff_seconds=0.01,
    )

    with pytest.raises(
        FleetError, match="Failed to clone database twin_inc_fail_0 after 2 attempts"
    ):
        await cloner.clone_twin_database("inc-fail", 0)


@pytest.mark.asyncio
async def test_database_cloner_non_retryable_sql_error() -> None:
    scripted = ScriptedDatabaseExecutor()
    scripted.sql_responses = [
        (0, "1", ""),  # template exists check
        (1, "", "ERROR: disk full or permission denied"),  # non-retryable
    ]
    cloner = PostgresDatabaseCloner(executor=scripted)

    with pytest.raises(
        FleetError, match="Failed to clone database twin_inc_err_0: ERROR: disk full"
    ):
        await cloner.clone_twin_database("inc-err", 0)


@pytest.mark.asyncio
async def test_database_cloner_invalid_name() -> None:
    executor = FakeDatabaseCommandExecutor()
    cloner = PostgresDatabaseCloner(executor=executor)

    with pytest.raises(FleetError, match="Candidate index must be non-negative"):
        await cloner.clone_twin_database("inc-001", -1)

    with pytest.raises(FleetError, match="Incident ID cannot be empty"):
        await cloner.clone_twin_database("", 0)


@pytest.mark.asyncio
async def test_database_cloner_drop_twin() -> None:
    executor = FakeDatabaseCommandExecutor()
    cloner = PostgresDatabaseCloner(executor=executor)
    executor.databases.add("twin_inc_1_0")

    await cloner.drop_twin_database("twin_inc_1_0")
    assert "twin_inc_1_0" not in executor.databases

    # Refuses to drop non-twin databases
    with pytest.raises(FleetError, match="Refusing to drop non-twin database"):
        await cloner.drop_twin_database("prod_postgres")

    # Invalid name rejection
    with pytest.raises(FleetError, match="Invalid database name"):
        await cloner.drop_twin_database("twin;drop database postgres")


@pytest.mark.asyncio
async def test_database_cloner_drop_sql_failure() -> None:
    scripted = ScriptedDatabaseExecutor()
    scripted.sql_responses = [
        (0, "t", ""),  # terminate connections
        (1, "", "drop failed: lock timeout"),  # drop fails
    ]
    cloner = PostgresDatabaseCloner(executor=scripted)

    with pytest.raises(FleetError, match="Failed to drop database twin_test_0: drop failed"):
        await cloner.drop_twin_database("twin_test_0")


@pytest.mark.asyncio
async def test_database_cloner_drop_all_incident_databases() -> None:
    executor = FakeDatabaseCommandExecutor()
    cloner = PostgresDatabaseCloner(executor=executor)

    executor.databases.update(["twin_inc_42_0", "twin_inc_42_1", "twin_other_0"])

    dropped = await cloner.drop_all_incident_databases("inc-42")
    assert sorted(dropped) == ["twin_inc_42_0", "twin_inc_42_1"]
    assert "twin_inc_42_0" not in executor.databases
    assert "twin_inc_42_1" not in executor.databases
    assert "twin_other_0" in executor.databases


@pytest.mark.asyncio
async def test_database_cloner_list_databases_all_and_filtered() -> None:
    executor = FakeDatabaseCommandExecutor()
    cloner = PostgresDatabaseCloner(executor=executor)
    executor.databases.update(["twin_inc_1_0", "twin_inc_1_1", "twin_inc_2_0"])

    all_dbs = await cloner.list_twin_databases()
    assert all_dbs == ["twin_inc_1_0", "twin_inc_1_1", "twin_inc_2_0"]

    filtered = await cloner.list_twin_databases(incident_id="inc-1")
    assert filtered == ["twin_inc_1_0", "twin_inc_1_1"]


@pytest.mark.asyncio
async def test_database_cloner_list_databases_error() -> None:
    scripted = ScriptedDatabaseExecutor()
    scripted.sql_responses = [(1, "", "connection error")]
    cloner = PostgresDatabaseCloner(executor=scripted)

    with pytest.raises(FleetError, match="Failed to list twin databases"):
        await cloner.list_twin_databases()


@pytest.mark.asyncio
async def test_database_cloner_get_item_count() -> None:
    executor = FakeDatabaseCommandExecutor()
    executor.items_count["twin_inc_1_0"] = 42
    cloner = PostgresDatabaseCloner(executor=executor)

    count = await cloner.get_item_count("twin_inc_1_0")
    assert count == 42


@pytest.mark.asyncio
async def test_database_cloner_get_item_count_errors() -> None:
    scripted = ScriptedDatabaseExecutor()
    scripted.sql_responses = [(1, "", "relation does not exist")]
    cloner = PostgresDatabaseCloner(executor=scripted)

    with pytest.raises(FleetError, match="Failed to query item count"):
        await cloner.get_item_count("twin_test_0")

    scripted.sql_responses = [(0, "not-an-int", "")]
    with pytest.raises(FleetError, match="Unexpected item count output"):
        await cloner.get_item_count("twin_test_0")


# ---------------------------------------------------------------------------
# KubectlDatabaseExecutor Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kubectl_executor_run_sql_success() -> None:
    executor = KubectlDatabaseExecutor(namespace="test-ns", deployment="test-pg")

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"1\n", b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        code, stdout, stderr = await executor.run_sql(["SELECT 1;"], database="postgres")
        assert code == 0
        assert stdout == "1"
        assert stderr == ""

        mock_exec.assert_called_once()
        cmd = mock_exec.call_args[0]
        assert "psql" in cmd
        assert "-c" in cmd
        assert "SELECT 1;" in cmd


@pytest.mark.asyncio
async def test_kubectl_executor_run_pipeline_success() -> None:
    executor = KubectlDatabaseExecutor()

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"pipeline done", b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        code, stdout, stderr = await executor.run_pipeline("echo 1 | cat")
        assert code == 0
        assert stdout == "pipeline done"
        assert stderr == ""

        cmd = mock_exec.call_args[0]
        assert "/bin/sh" in cmd
        assert "echo 1 | cat" in cmd


@pytest.mark.asyncio
async def test_kubectl_executor_timeout() -> None:
    executor = KubectlDatabaseExecutor()

    mock_proc = AsyncMock()
    mock_proc.communicate.side_effect = TimeoutError()
    mock_proc.kill = MagicMock()

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(FleetError, match="Database command timed out"),
    ):
        await executor.run_sql(["SELECT pg_sleep(100);"], timeout=0.01)
    mock_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_kubectl_executor_oserror() -> None:
    executor = KubectlDatabaseExecutor()

    with (
        patch("asyncio.create_subprocess_exec", side_effect=OSError("binary not found")),
        pytest.raises(FleetError, match="Failed to execute kubectl subprocess"),
    ):
        await executor.run_sql(["SELECT 1;"])


@pytest.mark.asyncio
async def test_clone_stamps_creation_time_and_reports_age() -> None:
    """A cloned twin database records when it was created so GC can judge its age."""
    clock = FrozenClock(datetime(2026, 9, 12, 20, 0, 0, tzinfo=UTC))
    executor = FakeDatabaseCommandExecutor()
    cloner = PostgresDatabaseCloner(executor=executor, clock=clock)

    await cloner.clone_twin_database("inc_test", 0)
    stamped = [
        sql
        for _db, cmds in executor.sql_history
        for sql in cmds
        if sql.startswith("COMMENT ON DATABASE")
    ]
    assert stamped == [
        f"COMMENT ON DATABASE twin_inc_test_0 IS "
        f"'{CREATED_AT_COMMENT_PREFIX}2026-09-12T20:00:00+00:00';"
    ]

    # Age is measured from the stamp, not from the listing time.
    later = PostgresDatabaseCloner(
        executor=executor,
        clock=FrozenClock(datetime(2026, 9, 12, 21, 0, 0, tzinfo=UTC)),
    )
    infos = await later.list_twin_databases_with_age()
    assert [(i.name, i.age_seconds) for i in infos] == [("twin_inc_test_0", 3600.0)]


@pytest.mark.asyncio
async def test_list_twin_databases_with_age_edge_cases() -> None:
    """Unstamped, malformed, naive, and failing listings are all handled explicitly."""
    clock = FrozenClock(datetime(2026, 9, 12, 20, 0, 0, tzinfo=UTC))
    executor = FakeDatabaseCommandExecutor()
    cloner = PostgresDatabaseCloner(executor=executor, clock=clock)

    executor.databases.update(
        {"twin_unstamped_0", "twin_malformed_0", "twin_naive_0", "twin_future_0"}
    )
    executor.database_comments["twin_malformed_0"] = f"{CREATED_AT_COMMENT_PREFIX}not-a-date"
    # A stamp written without an offset is read as UTC.
    executor.database_comments["twin_naive_0"] = f"{CREATED_AT_COMMENT_PREFIX}2026-09-12T19:00:00"
    # A stamp in the future clamps to zero rather than going negative.
    executor.database_comments["twin_future_0"] = (
        f"{CREATED_AT_COMMENT_PREFIX}2026-09-12T21:00:00+00:00"
    )

    ages = {i.name: i.age_seconds for i in await cloner.list_twin_databases_with_age()}
    assert ages["twin_unstamped_0"] is None
    assert ages["twin_malformed_0"] is None
    assert ages["twin_naive_0"] == 3600.0
    assert ages["twin_future_0"] == 0.0

    # A naive clock is treated as UTC too.
    class NaiveClock:
        def now(self) -> datetime:
            return datetime(2026, 9, 12, 20, 0, 0)

        async def sleep(self, seconds: float) -> None:
            pass

    naive_cloner = PostgresDatabaseCloner(executor=executor, clock=NaiveClock())
    naive_ages = {i.name: i.age_seconds for i in await naive_cloner.list_twin_databases_with_age()}
    assert naive_ages["twin_naive_0"] == 3600.0

    executor.fail_sql = "connection refused"
    with pytest.raises(FleetError, match="Failed to list twin databases with age"):
        await cloner.list_twin_databases_with_age()


@pytest.mark.asyncio
async def test_list_twin_databases_with_age_skips_blank_rows() -> None:
    """Blank rows in psql output are ignored rather than becoming nameless databases."""

    class BlankRowExecutor:
        """Executor whose listing output contains blank and whitespace-only rows."""

        async def run_sql(
            self,
            sql_commands: Any,
            database: str = "postgres",
            timeout: float = 30.0,
        ) -> tuple[int, str, str]:
            _ = (sql_commands, database, timeout)
            return 0, "twin_a_0|\n\n   \ntwin_b_0|", ""

        async def run_pipeline(
            self, shell_script: str, timeout: float = 120.0
        ) -> tuple[int, str, str]:
            _ = (shell_script, timeout)
            return 0, "", ""

    cloner = PostgresDatabaseCloner(
        executor=BlankRowExecutor(),
        clock=FrozenClock(datetime(2026, 9, 12, 20, 0, 0, tzinfo=UTC)),
    )
    infos = await cloner.list_twin_databases_with_age()
    assert [i.name for i in infos] == ["twin_a_0", "twin_b_0"]
    assert all(i.age_seconds is None for i in infos)
