"""Mirror component protocol interfaces."""

from typing import Protocol, runtime_checkable

from understudy.contracts.twin import MirrorStats, TwinHandle


@runtime_checkable
class MirrorRegistry(Protocol):
    """Registration and statistics retrieval for mirrored traffic destinations."""

    async def register_twin(self, twin_handle: TwinHandle) -> None:
        """Register an active twin to begin receiving mirrored production traffic."""
        raise NotImplementedError

    async def unregister_twin(self, twin_id: str) -> None:
        """Unregister a twin from mirrored traffic fan-out."""
        raise NotImplementedError

    async def get_stats(self, twin_id: str) -> MirrorStats:
        """Fetch delivery and drop metrics for a registered twin."""
        raise NotImplementedError
