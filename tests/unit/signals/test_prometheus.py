"""Unit tests for PrometheusClient and PrometheusLokiAdapter."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from understudy.common.clock import FrozenClock
from understudy.common.errors import ObservabilityError
from understudy.contracts.incident import ErrorSignature, MetricWindow
from understudy.signals.api import ObservabilityAdapter
from understudy.signals.loki import LokiClient
from understudy.signals.prometheus import (
    PrometheusClient,
    PrometheusLokiAdapter,
    _extract_scalar_or_vector_value,
)

FIXED_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def test_extract_scalar_or_vector_value() -> None:
    # None or empty
    assert _extract_scalar_or_vector_value({}) is None
    assert _extract_scalar_or_vector_value({"data": {}}) is None
    assert _extract_scalar_or_vector_value({"data": {"result": []}}) is None

    # Scalar type
    assert (
        _extract_scalar_or_vector_value(
            {"data": {"resultType": "scalar", "result": [1789300000, "42.5"]}}
        )
        == 42.5
    )
    assert (
        _extract_scalar_or_vector_value(
            {"data": {"resultType": "scalar", "result": [1789300000, "invalid"]}}
        )
        is None
    )

    # Vector type
    assert (
        _extract_scalar_or_vector_value(
            {
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [1789300000, "100"]}],
                }
            }
        )
        == 100.0
    )
    assert (
        _extract_scalar_or_vector_value(
            {
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [1789300000, "invalid"]}],
                }
            }
        )
        is None
    )
    # Other result types (e.g. matrix with data)
    assert (
        _extract_scalar_or_vector_value({"data": {"resultType": "matrix", "result": [1, 2]}})
        is None
    )

    # Vector with non-dict first item
    assert (
        _extract_scalar_or_vector_value(
            {"data": {"resultType": "vector", "result": ["not_a_dict"]}}
        )
        is None
    )

    # Vector with empty value pair or empty result
    assert (
        _extract_scalar_or_vector_value(
            {"data": {"resultType": "vector", "result": [{"value": []}]}}
        )
        is None
    )
    assert _extract_scalar_or_vector_value({"data": {"resultType": "vector", "result": []}}) is None
    assert (
        _extract_scalar_or_vector_value(
            {"data": {"resultType": "vector", "result": [{"value": None}]}}
        )
        is None
    )


@pytest.mark.asyncio
async def test_prometheus_client_query_success_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "error_expr" in str(request.url):
            return httpx.Response(200, json={"status": "error", "error": "Bad PromQL syntax"})
        if "500_expr" in str(request.url):
            return httpx.Response(500, text="Internal Prometheus Error")
        if "non_dict" in str(request.url):
            return httpx.Response(200, json=["unexpected"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "vector", "result": [{"value": [1789300000, "1"]}]},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PrometheusClient(base_url="http://localhost:9090", client=http_client)

        # Success with time
        res = await client.query("up", time=FIXED_NOW)
        assert res["status"] == "success"

        # Prometheus status error
        with pytest.raises(ObservabilityError, match="Prometheus query error: Bad PromQL syntax"):
            await client.query("error_expr")

        # HTTP error
        with pytest.raises(ObservabilityError, match="Prometheus query failed"):
            await client.query("500_expr")

        # Non-dict error
        with pytest.raises(ObservabilityError, match="Unexpected Prometheus response type"):
            await client.query("non_dict")


@pytest.mark.asyncio
async def test_prometheus_client_query_range_success_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "error_expr" in str(request.url):
            return httpx.Response(200, json={"status": "error", "error": "Range syntax error"})
        if "500_expr" in str(request.url):
            return httpx.Response(500, text="Server Error")
        if "non_dict" in str(request.url):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {
                                "__name__": "http_requests_total",
                                "service": "data-service",
                            },
                            "values": [
                                [1789300000, "10"],
                                [1789300015, "15"],
                                ["invalid_ts", "val"],
                            ],
                        },
                        "not_a_dict_series",
                    ],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PrometheusClient(base_url="http://localhost:9090", client=http_client)

        # Success
        res = await client.query_range(
            "http_requests_total",
            start=FIXED_NOW - timedelta(minutes=5),
            end=FIXED_NOW,
        )
        assert res["status"] == "success"

        # Range query errors
        with pytest.raises(ObservabilityError, match="Range syntax error"):
            await client.query_range("error_expr", start=FIXED_NOW, end=FIXED_NOW)

        with pytest.raises(ObservabilityError, match="Prometheus range query failed"):
            await client.query_range("500_expr", start=FIXED_NOW, end=FIXED_NOW)

        with pytest.raises(ObservabilityError, match="Unexpected Prometheus response type"):
            await client.query_range("non_dict", start=FIXED_NOW, end=FIXED_NOW)


@pytest.mark.asyncio
async def test_prometheus_client_get_metric_window_full() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        if "status_code" in q:
            # 5 errors
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "vector", "result": [{"value": [1789300000, "5"]}]},
                },
            )
        if "histogram_quantile" in q:
            # 0.125 seconds = 125 ms
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "vector", "result": [{"value": [1789300000, "0.125"]}]},
                },
            )
        if "/api/v1/query_range" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [
                            {
                                "metric": {"__name__": "http_requests_total", "code": "200"},
                                "values": [[1789300000, "100"], "not_a_list"],
                            },
                            "not_a_dict_series",
                            {
                                "metric": {"__name__": "http_requests_total"},
                                "values": "not_a_list",
                            },
                        ],
                    },
                },
            )
        # Total requests: 500
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "vector", "result": [{"value": [1789300000, "500"]}]},
            },
        )

    clock = FrozenClock(initial_time=FIXED_NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PrometheusClient(
            base_url="http://localhost:9090",
            client=http_client,
            clock=clock,
        )

        # Test start_time > end_time swap
        window = await client.get_metric_window(
            service="data-service",
            since=FIXED_NOW + timedelta(minutes=10),
            namespace="ust-prod",
            until=FIXED_NOW,
        )

        assert window.service == "data-service"
        assert window.request_count == 500
        assert window.error_rate == 0.01  # 5 / 500
        assert window.p99_latency_ms == 125.0
        assert len(window.series) == 2
        assert window.series[0].metric_name == "http_requests_total"


@pytest.mark.asyncio
async def test_prometheus_client_get_metric_window_fallbacks_and_zero_traffic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/query_range" in request.url.path:
            return httpx.Response(
                200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
            )
        q = request.url.params.get("query", "")
        # Primary queries with service="..." return empty; app="..." returns value
        if 'service="data-service"' in q:
            return httpx.Response(
                200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
            )
        if 'app="data-service"' in q and "status_code" not in q and "histogram" not in q:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "vector", "result": [{"value": [1789300000, "0"]}]},
                },
            )
        # Zero requests
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
        )

    clock = FrozenClock(initial_time=FIXED_NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PrometheusClient(base_url="http://localhost:9090", client=http_client, clock=clock)

        window = await client.get_metric_window(
            service="data-service",
            since=FIXED_NOW - timedelta(minutes=5),
            namespace="ust-prod",
        )

        assert window.request_count == 0
        assert window.error_rate == 0.0
        assert window.p99_latency_ms is None
        assert window.series == []


@pytest.mark.asyncio
async def test_prometheus_client_service_health() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"app": "data-service"}, "value": [1789300000, "1"]},
                        {"metric": {"service": "worker"}, "value": [1789300000, "0"]},
                        {"metric": {"app": "auth-service"}, "value": []},
                        {
                            "metric": {"instance": "auth-service.ust-prod:8000"},
                            "value": [1789300000, "1"],
                        },
                        {"metric": {"pod": "edge-gateway-xyz"}, "value": [1789300000, "1"]},
                        "malformed_item",
                    ],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PrometheusClient(base_url="http://localhost:9090", client=http_client)

        assert await client.check_service_health("ust-prod", "data-service") is True
        assert await client.check_service_health("ust-prod", "auth-service") is True
        assert await client.check_service_health("ust-prod", "edge-gateway") is True
        assert await client.check_service_health("ust-prod", "worker") is False
        assert await client.check_service_health("ust-prod", "missing-service") is False

    # Non-list result and invalid float value
    def malformed_health_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {"service": "data-service"},
                            "value": [1789300000, "not_a_float"],
                        },
                    ],
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(malformed_health_handler)
    ) as http_client:
        client_mal = PrometheusClient(base_url="http://localhost:9090", client=http_client)
        assert await client_mal.check_service_health("ust-prod", "data-service") is False

    # Result is not a list
    def non_list_health_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": "not_list"}}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(non_list_health_handler)
    ) as http_client:
        client_non_list = PrometheusClient(base_url="http://localhost:9090", client=http_client)
        assert await client_non_list.check_service_health("ust-prod", "data-service") is False

    # Connection error returns False
    def err_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(err_handler)) as http_client:
        client_err = PrometheusClient(base_url="http://localhost:9090", client=http_client)
        assert await client_err.check_service_health("ust-prod", "data-service") is False


@pytest.mark.asyncio
async def test_prometheus_client_metric_window_with_all_fallbacks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        # Primary queries with service="..." return empty; app="..." returns value
        if 'service="data-service"' in q:
            return httpx.Response(
                200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
            )
        if 'app="data-service"' in q and "status_code" in q:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "vector", "result": [{"value": [1789300000, "10"]}]},
                },
            )
        if 'app="data-service"' in q and "histogram" in q:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "vector", "result": [{"value": [1789300000, "0.25"]}]},
                },
            )
        if "/query_range" in request.url.path:
            # Fallback range returns matrix with some invalid points
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [
                            {
                                "metric": {
                                    "__name__": "http_requests_total",
                                    "service": "data-service",
                                },
                                "values": [
                                    [1789300000, "100"],
                                    ["invalid_ts", "50"],
                                    [1789300010, "invalid_val"],
                                ],
                            }
                        ],
                    },
                },
            )
        # Total requests fallback
        if 'app="data-service"' in q:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "vector", "result": [{"value": [1789300000, "200"]}]},
                },
            )
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
        )

    clock = FrozenClock(initial_time=FIXED_NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PrometheusClient(base_url="http://localhost:9090", client=http_client, clock=clock)

        window = await client.get_metric_window(
            service="data-service",
            since=FIXED_NOW - timedelta(minutes=5),
            namespace="ust-prod",
        )

        assert window.request_count == 200
        assert window.error_rate == 0.05  # 10 / 200
        assert window.p99_latency_ms == 250.0  # 0.25 * 1000
        assert len(window.series) == 1
        assert len(window.series[0].points) == 1  # only valid point kept


@pytest.mark.asyncio
async def test_prometheus_client_lifecycle_and_defaults() -> None:
    client = PrometheusClient(base_url="http://localhost:9090")
    http_c1 = await client._get_client()
    http_c2 = await client._get_client()
    assert http_c1 is http_c2
    assert not http_c1.is_closed
    await client.close()
    assert http_c1.is_closed


@pytest.mark.asyncio
async def test_prometheus_loki_adapter_delegation() -> None:
    clock = FrozenClock(initial_time=FIXED_NOW)

    # Mock responses for Prometheus and Loki
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/loki" in url_str:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {
                                "stream": {"app": "data-service", "namespace": "ust-prod"},
                                "values": [
                                    ["1789307936000000000", '{"level": "error", "event": "err 1"}'],
                                ],
                            }
                        ],
                    },
                },
            )
        if "up" in url_str:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [{"metric": {"app": "data-service"}, "value": [1789300000, "1"]}],
                    },
                },
            )
        # Standard metric window query
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "vector", "result": [{"value": [1789300000, "10"]}]},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        prom_client = PrometheusClient(
            base_url="http://localhost:9090", client=http_client, clock=clock
        )
        loki_client = LokiClient(base_url="http://localhost:3100", client=http_client, clock=clock)

        adapter = PrometheusLokiAdapter(
            prometheus_client=prom_client,
            loki_client=loki_client,
            clock=clock,
        )

        # Check protocol compliance
        assert isinstance(adapter, ObservabilityAdapter)

        # Check metric_window
        window = await adapter.metric_window("data-service", since=FIXED_NOW - timedelta(minutes=5))
        assert isinstance(window, MetricWindow)
        assert window.service == "data-service"

        # Check error_signatures
        sigs = await adapter.error_signatures(
            "data-service", since=FIXED_NOW - timedelta(minutes=5)
        )
        assert isinstance(sigs, list)
        assert len(sigs) == 1
        assert isinstance(sigs[0], ErrorSignature)

        # Check service_health
        healthy = await adapter.service_health("ust-prod", "data-service")
        assert healthy is True

        await adapter.close()
