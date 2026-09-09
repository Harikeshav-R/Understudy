"""Deterministic fake shadow loop implementation."""

from understudy.shadow.api import ShadowLoop


class FakeShadowLoop(ShadowLoop):
    """Deterministic in-memory shadow loop."""

    def __init__(self) -> None:
        self.cycle_count = 0

    async def run_cycle(self) -> None:
        """Increment cycle count."""
        self.cycle_count += 1
