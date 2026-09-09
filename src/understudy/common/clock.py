"""Clock protocol and implementations for deterministic time handling."""

import asyncio
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Protocol for time retrieval and sleeping."""

    def now(self) -> datetime:
        """Return the current time in UTC."""
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        """Sleep for the specified number of seconds."""
        raise NotImplementedError


class SystemClock:
    """Real system wall-clock implementation."""

    def now(self) -> datetime:
        """Return the current real UTC time."""
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        """Asynchronously sleep using asyncio."""
        await asyncio.sleep(seconds)


class FrozenClock:
    """Deterministic, settable clock for unit tests and simulation."""

    def __init__(self, initial_time: datetime | None = None) -> None:
        if initial_time is not None:
            if initial_time.tzinfo is None:
                self._current_time = initial_time.replace(tzinfo=UTC)
            else:
                self._current_time = initial_time
        else:
            self._current_time = datetime.now(UTC)

    def now(self) -> datetime:
        """Return the frozen UTC time."""
        return self._current_time

    def set_time(self, new_time: datetime) -> None:
        """Set the frozen clock to a new time."""
        if new_time.tzinfo is None:
            self._current_time = new_time.replace(tzinfo=UTC)
        else:
            self._current_time = new_time

    def advance(self, seconds: float) -> None:
        """Advance the frozen clock by a specified duration in seconds."""
        from datetime import timedelta

        self._current_time += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        """Simulate sleep by advancing the frozen clock without wall-clock delay."""
        self.advance(seconds)
