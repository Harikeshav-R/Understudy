"""PostgreSQL snapshot refresher and twin database cloner.

Implements build-plan step A2.3 and A2.3a:
- Snapshot refresher: staging + rename pattern, never holding an open connection to
  snapshot_template (ADR-009).
- Twin DB cloner: CREATE DATABASE ... TEMPLATE snapshot_template with retry on
  "source database is being accessed by other users" (SQLSTATE 55006).

Strict architectural rules:
- No psycopg or sqlalchemy imports (enforced by .importlinter).
- Operations execute via container shell/psql pipelines through DatabaseCommandExecutor.
- Pure async, deterministic timestamps via Clock, 100% branch and line coverage.
"""

import asyncio
import contextlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from understudy.common.clock import Clock, resolve_clock
from understudy.common.errors import FleetError
from understudy.common.logging import get_logger
from understudy.fleet.api import DatabaseCloner, SnapshotRefresher
from understudy.fleet.models import DatabaseCloneResult, TwinDatabaseInfo
from understudy.fleet.render import sanitize_database_name

logger = get_logger(__name__)

DEFAULT_SNAPSHOT_REFRESH_INTERVAL_SECONDS = 60.0
DEFAULT_MAX_CLONE_RETRIES = 5
DEFAULT_BASE_BACKOFF_SECONDS = 0.1
DEFAULT_MAX_BACKOFF_SECONDS = 2.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0
DEFAULT_PIPELINE_TIMEOUT_SECONDS = 120.0
SNAPSHOT_DUMP_PATH = "/tmp/ust_snapshot.sql"
CREATED_AT_COMMENT_PREFIX = "understudy:created_at="

SAFE_DB_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")


