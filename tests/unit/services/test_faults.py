"""Unit tests for fault injection engine, all 6 fault kinds, TTL recovery, and endpoints."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from psycopg_pool import PoolTimeout

from services._common.faults import (
    FaultKind,
    FaultManager,
    FaultRequest,
    _run_cpu_spin,
    setup_fault_middleware,
    setup_fault_routes,
)


@pytest.mark.asyncio
async def test_fault_latency() -> None:
    manager = FaultManager()
    await manager.apply_fault(
        FaultRequest(kind=FaultKind.LATENCY, magnitude=50, ttl_seconds=10),
    )
    assert manager.active_fault is not None

    start = asyncio.get_event_loop().time()
    await manager.pre_request_hook()
    duration = asyncio.get_event_loop().time() - start
    assert duration >= 0.04

    # Test magnitude 0
    await manager.apply_fault(
        FaultRequest(kind=FaultKind.LATENCY, magnitude=0, ttl_seconds=10),
    )
    await manager.pre_request_hook()
    await manager.clear_fault()


@pytest.mark.asyncio
async def test_fault_error_rate() -> None:
    manager = FaultManager()
    # 100% error rate (both float <= 1.0 and integer percentage > 1.0)
    await manager.apply_fault(
        FaultRequest(kind=FaultKind.ERROR_RATE, magnitude=1.0, ttl_seconds=10),
    )
    with pytest.raises(HTTPException) as exc_info:
        await manager.pre_request_hook()
    assert exc_info.value.status_code == 500

    await manager.apply_fault(
        FaultRequest(kind=FaultKind.ERROR_RATE, magnitude=100.0, ttl_seconds=10),
    )
    with pytest.raises(HTTPException) as exc_info:
        await manager.pre_request_hook()
    assert exc_info.value.status_code == 500

    # 0% error rate does not raise
    await manager.apply_fault(
        FaultRequest(kind=FaultKind.ERROR_RATE, magnitude=0.0, ttl_seconds=10),
    )
    await manager.pre_request_hook()

    await manager.clear_fault()
    # When no fault is active, pre_request_hook is a no-op
    await manager.pre_request_hook()


@pytest.mark.asyncio
async def test_fault_error_rate_seeded_determinism() -> None:
    """Same seed must produce the same accept/reject sequence (CLAUDE.md §5.7)."""

    async def run(seed: int) -> list[bool]:
        manager = FaultManager(seed=seed)
        await manager.apply_fault(
            FaultRequest(kind=FaultKind.ERROR_RATE, magnitude=0.5, ttl_seconds=10),
        )
        outcomes = []
        for _ in range(30):
            try:
                await manager.pre_request_hook()
                outcomes.append(False)
            except HTTPException:
                outcomes.append(True)
        return outcomes

    first = await run(42)
    second = await run(42)
    assert first == second
    assert any(first)
    assert not all(first)


@pytest.mark.asyncio
async def test_fault_error_rate_default_seed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing without an explicit seed falls back to FAULT_INJECTION_SEED."""
    monkeypatch.setenv("FAULT_INJECTION_SEED", "7")

    async def run() -> list[bool]:
        manager = FaultManager()
        await manager.apply_fault(
            FaultRequest(kind=FaultKind.ERROR_RATE, magnitude=0.5, ttl_seconds=10),
        )
        outcomes = []
        for _ in range(30):
            try:
                await manager.pre_request_hook()
                outcomes.append(False)
            except HTTPException:
                outcomes.append(True)
        return outcomes

    first = await run()
    second = await run()
    assert first == second


@pytest.mark.asyncio
async def test_fault_cpu_spin() -> None:
    manager = FaultManager()
    await manager.apply_fault(
        FaultRequest(kind=FaultKind.CPU_SPIN, magnitude=20, ttl_seconds=10),
    )
    await manager.pre_request_hook()

    # Test magnitude 0
    await manager.apply_fault(
        FaultRequest(kind=FaultKind.CPU_SPIN, magnitude=0, ttl_seconds=10),
    )
    await manager.pre_request_hook()
    await manager.clear_fault()


def test_run_cpu_spin_synchronous() -> None:
    _run_cpu_spin(0.01)


@pytest.mark.asyncio
async def test_fault_memory_leak() -> None:
    manager = FaultManager()
    await manager.apply_fault(
        FaultRequest(kind=FaultKind.MEMORY_LEAK, magnitude=1.0, ttl_seconds=10),
    )
    assert len(manager._leaked_buffers) == 1
    assert len(manager._leaked_buffers[0]) == 1024 * 1024
    await manager.pre_request_hook()

    await manager.clear_fault()
    assert len(manager._leaked_buffers) == 0


