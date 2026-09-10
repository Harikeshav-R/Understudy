"""Unit tests for Clock implementations."""

from datetime import UTC, datetime, timedelta

import pytest

from understudy.common.clock import Clock, FrozenClock, SystemClock


def test_system_clock_now() -> None:
    clock = SystemClock()
    assert isinstance(clock, Clock)
    before = datetime.now(UTC)
    now = clock.now()
    after = datetime.now(UTC)
    assert before <= now <= after
    assert now.tzinfo == UTC


@pytest.mark.asyncio
async def test_system_clock_sleep() -> None:
    clock = SystemClock()
    # tiny sleep
    await clock.sleep(0.001)


def test_frozen_clock_default_initialization() -> None:
    clock = FrozenClock()
    assert isinstance(clock, Clock)
    now1 = clock.now()
    now2 = clock.now()
    assert now1 == now2
    assert now1.tzinfo == UTC


def test_frozen_clock_explicit_initialization() -> None:
    naive_dt = datetime(2026, 1, 1, 12, 0, 0)
    clock = FrozenClock(naive_dt)
    assert clock.now() == naive_dt.replace(tzinfo=UTC)

    aware_dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    clock2 = FrozenClock(aware_dt)
    assert clock2.now() == aware_dt


def test_frozen_clock_set_time() -> None:
    clock = FrozenClock()
    dt1 = datetime(2026, 5, 1, 10, 0, 0)
    clock.set_time(dt1)
    assert clock.now() == dt1.replace(tzinfo=UTC)

    dt2 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
    clock.set_time(dt2)
    assert clock.now() == dt2


@pytest.mark.asyncio
async def test_frozen_clock_advance_and_sleep() -> None:
    dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    clock = FrozenClock(dt)
    clock.advance(30.5)
    assert clock.now() == dt + timedelta(seconds=30.5)

    await clock.sleep(10.0)
    assert clock.now() == dt + timedelta(seconds=40.5)
