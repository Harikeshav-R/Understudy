from collections.abc import Sequence
from datetime import datetime

from understudy.common.clock import Clock, resolve_clock
from understudy.common.errors import FleetError
from understudy.contracts.twin import TwinHandle
from understudy.fleet.api import DatabaseCloner, FleetController, SnapshotRefresher, WorkloadReader
from understudy.fleet.database import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_PIPELINE_TIMEOUT_SECONDS,
    DatabaseCommandExecutor,
)
from understudy.fleet.models import (
    ClusterWorkloadSnapshot,
    ContainerSnapshot,
    DatabaseCloneResult,
    EnvVar,
    ResourceSpec,
    TwinDatabaseInfo,
    WorkloadSnapshot,
)
from understudy.fleet.render import sanitize_database_name


class FakeFleetController(FleetController):
    """Deterministic in-memory twin environment controller."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = resolve_clock(clock)
        self._twins: dict[str, list[TwinHandle]] = {}

    async def fork(self, incident_id: str, n: int) -> list[TwinHandle]:
        """Fork N deterministic twin environments."""
        now = self.clock.now()
        twins: list[TwinHandle] = []
        for i in range(n):
            twin_id = f"twin_{incident_id}_{i}"
            handle = TwinHandle(
                twin_id=twin_id,
                incident_id=incident_id,
                candidate_index=i,
                namespace=f"ust-twin-{incident_id}-{i}",
                database=f"twin_{incident_id}_{i}_db",
                forked_from_snapshot_at=now,
                ready_at=now,
                state="ready",
            )
            twins.append(handle)
        self._twins[incident_id] = twins
        return twins

    async def teardown(self, twin_handle: TwinHandle) -> None:
        """Tear down a specific twin."""
        incident_id = twin_handle.incident_id
        if incident_id in self._twins:
            self._twins[incident_id] = [
                t for t in self._twins[incident_id] if t.twin_id != twin_handle.twin_id
            ]

    async def teardown_all(self, incident_id: str) -> None:
        """Idempotently tear down all twins for an incident."""
        self._twins.pop(incident_id, None)


class FakeWorkloadReader(WorkloadReader):
    """Deterministic fake workload reader for testing without a cluster."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = resolve_clock(clock)

    async def read_workloads(
        self,
        namespace: str,
        exclude_components: set[str] | None = None,
    ) -> ClusterWorkloadSnapshot:
        """Return deterministic snapshot of understudy production workloads."""
        res_standard = ResourceSpec(
            requests={"cpu": "50m", "memory": "64Mi"},
            limits={"cpu": "200m", "memory": "150Mi"},
        )
        res_postgres = ResourceSpec(
            requests={"cpu": "100m", "memory": "128Mi"},
            limits={"cpu": "500m", "memory": "400Mi"},
        )

        all_workloads = {
            "edge-gateway": WorkloadSnapshot(
                name="edge-gateway",
                namespace=namespace,
                component="gateway",
                labels={"app": "edge-gateway", "app.kubernetes.io/component": "gateway"},
                replicas=1,
                containers=[
                    ContainerSnapshot(
                        name="edge-gateway",
                        image_tag="localhost:5001/edge-gateway:good",
                        image_digest="sha256:d2a1193baf674530fd8721699f05c370c1ab69a04480259a3a4e79207aef86d5",
                        pinned_image="localhost:5001/edge-gateway@sha256:d2a1193baf674530fd8721699f05c370c1ab69a04480259a3a4e79207aef86d5",
                        resources=res_standard,
                        env=[
                            EnvVar(name="UNDERSTUDY_ROLE", value="prod"),
                            EnvVar(name="AUTH_SERVICE_URL", value="http://auth-service:8000"),
                            EnvVar(name="DATA_SERVICE_URL", value="http://data-service:8000"),
                            EnvVar(
                                name="FAULT_INJECTION_SEED",
                                value_from={
                                    "configMapKeyRef": {
                                        "name": "app-config",
                                        "key": "FAULT_INJECTION_SEED",
                                    }
                                },
                            ),
                        ],
                        ports=[{"containerPort": 8000, "name": "http", "protocol": "TCP"}],
                    )
                ],
                config_map_refs=["app-config"],
            ),
            "auth-service": WorkloadSnapshot(
                name="auth-service",
                namespace=namespace,
                component="auth",
                labels={"app": "auth-service", "app.kubernetes.io/component": "auth"},
                replicas=1,
                containers=[
                    ContainerSnapshot(
                        name="auth-service",
                        image_tag="localhost:5001/auth-service:good",
                        image_digest="sha256:a58457d534904d7faac3d4d4ee6f30ffe14e37b7b3f3e9adc4f6cae094a99e84",
                        pinned_image="localhost:5001/auth-service@sha256:a58457d534904d7faac3d4d4ee6f30ffe14e37b7b3f3e9adc4f6cae094a99e84",
                        resources=res_standard,
                        env=[
                            EnvVar(name="UNDERSTUDY_ROLE", value="prod"),
                            EnvVar(
                                name="DATABASE_URL",
                                value="postgresql://postgres@prod-postgres:5432/ust_prod",
                            ),
                            EnvVar(
                                name="FAULT_INJECTION_SEED",
                                value_from={
                                    "configMapKeyRef": {
                                        "name": "app-config",
                                        "key": "FAULT_INJECTION_SEED",
                                    }
                                },
                            ),
                        ],
                        ports=[{"containerPort": 8000, "name": "http", "protocol": "TCP"}],
                    )
                ],
                config_map_refs=["app-config"],
            ),
            "data-service": WorkloadSnapshot(
                name="data-service",
                namespace=namespace,
                component="data",
                labels={"app": "data-service", "app.kubernetes.io/component": "data"},
                replicas=1,
                containers=[
                    ContainerSnapshot(
                        name="data-service",
                        image_tag="localhost:5001/data-service:good",
                        image_digest="sha256:1ead9b47c5fe97d3551cc5001844061122dcc49388dcf77ebda51a3b6301f6b7",
                        pinned_image="localhost:5001/data-service@sha256:1ead9b47c5fe97d3551cc5001844061122dcc49388dcf77ebda51a3b6301f6b7",
                        resources=res_standard,
                        env=[
                            EnvVar(name="UNDERSTUDY_ROLE", value="prod"),
                            EnvVar(
                                name="DATABASE_URL",
                                value="postgresql://postgres@prod-postgres:5432/ust_prod",
                            ),
                            EnvVar(
                                name="FAULT_INJECTION_SEED",
                                value_from={
                                    "configMapKeyRef": {
                                        "name": "app-config",
                                        "key": "FAULT_INJECTION_SEED",
                                    }
                                },
                            ),
                        ],
                        ports=[{"containerPort": 8000, "name": "http", "protocol": "TCP"}],
                    )
                ],
                config_map_refs=["app-config"],
            ),
            "worker": WorkloadSnapshot(
                name="worker",
                namespace=namespace,
                component="worker",
                labels={"app": "worker", "app.kubernetes.io/component": "worker"},
                replicas=1,
                containers=[
                    ContainerSnapshot(
                        name="worker",
                        image_tag="localhost:5001/worker:good",
                        image_digest="sha256:2e2519600a03c9768b683b2850dd591bfae03e78c6f33cbe8b7e72c868bacee4",
                        pinned_image="localhost:5001/worker@sha256:2e2519600a03c9768b683b2850dd591bfae03e78c6f33cbe8b7e72c868bacee4",
                        resources=res_standard,
                        env=[
                            EnvVar(name="UNDERSTUDY_ROLE", value="prod"),
                            EnvVar(
                                name="DATABASE_URL",
                                value="postgresql://postgres@prod-postgres:5432/ust_prod",
                            ),
                            EnvVar(
                                name="FAULT_INJECTION_SEED",
                                value_from={
                                    "configMapKeyRef": {
                                        "name": "app-config",
                                        "key": "FAULT_INJECTION_SEED",
                                    }
                                },
                            ),
                        ],
                        ports=[{"containerPort": 8000, "name": "http", "protocol": "TCP"}],
                    )
                ],
                config_map_refs=["app-config"],
            ),
            "prod-postgres": WorkloadSnapshot(
                name="prod-postgres",
                namespace=namespace,
                component="database",
                labels={"app": "prod-postgres", "app.kubernetes.io/component": "database"},
                replicas=1,
                containers=[
                    ContainerSnapshot(
                        name="postgres",
                        image_tag="postgres:16-alpine",
                        image_digest="sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685",
                        pinned_image="docker.io/library/postgres@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685",
                        resources=res_postgres,
                        env=[
                            EnvVar(name="POSTGRES_USER", value="postgres"),
                            EnvVar(name="POSTGRES_DB", value="ust_prod"),
                        ],
                        ports=[{"containerPort": 5432, "name": "postgres", "protocol": "TCP"}],
                    )
                ],
                config_map_refs=[],
            ),
        }

        workloads: dict[str, WorkloadSnapshot] = {}
        for name, wl in all_workloads.items():
            if exclude_components and wl.component in exclude_components:
                continue
            workloads[name] = wl

        config_maps = {
            "app-config": {
                "ENVIRONMENT": "production",
                "LOG_LEVEL": "INFO",
                "FAULT_INJECTION_SEED": "1337",
            },
            "feature-flags": {
                "enable_recommendations": "true",
                "enable_fast_cache": "true",
                "enable_v2_catalogue": "false",
            },
        }

        return ClusterWorkloadSnapshot(
            namespace=namespace,
            workloads=workloads,
            config_maps=config_maps,
            captured_at=self.clock.now(),
        )


