"""Unit tests for TrafficObserver querying Prometheus metrics."""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from understudy.common.errors import GraphError
from understudy.graph.traffic import TrafficObserver


@pytest.mark.asyncio
async def test_traffic_observer_success() -> None:
    """Validate parsing of Prometheus http_requests_total series into ObservedTraffic."""
    mock_payload = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {
                        "__name__": "http_requests_total",
                        "service": "edge-gateway",
                        "endpoint": "/api/items",
                        "namespace": "ust-prod",
                    },
                    "value": [1789312000, "100.0"],
                },
                {
                    "metric": {
                        "__name__": "http_requests_total",
                        "service": "auth-service",
                        "endpoint": "/validate",
                        "namespace": "ust-prod",
                    },
                    "value": [1789312000, "80.0"],
                },
                {
                    "metric": {
                        "__name__": "http_requests_total",
                        "service": "data-service",
                        "endpoint": "/items",
                        "namespace": "ust-prod",
                    },
                    "value": [1789312000, "60.0"],
                },
                {
                    "metric": {
                        "__name__": "http_requests_total",
                        "service": "worker",
                        "namespace": "ust-prod",
                    },
                    "value": [1789312000, "20.0"],
                },
                {
                    "metric": {
                        "__name__": "http_requests_total",
                        "service": "custom-service",
                        "caller": "external-client",
                        "namespace": "ust-prod",
                    },
                    "value": [1789312000, "10.0"],
                },
                # Edge cases: non-dict item or missing service
                "not-a-dict",
                {"metric": "not-a-dict"},
                {"metric": {"service": None}},
                {"metric": {"service": "no-value"}, "value": "invalid"},
            ],
        },
    }

    mock_resp = Mock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_payload
    mock_resp.raise_for_status = Mock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = mock_resp
    mock_client.is_closed = False

    observer = TrafficObserver(base_url="http://prometheus.test:9090", client=mock_client)
    traffic = await observer.observe(namespace="ust-prod")

    assert "edge-gateway" in traffic.services
    assert "auth-service" in traffic.services
    assert "data-service" in traffic.services
    assert "worker" in traffic.services
    assert "custom-service" in traffic.services

    # Total requests
    assert traffic.total_requests == pytest.approx(270.0)

    # Inferred and explicit edges
    assert ("edge-gateway", "auth-service") in traffic.edges
    assert ("edge-gateway", "data-service") in traffic.edges
    assert ("worker", "data-service") in traffic.edges
    assert ("external-client", "custom-service") in traffic.edges


@pytest.mark.asyncio
async def test_traffic_observer_http_failure() -> None:
    """Network failure while querying Prometheus raises GraphError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")
    mock_client.is_closed = False

    observer = TrafficObserver(base_url="http://prometheus.test:9090", client=mock_client)
    with pytest.raises(GraphError, match="Failed to query Prometheus traffic metrics"):
        await observer.observe(namespace="ust-prod")


@pytest.mark.asyncio
async def test_traffic_observer_error_status() -> None:
    """Prometheus returning status != success raises GraphError."""
    mock_resp = Mock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "error", "error": "timeout executing PromQL"}
    mock_resp.raise_for_status = Mock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = mock_resp
    mock_client.is_closed = False

    observer = TrafficObserver(base_url="http://prometheus.test:9090", client=mock_client)
    with pytest.raises(GraphError, match="timeout executing PromQL"):
        await observer.observe(namespace="ust-prod")


@pytest.mark.asyncio
async def test_traffic_observer_client_lifecycle() -> None:
    """Verify initialization, reuse, and closing of owned httpx.AsyncClient."""
    observer = TrafficObserver(base_url="http://prometheus.test:9090")
    client1 = await observer._get_client()
    assert client1 is not None
    # Reuse owned client
    client2 = await observer._get_client()
    assert client2 is client1
    await observer.close()
    assert client1.is_closed
    # Calling close again when closed is a safe no-op
    await observer.close()


@pytest.mark.asyncio
async def test_traffic_observer_non_list_results_and_worker_only() -> None:
    """Non-list result in data and worker without data-service are handled gracefully."""
    mock_payload = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": "not-a-list",
        },
    }
    mock_resp = Mock(spec=httpx.Response, status_code=200, json=Mock(return_value=mock_payload))
    mock_client = AsyncMock(spec=httpx.AsyncClient, is_closed=False)
    mock_client.get.return_value = mock_resp

    observer = TrafficObserver(client=mock_client)
    traffic = await observer.observe()
    assert traffic.services == set()
    assert traffic.total_requests == 0.0

    # Now test worker only without data-service
    mock_payload_worker = {
        "status": "success",
        "data": {
            "result": [
                {
                    "metric": {"service": "worker"},
                    "value": [1000, "invalid-float"],
                }
            ],
        },
    }
    mock_resp_w = Mock(
        spec=httpx.Response, status_code=200, json=Mock(return_value=mock_payload_worker)
    )
    mock_client_w = AsyncMock(spec=httpx.AsyncClient, is_closed=False)
    mock_client_w.get.return_value = mock_resp_w

    observer_w = TrafficObserver(client=mock_client_w)
    traffic_w = await observer_w.observe()
    assert "worker" in traffic_w.services
    assert traffic_w.edges == set()
    assert traffic_w.request_counts["worker"] == 0.0
