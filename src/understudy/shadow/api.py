"""Shadow loop component protocol interfaces."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class ShadowLoop(Protocol):
    """Speculative injection and validation loop using idle capacity."""

    async def run_cycle(self) -> None:
        """Run one speculative hypothesis generation and rehearsal cycle."""
        raise NotImplementedError