class FakeDatabaseCommandExecutor(DatabaseCommandExecutor):
    """Deterministic in-memory database command executor for unit tests."""

    def __init__(self) -> None:
        self.sql_history: list[tuple[str, list[str]]] = []
        self.pipeline_history: list[str] = []
        self.databases: set[str] = {"postgres", "snapshot_template"}
        self.items_count: dict[str, int] = {"snapshot_template": 207}
        self.database_comments: dict[str, str] = {}
        self.in_use_counter: int = 0
        self.fail_sql: str | None = None
        self.fail_pipeline: bool = False

    async def run_sql(
        self,
        sql_commands: Sequence[str],
        database: str = "postgres",
        timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> tuple[int, str, str]:
        _ = timeout
        self.sql_history.append((database, list(sql_commands)))
        if self.fail_sql:
            return 1, "", self.fail_sql

        out_lines: list[str] = []
        for cmd in sql_commands:
            normalized = cmd.strip()
            if "SELECT 1 FROM pg_database WHERE datname" in normalized:
                if "snapshot_template" in self.databases:
                    out_lines.append("1")
                else:
                    return 0, "", ""
            elif normalized.startswith("CREATE DATABASE"):
                if self.in_use_counter > 0:
                    self.in_use_counter -= 1
                    return (
                        1,
                        "",
                        'ERROR: source database "snapshot_template" '
                        "is being accessed by other users\n"
                        "DETAIL: There is 1 other session using the database.",
                    )
                parts = normalized.replace(";", "").split()
                db_name = parts[2]
                self.databases.add(db_name)
                self.items_count[db_name] = self.items_count.get("snapshot_template", 207)
                out_lines.append("CREATE DATABASE")
            elif normalized.startswith("DROP DATABASE"):
                tokens = [
                    p
                    for p in normalized.replace(";", "").split()
                    if p.lower() not in ("with", "(force)") and not p.lower().startswith("with")
                ]
                db_name = tokens[-1]
                self.databases.discard(db_name)
                self.items_count.pop(db_name, None)
                self.database_comments.pop(db_name, None)
                out_lines.append("DROP DATABASE")
            elif normalized.startswith("COMMENT ON DATABASE"):
                parts = normalized.split(" IS ", 1)
                db_name = parts[0].split()[-1]
                self.database_comments[db_name] = parts[1].rstrip(";").strip().strip("'")
                out_lines.append("COMMENT")
            elif "shobj_description" in normalized:
                for db in sorted(d for d in self.databases if d.startswith("twin_")):
                    out_lines.append(f"{db}|{self.database_comments.get(db, '')}")
            elif "SELECT datname FROM pg_database" in normalized:
                twins = sorted([d for d in self.databases if d.startswith("twin_")])
                out_lines.extend(twins)
            elif "SELECT count(*) FROM items" in normalized:
                count = self.items_count.get(database, 207)
                out_lines.append(str(count))
            elif "pg_terminate_backend" in normalized:
                out_lines.append("t")
            else:
                out_lines.append("ok")

        return 0, "\n".join(out_lines), ""

    async def run_pipeline(
        self,
        shell_script: str,
        timeout: float = DEFAULT_PIPELINE_TIMEOUT_SECONDS,
    ) -> tuple[int, str, str]:
        _ = timeout
        self.pipeline_history.append(shell_script)
        if self.fail_pipeline:
            return 1, "", "Simulated pipeline execution failure"
        self.databases.add("snapshot_template")
        self.items_count["snapshot_template"] = 207
        return 0, "Pipeline completed successfully", ""


class FakeSnapshotRefresher(SnapshotRefresher):
    """Deterministic in-memory snapshot template refresher."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = resolve_clock(clock)
        self.refreshed_at: datetime | None = None
        self.refresh_count: int = 0
        self._is_running = False

    @property
    def is_running(self) -> bool:
        """Return whether the refresher background task is running."""
        return self._is_running

    @is_running.setter
    def is_running(self, value: bool) -> None:
        self._is_running = value

    async def refresh_snapshot(self) -> datetime:
        now = self.clock.now()
        self.refreshed_at = now
        self.refresh_count += 1
        return now

    async def get_last_snapshot_time(self) -> datetime | None:
        return self.refreshed_at

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False


class FakeDatabaseCloner(DatabaseCloner):
    """Deterministic in-memory database cloner."""

    def __init__(
        self,
        clock: Clock | None = None,
        refresher: SnapshotRefresher | None = None,
        simulated_in_use_failures: int = 0,
    ) -> None:
        self.clock: Clock = resolve_clock(clock)
        self.refresher = refresher
        self.cloned_databases: dict[str, DatabaseCloneResult] = {}
        self.simulated_in_use_failures = simulated_in_use_failures
        self.items_count: dict[str, int] = {}
        # Databases whose creation stamp is missing, i.e. age unknown to garbage collection.
        self.unknown_age_databases: set[str] = set()

    async def clone_twin_database(
        self, incident_id: str, candidate_index: int
    ) -> DatabaseCloneResult:
        if self.simulated_in_use_failures > 0:
            self.simulated_in_use_failures -= 1
            raise FleetError(
                'ERROR: source database "snapshot_template" is being accessed by other users'
            )

        db_name = sanitize_database_name(incident_id, candidate_index)
        now = self.clock.now()
        forked_at = (
            await self.refresher.get_last_snapshot_time() if self.refresher is not None else None
        ) or now

        result = DatabaseCloneResult(
            database_name=db_name,
            incident_id=incident_id,
            candidate_index=candidate_index,
            forked_from_snapshot_at=forked_at,
            cloned_at=now,
        )
        self.cloned_databases[db_name] = result
        self.items_count[db_name] = 207
        return result

    async def drop_twin_database(self, database_name: str) -> None:
        self.cloned_databases.pop(database_name, None)
        self.items_count.pop(database_name, None)

    async def drop_all_incident_databases(self, incident_id: str) -> list[str]:
        clean = incident_id.replace("-", "_").strip()
        prefix = f"twin_{clean}_"
        matching = [name for name in self.cloned_databases if name.startswith(prefix)]
        for name in matching:
            await self.drop_twin_database(name)
        return matching

    async def list_twin_databases(self, incident_id: str | None = None) -> list[str]:
        if incident_id:
            clean = incident_id.replace("-", "_").strip()
            prefix = f"twin_{clean}_"
            return sorted([name for name in self.cloned_databases if name.startswith(prefix)])
        return sorted(self.cloned_databases.keys())

    async def list_twin_databases_with_age(self) -> list[TwinDatabaseInfo]:
        now = self.clock.now()
        infos: list[TwinDatabaseInfo] = []
        for name in sorted(self.cloned_databases):
            if name in self.unknown_age_databases:
                infos.append(TwinDatabaseInfo(name=name, age_seconds=None))
                continue
            cloned_at = self.cloned_databases[name].cloned_at
            infos.append(
                TwinDatabaseInfo(
                    name=name,
                    age_seconds=max(0.0, (now - cloned_at).total_seconds()),
                )
            )
        return infos

    async def get_item_count(self, database_name: str) -> int:
        return self.items_count.get(database_name, 207)