@pytest.mark.asyncio
async def test_fault_pool_exhaustion() -> None:
    mock_conn = MagicMock()
    mock_pool = AsyncMock()
    mock_pool.getconn.return_value = mock_conn

    manager = FaultManager(pool=mock_pool)
    await manager.apply_fault(
        FaultRequest(kind=FaultKind.POOL_EXHAUSTION, magnitude=3, ttl_seconds=10),
    )
    assert len(manager._held_connections) == 3
    assert mock_pool.getconn.await_count == 3

    await manager.clear_fault()
    assert len(manager._held_connections) == 0
    assert mock_pool.putconn.await_count == 3


@pytest.mark.asyncio
async def test_fault_pool_exhaustion_errors_handled() -> None:
    mock_pool = AsyncMock()
    mock_pool.getconn.side_effect = PoolTimeout("exhausted")
    mock_pool.putconn.side_effect = RuntimeError("broken")

    manager = FaultManager(pool=mock_pool)
    # getconn failure breaks gracefully
    await manager.apply_fault(
        FaultRequest(kind=FaultKind.POOL_EXHAUSTION, magnitude=2, ttl_seconds=10),
    )
    assert len(manager._held_connections) == 0

    # Add a mock conn manually to test putconn exception handling in clear_fault
    manager._held_connections.append(MagicMock())
    await manager.clear_fault()
    assert len(manager._held_connections) == 0


@pytest.mark.asyncio
async def test_fault_stall() -> None:
    """STALL blocks pre_request_hook indefinitely (like the worker), ignoring magnitude,
    until the fault is cleared -- it must not merely sleep for `magnitude` ms like LATENCY."""
    manager = FaultManager()
    await manager.apply_fault(
        FaultRequest(kind=FaultKind.STALL, magnitude=20, ttl_seconds=10),
    )
    assert manager.is_stalled is True

    task = asyncio.create_task(manager.pre_request_hook())
    await asyncio.sleep(0.05)
    assert not task.done()

    await manager.clear_fault()
    await asyncio.wait_for(task, timeout=1.0)
    assert manager.is_stalled is False


@pytest.mark.asyncio
async def test_fault_ttl_auto_recovery() -> None:
    manager = FaultManager()
    await manager.apply_fault(
        FaultRequest(kind=FaultKind.STALL, magnitude=10, ttl_seconds=0.05),
    )
    assert manager.get_active_fault() is not None
    await asyncio.sleep(0.08)
    assert manager.get_active_fault() is None


@pytest.mark.asyncio
async def test_fault_routes_and_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERSTUDY_ROLE", "twin")
    manager = FaultManager()
    app = FastAPI()
    setup_fault_middleware(app, manager)
    setup_fault_routes(app, manager)

    @app.get("/target")
    async def target() -> dict[str, str]:
        return {"status": "ok"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Initially no fault
        get_res = await client.get("/admin/fault")
        assert get_res.status_code == 200
        assert get_res.json()["active"] is None

        # 2. Apply fault
        post_res = await client.post(
            "/admin/fault",
            json={"kind": "latency", "magnitude": 30, "ttl_seconds": 10},
        )
        assert post_res.status_code == 200
        assert post_res.json()["status"] == "applied"

        # 3. Call target - should succeed but delayed
        target_res = await client.get("/target")
        assert target_res.status_code == 200

        # 4. Check active fault
        get_res2 = await client.get("/admin/fault")
        assert get_res2.json()["active"]["kind"] == "latency"

        # 5. Clear fault
        del_res = await client.delete("/admin/fault")
        assert del_res.status_code == 200
        assert del_res.json()["status"] == "cleared"


@pytest.mark.asyncio
async def test_fault_routes_prod_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERSTUDY_ROLE", "prod")
    monkeypatch.setenv("UNDERSTUDY_FAULT_INJECTION_ENABLED", "false")
    manager = FaultManager()
    app = FastAPI()
    setup_fault_routes(app, manager)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        post_res = await client.post(
            "/admin/fault",
            json={"kind": "latency", "magnitude": 50, "ttl_seconds": 10},
        )
        assert post_res.status_code == 403

        del_res = await client.delete("/admin/fault")
        assert del_res.status_code == 403