@runtime_checkable
class DatabaseCommandExecutor(Protocol):
    """Protocol for executing psql queries and command pipelines on twin-postgres."""

    async def run_sql(
        self,
        sql_commands: Sequence[str],
        database: str = "postgres",
        timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> tuple[int, str, str]:
        """Execute one or more SQL commands sequentially using psql.

        Returns:
            tuple[int, str, str]: (returncode, stdout, stderr)
        """
        raise NotImplementedError

    async def run_pipeline(
        self,
        shell_script: str,
        timeout: float = DEFAULT_PIPELINE_TIMEOUT_SECONDS,
    ) -> tuple[int, str, str]:
        """Execute a shell pipeline inside the twin-postgres environment.

        Returns:
            tuple[int, str, str]: (returncode, stdout, stderr)
        """
        raise NotImplementedError


class KubectlDatabaseExecutor(DatabaseCommandExecutor):
    """Executes database commands inside the twin-postgres pod via kubectl exec."""

    def __init__(
        self,
        namespace: str = "ust-system",
        deployment: str = "twin-postgres",
        context: str = "k3d-ust",
        kubectl_bin: str = "kubectl",
    ) -> None:
        self.namespace = namespace
        self.deployment = deployment
        self.context = context
        self.kubectl_bin = kubectl_bin

    def _base_cmd(self) -> list[str]:
        return [
            self.kubectl_bin,
            "exec",
            "-n",
            self.namespace,
            f"deploy/{self.deployment}",
            f"--context={self.context}",
            "--",
        ]

    async def run_sql(
        self,
        sql_commands: Sequence[str],
        database: str = "postgres",
        timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> tuple[int, str, str]:
        cmd = [
            *self._base_cmd(),
            "psql",
            "-U",
            "postgres",
            "-d",
            database,
            "-t",
            "-A",
        ]
        for sql in sql_commands:
            cmd.extend(["-c", sql])

        return await self._run_proc(cmd, timeout=timeout)

    async def run_pipeline(
        self,
        shell_script: str,
        timeout: float = DEFAULT_PIPELINE_TIMEOUT_SECONDS,
    ) -> tuple[int, str, str]:
        cmd = [*self._base_cmd(), "/bin/sh", "-c", shell_script]
        return await self._run_proc(cmd, timeout=timeout)

    async def _run_proc(self, cmd: list[str], timeout: float) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return (
                proc.returncode if proc.returncode is not None else -1,
                stdout_bytes.decode(errors="replace").strip(),
                stderr_bytes.decode(errors="replace").strip(),
            )
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise FleetError(
                f"Database command timed out after {timeout}s: {' '.join(cmd)}"
            ) from exc
        except OSError as exc:
            raise FleetError(f"Failed to execute kubectl subprocess: {exc}") from exc


class PostgresSnapshotRefresher(SnapshotRefresher):
    """Refreshes the snapshot_template database from production every 60 seconds.

    Uses the staging-and-rename pattern to ensure no active connections are ever
    held to snapshot_template (ADR-009).
    """

    def __init__(
        self,
        executor: DatabaseCommandExecutor,
        source_host: str = "prod-postgres.ust-prod",
        source_port: int = 5432,
        source_user: str = "postgres",
        source_db: str = "ust_prod",
        target_template_db: str = "snapshot_template",
        target_staging_db: str = "snapshot_staging",
        refresh_interval_seconds: float = DEFAULT_SNAPSHOT_REFRESH_INTERVAL_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        self.executor = executor
        self.source_host = source_host
        self.source_port = source_port
        self.source_user = source_user
        self.source_db = source_db
        self.target_template_db = target_template_db
        self.target_staging_db = target_staging_db
        self.refresh_interval_seconds = refresh_interval_seconds
        self.clock: Clock = resolve_clock(clock)

        self._last_snapshot_time: datetime | None = None
        self._loop_task: asyncio.Task[None] | None = None

    def build_refresh_pipeline(self) -> str:
        """Construct the atomic shell pipeline for staging dump and swap by rename.

        Every step must be able to fail the whole refresh:
        - `-v ON_ERROR_STOP=1` on every psql, because psql otherwise exits 0 even when all of
          its statements error, and `run_pipeline` reports only the last command's status.
        - the dump goes to a file rather than a pipe, so `pg_dump`'s exit status is the one
          the `&&` chain sees (`/bin/sh` here is dash, which has no `pipefail`).
        - a guard query against the restored staging database, so an empty restore can never
          be renamed over the template that every twin clones from.
        """
        psql = "psql -v ON_ERROR_STOP=1 -U postgres"
        return (
            f"{psql} -c 'DROP DATABASE IF EXISTS {self.target_staging_db};' "
            f"-c 'CREATE DATABASE {self.target_staging_db};' && "
            f"pg_dump -h {self.source_host} -p {self.source_port} -U {self.source_user} "
            f"-d {self.source_db} > {SNAPSHOT_DUMP_PATH} && "
            f"{psql} -d {self.target_staging_db} -q -f {SNAPSHOT_DUMP_PATH} && "
            f"{psql} -d {self.target_staging_db} -c 'SELECT 1 FROM items LIMIT 1;' && "
            f"{psql} -c 'SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '\"'\"'{self.target_template_db}'\"'\"' "
            f"AND pid <> pg_backend_pid();' "
            f"-c 'DROP DATABASE IF EXISTS {self.target_template_db};' "
            f"-c 'ALTER DATABASE {self.target_staging_db} RENAME TO {self.target_template_db};' "
            f"&& rm -f {SNAPSHOT_DUMP_PATH}"
        )

    async def refresh_snapshot(self) -> datetime:
        """Perform a single staging-and-rename refresh cycle."""
        logger.info(
            "snapshot_refresh_started",
            source_db=self.source_db,
            template_db=self.target_template_db,
        )
        pipeline = self.build_refresh_pipeline()
        returncode, stdout, stderr = await self.executor.run_pipeline(
            pipeline, timeout=DEFAULT_PIPELINE_TIMEOUT_SECONDS
        )

        if returncode != 0:
            logger.error(
                "snapshot_refresh_failed",
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
            raise FleetError(
                f"Snapshot refresh pipeline failed with exit code {returncode}: {stderr or stdout}"
            )

        now = self.clock.now()
        self._last_snapshot_time = now
        logger.info(
            "snapshot_refresh_completed",
            template_db=self.target_template_db,
            refreshed_at=now.isoformat(),
        )
        return now

    async def get_last_snapshot_time(self) -> datetime | None:
        """Return the timestamp of the last successful snapshot refresh."""
        return self._last_snapshot_time

    async def start(self) -> None:
        """Start the background periodic refresh loop."""
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._periodic_loop())

    async def stop(self) -> None:
        """Gracefully stop the background periodic refresh loop."""
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        self._loop_task = None

    async def _periodic_loop(self) -> None:
        """Periodic loop refreshing the snapshot every configured interval."""
        while True:
            try:
                await self.refresh_snapshot()
            except Exception as exc:  # pragma: no branch
                logger.warning("snapshot_refresh_periodic_error", error=str(exc))
            try:
                await asyncio.sleep(self.refresh_interval_seconds)
            except asyncio.CancelledError:
                return


class PostgresDatabaseCloner(DatabaseCloner):
    """Clones and manages isolated twin databases from snapshot_template.

    Implements ADR-009 sub-second filesystem template cloning with automatic retry
    on PostgreSQL concurrency error 55006 ("source database is being accessed by other users").
    """

    def __init__(
        self,
        executor: DatabaseCommandExecutor,
        template_db: str = "snapshot_template",
        refresher: SnapshotRefresher | None = None,
        max_retries: int = DEFAULT_MAX_CLONE_RETRIES,
        base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        self.executor = executor
        self.template_db = template_db
        self.refresher = refresher
        self.max_retries = max(1, max_retries)
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.clock: Clock = resolve_clock(clock)

    async def clone_twin_database(
        self, incident_id: str, candidate_index: int
    ) -> DatabaseCloneResult:
        """Clone snapshot_template into an isolated twin database with retry."""
        db_name = sanitize_database_name(incident_id, candidate_index)
        self._validate_db_name(db_name)

        # Check template existence
        exists_code, exists_out, _ = await self.executor.run_sql(
            [f"SELECT 1 FROM pg_database WHERE datname = '{self.template_db}';"]
        )
        if exists_code != 0 or "1" not in exists_out.split():
            raise FleetError(
                f"Snapshot template database {self.template_db!r} does not exist. "
                "Snapshot refresher must run before cloning."
            )

        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            logger.info(
                "twin_database_clone_attempt",
                database=db_name,
                template=self.template_db,
                attempt=attempt,
            )

            created_at = self.clock.now()
            clone_sql = f"CREATE DATABASE {db_name} TEMPLATE {self.template_db};"
            # Stamp the creation time so garbage collection can tell an abandoned twin
            # database from one whose namespace has not been created yet (ADR-009 clones the
            # database before the namespace exists).
            stamp_sql = (
                f"COMMENT ON DATABASE {db_name} IS "
                f"'{CREATED_AT_COMMENT_PREFIX}{created_at.isoformat()}';"
            )
            code, stdout, stderr = await self.executor.run_sql([clone_sql, stamp_sql])

            if code == 0:
                now = created_at
                forked_at = (
                    await self.refresher.get_last_snapshot_time()
                    if self.refresher is not None
                    else None
                ) or now
                logger.info(
                    "twin_database_cloned",
                    database=db_name,
                    template=self.template_db,
                    attempt=attempt,
                )
                return DatabaseCloneResult(
                    database_name=db_name,
                    incident_id=incident_id,
                    candidate_index=candidate_index,
                    forked_from_snapshot_at=forked_at,
                    cloned_at=now,
                )

            last_error = stderr or stdout
            if self._is_in_use_error(last_error):
                logger.warning(
                    "twin_database_clone_retry_in_use",
                    database=db_name,
                    attempt=attempt,
                    max_retries=self.max_retries,
                    error=last_error,
                )
                # Terminate any stray connections to the template
                await self._terminate_connections(self.template_db)

                if attempt < self.max_retries:
                    backoff = min(
                        self.base_backoff_seconds * (2 ** (attempt - 1)),
                        self.max_backoff_seconds,
                    )
                    await asyncio.sleep(backoff)
                    continue
            else:
                raise FleetError(f"Failed to clone database {db_name}: {last_error}")

        raise FleetError(
            f"Failed to clone database {db_name} after {self.max_retries} attempts "
            f"due to active connections on template: {last_error}"
        )

    async def drop_twin_database(self, database_name: str) -> None:
        """Drop a twin database after terminating its active connections."""
        self._validate_db_name(database_name)
        if not database_name.startswith("twin_"):
            raise FleetError(
                f"Refusing to drop non-twin database {database_name!r}: "
                "database name must start with 'twin_'"
            )

        logger.info("twin_database_drop_started", database=database_name)
        await self._terminate_connections(database_name)
        code, stdout, stderr = await self.executor.run_sql(
            [f"DROP DATABASE IF EXISTS {database_name} WITH (FORCE);"]
        )
        if code != 0:
            raise FleetError(f"Failed to drop database {database_name}: {stderr or stdout}")
        logger.info("twin_database_dropped", database=database_name)

    async def drop_all_incident_databases(self, incident_id: str) -> list[str]:
        """Drop all twin databases created for a specific incident."""
        clean_incident = incident_id.replace("-", "_").strip()
        prefix = f"twin_{clean_incident}_"
        twins = await self.list_twin_databases()

        dropped: list[str] = []
        for db in twins:
            if db.startswith(prefix):
                await self.drop_twin_database(db)
                dropped.append(db)
        return dropped

    async def list_twin_databases(self, incident_id: str | None = None) -> list[str]:
        """List active twin databases matching optional incident_id prefix."""
        code, stdout, stderr = await self.executor.run_sql(
            ["SELECT datname FROM pg_database WHERE datname LIKE 'twin_%' ORDER BY datname;"]
        )
        if code != 0:
            raise FleetError(f"Failed to list twin databases: {stderr or stdout}")

        names = [line.strip() for line in stdout.splitlines() if line.strip()]
        if incident_id:
            clean_incident = incident_id.replace("-", "_").strip()
            prefix = f"twin_{clean_incident}_"
            return [name for name in names if name.startswith(prefix)]
        return names

    async def list_twin_databases_with_age(self) -> list[TwinDatabaseInfo]:
        """List twin databases alongside the age recorded in their creation stamp."""
        code, stdout, stderr = await self.executor.run_sql(
            [
                "SELECT d.datname, coalesce(shobj_description(d.oid, 'pg_database'), '') "
                "FROM pg_database d WHERE d.datname LIKE 'twin_%' ORDER BY d.datname;"
            ]
        )
        if code != 0:
            raise FleetError(f"Failed to list twin databases with age: {stderr or stdout}")

        now = self.clock.now()
        infos: list[TwinDatabaseInfo] = []
        for line in stdout.splitlines():
            raw = line.strip()
            if not raw:
                continue
            name, _, comment = raw.partition("|")
            infos.append(
                TwinDatabaseInfo(
                    name=name.strip(),
                    age_seconds=self._comment_age_seconds(comment, now),
                )
            )
        return infos

    def _comment_age_seconds(self, comment: str, now: datetime) -> float | None:
        """Derive an age in seconds from a twin database's creation-stamp comment."""
        stamp = comment.strip()
        if not stamp.startswith(CREATED_AT_COMMENT_PREFIX):
            return None
        try:
            created = datetime.fromisoformat(stamp[len(CREATED_AT_COMMENT_PREFIX) :])
        except ValueError:
            return None

        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return max(0.0, (now - created).total_seconds())

    async def get_item_count(self, database_name: str) -> int:
        """Count items in a database for verification assertions."""
        self._validate_db_name(database_name)
        code, stdout, stderr = await self.executor.run_sql(
            ["SELECT count(*) FROM items;"], database=database_name
        )
        if code != 0:
            raise FleetError(f"Failed to query item count in {database_name}: {stderr or stdout}")
        try:
            return int(stdout.strip())
        except ValueError as exc:
            raise FleetError(
                f"Unexpected item count output from {database_name}: {stdout!r}"
            ) from exc

    async def _terminate_connections(self, database_name: str) -> None:
        """Terminate all non-self connections to a database."""
        terminate_sql = (
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{database_name}' AND pid <> pg_backend_pid();"
        )
        await self.executor.run_sql([terminate_sql])

    def _is_in_use_error(self, message: str) -> bool:
        """Detect PostgreSQL SQLSTATE 55006 concurrency error."""
        normalized = message.lower()
        return (
            "being accessed by other users" in normalized
            or "55006" in normalized
            or "object_in_use" in normalized
        )

    def _validate_db_name(self, name: str) -> None:
        """Guard against unsafe database identifiers."""
        if not name or not SAFE_DB_NAME_RE.match(name):
            raise FleetError(f"Invalid database name: {name!r}")


__all__ = [
    "CREATED_AT_COMMENT_PREFIX",
    "DEFAULT_BASE_BACKOFF_SECONDS",
    "DEFAULT_COMMAND_TIMEOUT_SECONDS",
    "DEFAULT_MAX_BACKOFF_SECONDS",
    "DEFAULT_MAX_CLONE_RETRIES",
    "DEFAULT_PIPELINE_TIMEOUT_SECONDS",
    "DEFAULT_SNAPSHOT_REFRESH_INTERVAL_SECONDS",
    "SNAPSHOT_DUMP_PATH",
    "DatabaseCommandExecutor",
    "KubectlDatabaseExecutor",
    "PostgresDatabaseCloner",
    "PostgresSnapshotRefresher",
]
