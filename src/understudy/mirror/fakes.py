"""Deterministic fake mirror registry implementation."""

from understudy.contracts.twin import MirrorStats, TwinHandle
from understudy.mirror.api import MirrorRegistry


class FakeMirrorRegistry(MirrorRegistry):
    """Deterministic in-memory mirror registry."""

    def __init__(self, drop_rate: float = 0.0) -> None:
        self.drop_rate = drop_rate
        self._registered: dict[str, MirrorStats] = {}

    async def register_twin(self, twin_handle: TwinHandle) -> None:
        """Register twin in registry."""
        delivered = 100
        dropped = int(delivered * self.drop_rate)
        self._registered[twin_handle.twin_id] = MirrorStats(
            twin_id=twin_handle.twin_id,
            delivered=delivered,
            dropped=dropped,
        )

    async def unregister_twin(self, twin_id: str) -> None:
        """Unregister twin from registry."""
        self._registered.pop(twin_id, None)

    async def get_stats(self, twin_id: str) -> MirrorStats:
        """Retrieve mirror stats for twin."""
        if twin_id in self._registered:
            return self._registered[twin_id]
        return MirrorStats(twin_id=twin_id, delivered=0, dropped=0)
