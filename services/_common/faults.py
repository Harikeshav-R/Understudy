"""Fault injection engine and admin endpoints for Understudy demo services."""

import asyncio
import contextlib
import gc
import math
import os
import random
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from pydantic import BaseModel, Field

from services._common.role_guard import ensure_fault_injection_permitted
from services._common.settings import get_services_settings

FAULT_INJECTION_SEED_ENV = "FAULT_INJECTION_SEED"


class FaultKind(StrEnum):
    """Supported fault injection kinds per architecture §2.5."""

    LATENCY = "latency"
    ERROR_RATE = "error_rate"
    CPU_SPIN = "cpu_spin"
    MEMORY_LEAK = "memory_leak"
    POOL_EXHAUSTION = "pool_exhaustion"
    STALL = "stall"


class FaultRequest(BaseModel):
    """Specification for an injected fault."""

    kind: FaultKind
    magnitude: float = Field(
        ge=0,
        description=(
            "Magnitude of fault, units depend on kind: LATENCY is milliseconds, "
            "MEMORY_LEAK is megabytes, POOL_EXHAUSTION is a connection count, CPU_SPIN "
            "is milliseconds of burn time. ERROR_RATE is the odd one out: a value "
            "<= 1.0 is read as a fraction (0.5 = 50%), a value > 1.0 as a percentage "
            "(50.0 = 50%) -- so a caller who means '1%' and passes 1.0 gets 100% "
            "instead, since 1.0 lands on the fraction side of that boundary."
        ),
    )
    ttl_seconds: float = Field(gt=0, description="Time-to-live before recovery in seconds")


def _run_cpu_spin(duration_sec: float) -> None:
    """Synchronous CPU burning loop executed in a separate worker thread."""
    start = time.monotonic()
    x = 0.0001
    while time.monotonic() - start < duration_sec:
        x = math.sin(x) + math.cos(x)


