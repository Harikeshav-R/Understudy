"""Unit tests for /healthz and /readyz endpoints."""

import httpx
import pytest
from fastapi import FastAPI

from services._common.health import setup_health_routes


@pytest.mark.asyncio
async def test_healthz_and_readyz_without_checks() -> None:
    app = FastAPI()
    setup_health_routes(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        resp_ready = await client.get("/readyz")
        assert resp_ready.status_code == 200
        assert resp_ready.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_readyz_all_passing() -> None:
    app = FastAPI()

    async def check_one() -> bool:
        return True

    async def check_two() -> bool:
        return True

    setup_health_routes(app, [check_one, check_two])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_readyz_with_failure_and_exception() -> None:
    app = FastAPI()

    async def check_pass() -> bool:
        return True

    async def check_fail() -> bool:
        return False

    async def check_raise() -> bool:
        raise ConnectionResetError("connection lost")

    setup_health_routes(app, [check_pass, check_fail, check_raise])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/readyz")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "unready"
        assert "failed" in data["failures"]
        assert any("connection lost" in f for f in data["failures"])
