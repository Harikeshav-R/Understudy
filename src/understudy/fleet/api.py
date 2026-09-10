"""Fleet component protocol interfaces."""

from typing import Protocol, runtime_checkable

from understudy.contracts.twin import TwinHandle


@runtime_checkable
class FleetController(Protocol):
    """Twin namespace and database lifecycle controller."""

    async def fork(self, incident_id: str, n: int) -> list[TwinHandle]:
        """Concurrently fork N isolated twin environments for an incident."""
        raise NotImplementedError

    async def teardown(self, twin_handle: TwinHandle) -> None:
        """Tear down a specific twin namespace and its cloned database."""
        raise NotImplementedError

    async def teardown_all(self, incident_id: str) -> None:
        """Idempotently tear down all twin environments associated with an incident."""
        raise NotImplementedError
