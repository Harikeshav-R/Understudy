"""Deterministic fake fleet controller and workload reader implementations."""

from understudy.common.clock import Clock, resolve_clock
from understudy.contracts.twin import TwinHandle
from understudy.fleet.api import FleetController, WorkloadReader
from understudy.fleet.models import (
    ClusterWorkloadSnapshot,
    ContainerSnapshot,
    EnvVar,
    ResourceSpec,
    WorkloadSnapshot,
)


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
