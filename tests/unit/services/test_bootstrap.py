"""Unit tests for the shared FastAPI bootstrap and db-pool lifespan helpers."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from services._common.bootstrap import create_service_app, db_pool_lifespan
from services._common.faults import FaultManager


@asynccontextmanager
async def _noop_lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    yield


def test_create_service_app_wires_metrics_and_fault_routes() -> None:
    fault_manager = FaultManager()
    app = create_service_app("test-service", fault_manager, _noop_lifespan)

    assert app.title == "test-service"
    route_paths = {getattr(route, "path", None) for route in app.routes}
    assert "/metrics" in route_paths
    assert "/admin/fault" in route_paths


@pytest.mark.asyncio
async def test_create_service_app_records_requests() -> None:
    """The fault middleware and metrics middleware both attach; a request through the
    app should be visible on /metrics."""
    fault_manager = FaultManager()
    app = create_service_app("test-service", fault_manager, _noop_lifespan)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "true"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/ping")
        assert resp.status_code == 200
        metrics = (await client.get("/metrics")).text
        assert "test-service" in metrics


@pytest.mark.asyncio
async def test_db_pool_lifespan_no_database_url() -> None:
    fault_manager = FaultManager()
    async with db_pool_lifespan(None, fault_manager) as pool:
        assert pool is None
    assert fault_manager.pool is None


@pytest.mark.asyncio
async def test_db_pool_lifespan_opens_assigns_and_closes() -> None:
    fault_manager = FaultManager()
    mock_pool = AsyncMock()
    on_open = AsyncMock()

    with patch("services._common.bootstrap.create_pool", return_value=mock_pool):
        async with db_pool_lifespan(
            "postgresql://fake:5432/db", fault_manager, on_open=on_open
        ) as pool:
            assert pool is mock_pool
            assert mock_pool.open.await_count == 1
            assert fault_manager.pool is mock_pool
            on_open.assert_awaited_once_with(mock_pool)

    assert mock_pool.close.await_count == 1


@pytest.mark.asyncio
async def test_db_pool_lifespan_on_open_failure_is_logged_not_raised() -> None:
    """A failed schema init must not prevent the app from starting -- readiness probes
    are what's supposed to catch this, matching every service's prior behavior."""
    fault_manager = FaultManager()
    mock_pool = AsyncMock()
    on_open = AsyncMock(side_effect=psycopg.OperationalError("init failed"))

    with patch("services._common.bootstrap.create_pool", return_value=mock_pool):
        async with db_pool_lifespan(
            "postgresql://fake:5432/db", fault_manager, on_open=on_open
        ) as pool:
            assert pool is mock_pool

    assert mock_pool.close.await_count == 1
