"""Unit tests for Prometheus metrics collection and /metrics endpoint."""

import httpx
import pytest
from fastapi import FastAPI

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