class FaultManager:
    """Manages active faults, applies per-request mutations, and handles TTL recovery."""

    def __init__(self, pool: AsyncConnectionPool | None = None, seed: int | None = None) -> None:
        self.pool = pool
        self.active_fault: FaultRequest | None = None
        self.expires_at: float | None = None
        self._timer_task: asyncio.Task[None] | None = None
        self._leaked_buffers: list[bytearray] = []
        self._held_connections: list[Any] = []
        self._stall_event = asyncio.Event()
        self._stall_event.set()
        # Guards the active-fault-transition critical section (active_fault, expires_at,
        # _timer_task, _leaked_buffers, _held_connections) against two sources of
        # concurrency: overlapping admin apply_fault/clear_fault calls, and the
        # independent _ttl_watcher background task clearing a fault out from under a
        # fresh apply_fault. Without it, e.g. a TTL expiry racing a new apply_fault could
        # cancel the new fault's timer instead of the old one, or drop a connection
        # POOL_EXHAUSTION just appended while clear_fault is mid-iteration over the list.
        self._state_lock = asyncio.Lock()
        if seed is None:
            default_seed = get_services_settings().faults.injection_seed
            seed = int(os.getenv(FAULT_INJECTION_SEED_ENV, str(default_seed)))
        self._rng = random.Random(seed)

    def get_active_fault(self) -> FaultRequest | None:
        """Return the active fault specification if one is active."""
        return self.active_fault

    @property
    def is_stalled(self) -> bool:
        """Indicates if worker or request processing is currently stalled."""
        return not self._stall_event.is_set()

    async def wait_if_stalled(self) -> None:
        """Wait until stall condition is cleared if currently stalled."""
        await self._stall_event.wait()

    async def apply_fault(self, fault: FaultRequest) -> None:
        """Apply a new fault and schedule TTL expiration."""
        async with self._state_lock:
            await self._clear_fault_locked()

            self.active_fault = fault
            self.expires_at = time.time() + fault.ttl_seconds

            # Kind-specific initialization
            if fault.kind == FaultKind.MEMORY_LEAK:
                # Allocate magnitude in MB
                self._leaked_buffers.append(bytearray(int(fault.magnitude * 1024 * 1024)))
            elif fault.kind == FaultKind.POOL_EXHAUSTION and self.pool is not None:
                # Acquire connections to exhaust pool
                count = max(1, int(fault.magnitude))
                getconn_timeout = (
                    get_services_settings().faults.pool_exhaustion_getconn_timeout_seconds
                )
                for _ in range(count):
                    try:
                        conn = await self.pool.getconn(timeout=getconn_timeout)
                        self._held_connections.append(conn)
                    except PoolTimeout:
                        break
            elif fault.kind == FaultKind.STALL:
                self._stall_event.clear()

            # Schedule automatic TTL clearance
            self._timer_task = asyncio.create_task(self._ttl_watcher(fault.ttl_seconds))

    async def clear_fault(self) -> None:
        """Revert active faults and restore baseline state."""
        async with self._state_lock:
            await self._clear_fault_locked()

    async def _clear_fault_locked(self) -> None:
        """Revert active faults and restore baseline state. Caller must hold
        _state_lock; apply_fault calls this directly (already holding the lock) instead
        of the public clear_fault to avoid a self-deadlock on the non-reentrant Lock."""
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timer_task
        self._timer_task = None

        self.active_fault = None
        self.expires_at = None

        # Clean up leaked memory
        if self._leaked_buffers:
            self._leaked_buffers.clear()
            gc.collect()

        # Release exhausted pool connections
        if self._held_connections and self.pool is not None:
            for conn in self._held_connections:
                with contextlib.suppress(Exception):
                    await self.pool.putconn(conn)
            self._held_connections.clear()

        # Clear stall
        self._stall_event.set()

    async def _ttl_watcher(self, ttl_seconds: float) -> None:
        """Wait for TTL duration and automatically clear fault."""
        try:
            await asyncio.sleep(ttl_seconds)
            await self.clear_fault()
        except asyncio.CancelledError:
            pass

    async def pre_request_hook(self) -> None:
        """Execute per-request fault action before request handler runs."""
        fault = self.active_fault
        if not fault:
            return

        if fault.kind == FaultKind.LATENCY:
            await asyncio.sleep(fault.magnitude / 1000.0)

        elif fault.kind == FaultKind.ERROR_RATE:
            # magnitude is a fraction at <= 1.0, a percentage above it (see FaultRequest
            # .magnitude docstring) -- this boundary is intentional and load-bearing for
            # test_faults.py::test_fault_error_rate (1.0 and 100.0 both mean 100%), not a
            # bug to "fix" by changing the comparison.
            rate = fault.magnitude if fault.magnitude <= 1.0 else (fault.magnitude / 100.0)
            if self._rng.random() < rate:
                raise HTTPException(
                    status_code=500,
                    detail="Injected fault: simulated error rate",
                )

        elif fault.kind == FaultKind.CPU_SPIN:
            await asyncio.to_thread(_run_cpu_spin, fault.magnitude / 1000.0)

        elif fault.kind == FaultKind.STALL:
            # Same semantics as the worker: block until the fault clears (TTL or explicit
            # DELETE /admin/fault), ignoring magnitude. Previously this slept for a bounded
            # `magnitude` ms, identical to LATENCY, which made STALL indistinguishable from
            # LATENCY for HTTP services.
            await self.wait_if_stalled()


def setup_fault_routes(app: FastAPI, fault_manager: FaultManager) -> None:
    """Register /admin/fault control routes on the FastAPI application."""

    @app.post("/admin/fault", tags=["Fault Injection"])
    async def post_fault(fault: FaultRequest) -> dict[str, Any]:
        ensure_fault_injection_permitted()
        await fault_manager.apply_fault(fault)
        return {"status": "applied", "fault": fault.model_dump()}

    @app.delete("/admin/fault", tags=["Fault Injection"])
    async def delete_fault() -> dict[str, str]:
        ensure_fault_injection_permitted()
        await fault_manager.clear_fault()
        return {"status": "cleared"}

    @app.get("/admin/fault", tags=["Fault Injection"])
    async def get_fault() -> dict[str, Any]:
        return {
            "active": fault_manager.active_fault.model_dump()
            if fault_manager.active_fault
            else None,
            "expires_at": fault_manager.expires_at,
        }


def setup_fault_middleware(app: FastAPI, fault_manager: FaultManager) -> None:
    """Attach middleware to intercept application endpoints with active faults."""

    @app.middleware("http")
    async def fault_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        # Skip administrative and probe endpoints to prevent lockout
        if path.startswith("/admin/") or path in ("/healthz", "/readyz", "/metrics"):
            return await call_next(request)

        await fault_manager.pre_request_hook()
        return await call_next(request)
