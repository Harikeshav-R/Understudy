"""Unit tests for Prometheus metrics collection and /metrics endpoint."""

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import CollectorRegistry

from services._common.metrics import setup_metrics


@pytest.mark.asyncio
async def test_metrics_collection_and_endpoint() -> None:
    app = FastAPI()
    setup_metrics(app, "test-service")

    @app.get("/hello")
    async def hello() -> dict[str, str]:
        return {"msg": "world"}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("simulated error")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Normal request
        resp = await client.get("/hello")
        assert resp.status_code == 200

        # 2. Error request
        with pytest.raises(RuntimeError):
            await client.get("/boom")

        # 3. Scrape /metrics
        metrics_resp = await client.get("/metrics")
        assert metrics_resp.status_code == 200
        text = metrics_resp.text
        assert "http_requests_total" in text
        assert "test-service" in text
        assert "understudy_service_info" in text


@pytest.mark.asyncio
async def test_metrics_registries_are_isolated_per_service() -> None:
    """Two setup_metrics calls (as happen once per service's app.py at import time, plus
    once more per test file that imports one) must not share state: each gets its own
    CollectorRegistry, so neither service's request counts nor its understudy_service_info
    leak into the other's /metrics output."""
    app_a = FastAPI()
    registry_a = CollectorRegistry()
    setup_metrics(app_a, "service-a", registry=registry_a)

    app_b = FastAPI()
    registry_b = CollectorRegistry()
    setup_metrics(app_b, "service-b", registry=registry_b)

    @app_a.get("/ping")
    async def ping_a() -> dict[str, str]:
        return {"ok": "a"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_a), base_url="http://test"
    ) as client_a:
        resp = await client_a.get("/ping")
        assert resp.status_code == 200

        metrics_a = (await client_a.get("/metrics")).text
        assert "service-a" in metrics_a
        assert "service-b" not in metrics_a

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_b), base_url="http://test"
    ) as client_b:
        metrics_b = (await client_b.get("/metrics")).text
        assert "service-b" in metrics_b
        assert "service-a" not in metrics_b
        # service-b never took a request, so no http_requests_total *sample* line should
        # exist (prometheus_client always emits the metric family's HELP/TYPE header even
        assert "http_requests_total{" not in metrics_b


def test_setup_metrics_returns_registry_and_sets_state() -> None:
    """setup_metrics returns the active CollectorRegistry and attaches it to app.state."""
    app = FastAPI()
    custom_reg = CollectorRegistry()
    returned_reg = setup_metrics(app, "my-service", registry=custom_reg)
    assert returned_reg is custom_reg
    assert app.state.metrics_registry is custom_reg

    app2 = FastAPI()
    default_reg = setup_metrics(app2, "my-service-2")
    assert isinstance(default_reg, CollectorRegistry)
    assert app2.state.metrics_registry is default_reg
