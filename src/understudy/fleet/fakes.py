"""Deterministic fake fleet controller implementation."""

from understudy.common.clock import Clock, resolve_clock
from understudy.contracts.twin import TwinHandle
from understudy.fleet.api import FleetController


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
