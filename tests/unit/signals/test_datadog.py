"""Unit tests for DatadogClient and DatadogAdapter."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from understudy.common.clock import FrozenClock
from understudy.common.errors import DatadogError
from understudy.contracts.incident import MetricWindow
from understudy.signals.api import ObservabilityAdapter
from understudy.signals.datadog import (
    DatadogAdapter,
    DatadogClient,
    _extract_log_message_and_error,
    _parse_datadog_timestamp,
    _parse_log_timestamp,
)

FIXED_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def test_parse_datadog_timestamp() -> None:
    # Milliseconds branch (> 1e11)
    dt_ms = _parse_datadog_timestamp(1789300000000.0)
    assert dt_ms.tzinfo == UTC
    assert dt_ms.timestamp() == 1789300000.0

    # Seconds branch (<= 1e11)
    dt_sec = _parse_datadog_timestamp(1789300000.0)
    assert dt_sec.tzinfo == UTC
    assert dt_sec.timestamp() == 1789300000.0


def test_parse_log_timestamp() -> None:
    # ISO string with Z
    dt_z = _parse_log_timestamp("2026-09-13T12:00:00Z")
    assert dt_z == datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

    # ISO string with tz offset
    dt_off = _parse_log_timestamp("2026-09-13T16:00:00+04:00")
    assert dt_off == datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

    # Naive ISO string gets UTC attached
    dt_naive = _parse_log_timestamp("2026-09-13T12:00:00")
    assert dt_naive.tzinfo == UTC
    assert dt_naive.hour == 12

    # Invalid ISO string falls back to current time
    dt_inv = _parse_log_timestamp("invalid-date")
    assert dt_inv.tzinfo == UTC

    # Numeric timestamp (int/float)
    dt_num = _parse_log_timestamp(1789300000.0)
    assert dt_num.timestamp() == 1789300000.0

    # Overflow / invalid numeric
    dt_overflow = _parse_log_timestamp(1e25)
    assert dt_overflow.tzinfo == UTC

    # Unsupported type (None, list, etc.)
    dt_none = _parse_log_timestamp(None)
    assert dt_none.tzinfo == UTC


def test_extract_log_message_and_error() -> None:
    # Non-dict attrs fallback
    is_err, msg, dt = _extract_log_message_and_error({"attributes": None})
    assert is_err is False
    assert msg == ""
    assert isinstance(dt, datetime)

    # Standard message and error status
    evt1 = {
        "attributes": {
            "status": "error",
            "message": "Database connection refused",
            "timestamp": "2026-09-13T12:00:00Z",
        }
    }
    is_err, msg, dt = _extract_log_message_and_error(evt1)
    assert is_err is True
    assert msg == "Database connection refused"
    assert dt == datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

    # Event field fallback
    evt2 = {
        "attributes": {
            "status": "CRITICAL",
            "event": "Out of memory",
        }
    }
    is_err, msg, _ = _extract_log_message_and_error(evt2)
    assert is_err is True
    assert msg == "Out of memory"

    # Nested attributes message and event
    evt3 = {
        "attributes": {
            "attributes": {
                "message": "Nested error message",
                "status": "fatal",
            }
        }
    }
    is_err, msg, _ = _extract_log_message_and_error(evt3)
    assert is_err is True
    assert msg == "Nested error message"

    evt3b = {
        "attributes": {
            "attributes": {
                "event": "Nested event message",
                "status": "warn",
            }
        }
    }
    is_err, msg, _ = _extract_log_message_and_error(evt3b)
    assert is_err is True
    assert msg == "Nested event message"

    # Empty / whitespace message
    evt4 = {"attributes": {"message": "   "}}
    assert _extract_log_message_and_error(evt4)[0] is False

    # JSON structlog message with error level
    json_err = '{"level": "error", "event": "HTTP request failed", "status_code": 502}'
    evt5 = {"attributes": {"message": json_err, "status": "info"}}
    is_err, msg, _ = _extract_log_message_and_error(evt5)
    assert is_err is True
    assert msg == "HTTP request failed"

    # JSON structlog message with 5xx status code
    json_5xx = '{"level": "info", "message": "Gateway error", "status_code": 500}'
    evt6 = {"attributes": {"message": json_5xx, "status": "info"}}
    is_err, msg, _ = _extract_log_message_and_error(evt6)
    assert is_err is True
    assert msg == "Gateway error"

    # JSON structlog message with exception flag
    json_exc = '{"level": "info", "event": "Unhandled crash", "exc_info": "traceback..."}'
    evt7 = {"attributes": {"message": json_exc, "status": "info"}}
    is_err, msg, _ = _extract_log_message_and_error(evt7)
    assert is_err is True
    assert msg == "Unhandled crash"

    # JSON structlog non-error
    json_ok = '{"level": "info", "event": "All good", "status_code": 200}'
    evt8 = {"attributes": {"message": json_ok, "status": "info"}}
    is_err, msg, _ = _extract_log_message_and_error(evt8)
    assert is_err is False
    assert msg == "All good"

    # Malformed JSON in message does not raise and preserves string
    evt9 = {"attributes": {"message": "{not-valid-json}", "status": "info"}}
    is_err, msg, _ = _extract_log_message_and_error(evt9)
    assert is_err is False
    assert msg == "{not-valid-json}"


@pytest.mark.asyncio
async def test_datadog_client_init_and_headers() -> None:
    # Explicit keys and base_url
    client = DatadogClient(
        api_key="key123",
        app_key="app456",
        base_url="https://custom.datadog.com/",
        timeout=15.0,
    )
    assert client.api_key == "key123"
    assert client.app_key == "app456"
    assert client.base_url == "https://custom.datadog.com"
    headers = client._get_headers()
    assert headers["DD-API-KEY"] == "key123"
    assert headers["DD-APPLICATION-KEY"] == "app456"

    # Default site resolution
    client_site = DatadogClient(site="datadoghq.eu")
    assert client_site.base_url == "https://api.datadoghq.eu"

    # Missing keys
    client_no_keys = DatadogClient(api_key="", app_key="")
    headers_no_keys = client_no_keys._get_headers()
    assert "DD-API-KEY" not in headers_no_keys
    assert "DD-APPLICATION-KEY" not in headers_no_keys


@pytest.mark.asyncio
async def test_datadog_client_owned_client_and_close() -> None:
    # Uninjected client creates owned client on demand and closes it
    client = DatadogClient(api_key="k", app_key="a")
    http_c = await client._get_client()
    assert isinstance(http_c, httpx.AsyncClient)
    assert not http_c.is_closed
    # Calling again reuses the unclosed client
    http_c2 = await client._get_client()
    assert http_c2 is http_c

    await client.close()
    # Close again is idempotent
    await client.close()
    assert bool(http_c.is_closed)

    # Custom injected client is not closed by client.close()
    custom_c = httpx.AsyncClient()
    client_custom = DatadogClient(client=custom_c)
    assert await client_custom._get_client() is custom_c
    await client_custom.close()
    assert not custom_c.is_closed
    await custom_c.aclose()


@pytest.mark.asyncio
async def test_datadog_client_query_metrics_success_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "error_syntax" in url_str:
            return httpx.Response(200, json={"status": "error", "error": "Invalid metric query"})
        if "http_500" in url_str:
            return httpx.Response(500, text="Internal Datadog Error")
        if "non_dict" in url_str:
            return httpx.Response(200, json=["unexpected-list"])
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "series": [
                    {
                        "metric": "http_requests_total",
                        "pointlist": [[1789300000000.0, 42.0]],
                        "tag_set": ["service:data-service", "namespace:ust-prod"],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DatadogClient(client=http_client)

        res = await client.query_metrics("sum:up{}", FIXED_NOW, FIXED_NOW + timedelta(minutes=5))
        assert res["status"] == "ok"
        assert len(res["series"]) == 1

        with pytest.raises(DatadogError, match="Datadog query error: Invalid metric query"):
            await client.query_metrics("error_syntax", FIXED_NOW, FIXED_NOW + timedelta(minutes=5))

        with pytest.raises(DatadogError, match="Datadog query failed"):
            await client.query_metrics("http_500", FIXED_NOW, FIXED_NOW + timedelta(minutes=5))

        with pytest.raises(DatadogError, match="Unexpected Datadog response type"):
            await client.query_metrics("non_dict", FIXED_NOW, FIXED_NOW + timedelta(minutes=5))


@pytest.mark.asyncio
async def test_datadog_client_search_logs_success_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "http_500" in url_str:
            return httpx.Response(500, text="Internal Logs Error")
        if "non_dict" in url_str:
            return httpx.Response(200, json="string-response")
        if "non_list_data" in url_str:
            return httpx.Response(200, json={"data": "not-a-list"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "log_1",
                        "attributes": {
                            "status": "error",
                            "message": "Connection refused to backend",
                            "timestamp": "2026-09-13T12:01:00Z",
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DatadogClient(client=http_client)

        logs = await client.search_logs("status:error", FIXED_NOW, FIXED_NOW + timedelta(minutes=5))
        assert len(logs) == 1
        assert logs[0]["id"] == "log_1"

        with pytest.raises(DatadogError, match="Datadog search logs failed"):
            client_fail = DatadogClient(
                base_url="https://api.datadoghq.com/http_500", client=http_client
            )
            await client_fail.search_logs(
                "status:error", FIXED_NOW, FIXED_NOW + timedelta(minutes=5)
            )

        with pytest.raises(DatadogError, match="Unexpected Datadog logs response type"):
            client_nondict = DatadogClient(
                base_url="https://api.datadoghq.com/non_dict", client=http_client
            )
            await client_nondict.search_logs(
                "status:error", FIXED_NOW, FIXED_NOW + timedelta(minutes=5)
            )

        client_nonlist = DatadogClient(
            base_url="https://api.datadoghq.com/non_list_data", client=http_client
        )
        assert (
            await client_nonlist.search_logs(
                "status:error", FIXED_NOW, FIXED_NOW + timedelta(minutes=5)
            )
            == []
        )


@pytest.mark.asyncio
async def test_datadog_client_check_monitors_success_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "http_500" in url_str:
            return httpx.Response(500, text="Monitor search error")
        if "non_dict" in url_str:
            return httpx.Response(200, json=["invalid"])
        if "non_list_monitors" in url_str:
            return httpx.Response(200, json={"monitors": None})
        return httpx.Response(
            200,
            json={
                "monitors": [
                    {"id": 1234, "name": "data-service health", "status": "OK"},
                    {"id": 1235, "name": "auth-service health", "status": "Alert"},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DatadogClient(client=http_client)
        monitors = await client.check_monitors("service:data-service")
        assert len(monitors) == 2

        # Non-dict and non-list monitors return []
        client_nondict = DatadogClient(
            base_url="https://api.datadoghq.com/non_dict", client=http_client
        )
        assert await client_nondict.check_monitors("query") == []

        client_nonlist = DatadogClient(
            base_url="https://api.datadoghq.com/non_list_monitors", client=http_client
        )
        assert await client_nonlist.check_monitors("query") == []

        # HTTP error logs warning and returns []
        client_err = DatadogClient(
            base_url="https://api.datadoghq.com/http_500", client=http_client
        )
        assert await client_err.check_monitors("query") == []


@pytest.mark.asyncio
async def test_datadog_adapter_metric_window_full_flow() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query_param = request.url.params.get("query", "")
        if "status_code:5*" in query_param:
            # Errors query
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "series": [
                        {
                            "metric": "http_requests_total",
                            "pointlist": [
                                [1789300000000.0, 5.0],
                                [1789300015000.0, None],  # None point test
                                [1789300030000.0, 5.0],
                            ],
                        }
                    ],
                },
            )
        if "p99:http_request_duration_seconds" in query_param:
            # Latency query (value in seconds < 100 -> should scale to ms)
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "series": [
                        {
                            "metric": "http_request_duration_seconds",
                            "pointlist": [
                                [1789300000000.0, 0.125],
                                [1789300015000.0, "invalid_float"],
                            ],
                        }
                    ],
                },
            )
        # Total requests query
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "series": [
                    {
                        "metric": "http_requests_total",
                        "tag_set": [
                            "service:data-service",
                            "namespace:ust-prod",
                            "raw_tag_without_colon",
                        ],
                        "pointlist": [
                            [1789300000000.0, 50.0],
                            [1789300015000.0, 50.0],
                            ["not_a_tuple"],
                            [1e25, 10.0],  # Overflow timestamp handled gracefully
                        ],
                    },
                    "not_a_dict_item",
                ],
            },
        )

    clock = FrozenClock(FIXED_NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        dd_client = DatadogClient(client=http_client, clock=clock)
        adapter = DatadogAdapter(datadog_client=dd_client, clock=clock)
        assert isinstance(adapter, ObservabilityAdapter)

        # Inverted time range (since > now) is swapped automatically
        since = FIXED_NOW + timedelta(minutes=10)
        window = await adapter.metric_window(
            service="data-service",
            since=since,
            namespace="ust-prod",
            until=FIXED_NOW,
        )

        assert isinstance(window, MetricWindow)
        assert window.service == "data-service"
        assert window.request_count == 100
        assert window.error_rate == pytest.approx(0.1)  # 10 errors / 100 requests
        assert window.p99_latency_ms == 125.0  # 0.125s * 1000 = 125ms
        assert len(window.series) == 1
        assert window.series[0].labels["service"] == "data-service"
        assert window.series[0].labels["namespace"] == "ust-prod"
        assert len(window.series[0].points) == 2

        await adapter.close()


@pytest.mark.asyncio
async def test_datadog_adapter_metric_window_fallbacks_and_zero_traffic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query_param = request.url.params.get("query", "")
        # Primary service: queries return empty series, app: queries return data
        if "service:data-service" in query_param:
            return httpx.Response(200, json={"status": "ok", "series": []})
        if "app:data-service" in query_param and "status_code:5*" in query_param:
            return httpx.Response(
                200,
                json={"status": "ok", "series": [{"pointlist": [[1789300000.0, 0.0]]}]},
            )
        if "app:data-service" in query_param and "p99:" in query_param:
            # Value already >= 100ms
            return httpx.Response(
                200,
                json={"status": "ok", "series": [{"pointlist": [[1789300000.0, 250.5]]}]},
            )
        if "app:data-service" in query_param:
            return httpx.Response(
                200,
                json={"status": "ok", "series": [{"pointlist": [[1789300000.0, 0.0]]}]},
            )
        return httpx.Response(200, json={"status": "ok", "series": []})

    clock = FrozenClock(FIXED_NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        dd_client = DatadogClient(client=http_client, clock=clock)
        adapter = DatadogAdapter(datadog_client=dd_client, clock=clock)

        window = await adapter.metric_window(
            service="data-service",
            since=FIXED_NOW - timedelta(minutes=5),
        )

        assert window.request_count == 0
        assert window.error_rate == 0.0
        assert window.p99_latency_ms == 250.5


@pytest.mark.asyncio
async def test_datadog_adapter_error_signatures_aggregation() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "1",
                        "attributes": {
                            "status": "error",
                            "message": "Connection to redis://127.0.0.1:6379 failed with timeout",
                            "timestamp": "2026-09-13T12:01:00Z",
                        },
                    },
                    {
                        "id": "2",
                        "attributes": {
                            "status": "error",
                            "message": "Connection to redis://127.0.0.1:6380 failed with timeout",
                            "timestamp": "2026-09-13T12:05:00Z",
                        },
                    },
                    {
                        "id": "3",
                        "attributes": {
                            "status": "fatal",
                            "message": "Out of memory error in worker thread 4",
                            "timestamp": "2026-09-13T12:03:00Z",
                        },
                    },
                    {
                        "id": "4",
                        "attributes": {
                            "status": "info",
                            "message": "Normal operational event",
                            "timestamp": "2026-09-13T12:02:00Z",
                        },
                    },
                ]
            },
        )

    clock = FrozenClock(FIXED_NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        dd_client = DatadogClient(client=http_client, clock=clock)
        adapter = DatadogAdapter(datadog_client=dd_client, clock=clock)

        signatures = await adapter.error_signatures(
            service="data-service",
            since=FIXED_NOW - timedelta(minutes=10),
            namespace="ust-prod",
        )

        # Both redis timeouts normalize to identical template (<IP> replaced) and group together
        assert len(signatures) == 2
        # Top count signature is Redis connection timeout
        assert signatures[0].count == 2
        assert "<IP>" in signatures[0].message
        assert signatures[0].first_seen == datetime(2026, 9, 13, 12, 1, 0, tzinfo=UTC)
        assert signatures[0].last_seen == datetime(2026, 9, 13, 12, 5, 0, tzinfo=UTC)

        # Second signature is Out of memory
        assert signatures[1].count == 1
        assert "Out of memory" in signatures[1].message


@pytest.mark.asyncio
async def test_datadog_adapter_service_health_check() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        query_param = request.url.params.get("query", "")
        # Monitor search endpoint
        if "/api/v1/monitor/search" in url_str:
            if "unhealthy-service" in query_param or "unhealthy-service" in url_str:
                return httpx.Response(
                    200,
                    json={"monitors": [{"status": "Alert", "name": "Latency breach"}]},
                )
            if "healthy-service" in query_param or "healthy-service" in url_str:
                return httpx.Response(
                    200,
                    json={"monitors": [{"status": "OK", "name": "All good"}]},
                )
            if "no-monitor-service" in query_param or "no-monitor-service" in url_str:
                return httpx.Response(200, json={"monitors": []})
            if "metric-down-service" in query_param or "metric-down-service" in url_str:
                return httpx.Response(200, json={"monitors": []})
            if "error-service" in query_param or "error-service" in url_str:
                return httpx.Response(500, text="Monitor search down")

        # Metric query fallback endpoint
        if "/api/v1/query" in url_str:
            if "no-monitor-service" in query_param or "no-monitor-service" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "series": [{"pointlist": [[1789300000.0, 1.0]]}],
                    },
                )
            if "metric-down-service" in query_param or "metric-down-service" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "series": [{"pointlist": [[1789300000.0, 0.0]]}],
                    },
                )
            if "error-service" in query_param or "error-service" in url_str:
                return httpx.Response(500, text="Metric query down")

        return httpx.Response(404, text="Not found")

    clock = FrozenClock(FIXED_NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        dd_client = DatadogClient(client=http_client, clock=clock)
        adapter = DatadogAdapter(datadog_client=dd_client, clock=clock)

        # Monitor in Alert -> False
        assert await adapter.service_health("ust-prod", "unhealthy-service") is False

        # Monitor in OK -> True
        assert await adapter.service_health("ust-prod", "healthy-service") is True

        # No monitor, metric up = 1.0 -> True
        assert await adapter.service_health("ust-prod", "no-monitor-service") is True

        # No monitor, metric up = 0.0 -> False
        assert await adapter.service_health("ust-prod", "metric-down-service") is False

        # Errors handled gracefully -> False
        assert await adapter.service_health("ust-prod", "error-service") is False


def test_extract_log_message_and_error_edge_cases() -> None:
    # Nested status applied when outer status empty
    evt_nested_status = {
        "attributes": {
            "message": "some message",
            "attributes": {"status": "error"},
        }
    }
    is_err, msg, _ = _extract_log_message_and_error(evt_nested_status)
    assert is_err is True
    assert msg == "some message"

    # JSON parsed is a list (not dict)
    evt_json_list = {"attributes": {"message": "[1, 2, 3]", "status": "info"}}
    is_err, msg, _ = _extract_log_message_and_error(evt_json_list)
    assert is_err is False
    assert msg == "[1, 2, 3]"

    # JSON dict with no event/message string
    evt_json_no_str = {"attributes": {"message": '{"level": "error", "event": 12345}'}}
    is_err, msg, _ = _extract_log_message_and_error(evt_json_no_str)
    assert is_err is True
    assert msg == '{"level": "error", "event": 12345}'


@pytest.mark.asyncio
async def test_datadog_client_single_keys_and_default_adapter() -> None:
    # API key only
    c_api = DatadogClient(api_key="only_api", app_key="")
    h_api = c_api._get_headers()
    assert "DD-API-KEY" in h_api
    assert "DD-APPLICATION-KEY" not in h_api

    # APP key only
    c_app = DatadogClient(api_key="", app_key="only_app")
    h_app = c_app._get_headers()
    assert "DD-API-KEY" not in h_app
    assert "DD-APPLICATION-KEY" in h_app

    # Default adapter with no arguments
    adapter_default = DatadogAdapter()
    assert isinstance(adapter_default.client, DatadogClient)
    assert isinstance(adapter_default, ObservabilityAdapter)


@pytest.mark.asyncio
async def test_datadog_adapter_metric_window_malformed_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query_param = request.url.params.get("query", "")
        if "status_code:5*" in query_param:
            # Series is not a list, then fallback returns malformed items
            if "app:bad-data" in query_param:
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "series": [
                            "not_a_dict",
                            {"pointlist": "not_a_list"},
                            {"pointlist": [["too_short"], [123, "not_a_float"], [124, 1.0]]},
                        ],
                    },
                )
            return httpx.Response(200, json={"status": "ok", "series": None})

        if "p99:" in query_param:
            if "app:bad-data" in query_param:
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "series": [
                            "not_a_dict",
                            {"pointlist": "not_a_list"},
                            {
                                "pointlist": [
                                    ["too_short"],
                                    [123, None],
                                    [124, "bad_val"],
                                    [125, 0.05],
                                ]
                            },
                        ],
                    },
                )
            return httpx.Response(200, json={"status": "ok", "series": None})

        # Requests query
        if "app:bad-data" in query_param:
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "series": [
                        {
                            "tag_set": None,  # Not a list
                            "pointlist": None,  # Not a list
                        },
                        {
                            "tag_set": ["valid:tag"],
                            "pointlist": [[1789300000.0, None], [1789300001.0, 10.0]],
                        },
                    ],
                },
            )
        return httpx.Response(200, json={"status": "ok", "series": None})

    clock = FrozenClock(FIXED_NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        dd_client = DatadogClient(client=http_client, clock=clock)
        adapter = DatadogAdapter(datadog_client=dd_client, clock=clock)

        window = await adapter.metric_window(
            service="bad-data",
            since=FIXED_NOW - timedelta(minutes=5),
        )
        assert window.request_count == 10
        assert window.p99_latency_ms == 50.0  # 0.05s * 1000 = 50ms


@pytest.mark.asyncio
async def test_datadog_adapter_error_signatures_edge_cases() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "attributes": {
                            "status": "error",
                            "message": "Error one",
                            "timestamp": "2026-09-13T12:05:00Z",
                        }
                    },
                    {
                        "attributes": {
                            "status": "error",
                            "message": "Error one",
                            # Earlier timestamp than previous -> triggers dt < first_seen
                            "timestamp": "2026-09-13T12:01:00Z",
                        }
                    },
                    {
                        "attributes": {
                            "status": "error",
                            "message": "Error one",
                            # Timestamp between first_seen and last_seen -> triggers dt <= last_seen
                            "timestamp": "2026-09-13T12:03:00Z",
                        }
                    },
                ]
            },
        )

    clock = FrozenClock(FIXED_NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        dd_client = DatadogClient(client=http_client, clock=clock)
        adapter = DatadogAdapter(datadog_client=dd_client, clock=clock)

        # Inverted time range: since > until
        sigs = await adapter.error_signatures(
            service="svc",
            since=FIXED_NOW,
            until=FIXED_NOW - timedelta(minutes=10),
        )
        assert len(sigs) == 1
        assert sigs[0].count == 3
        assert sigs[0].first_seen == datetime(2026, 9, 13, 12, 1, 0, tzinfo=UTC)
        assert sigs[0].last_seen == datetime(2026, 9, 13, 12, 5, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_datadog_adapter_service_health_metric_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/api/v1/monitor/search" in url_str:
            return httpx.Response(200, json={"monitors": []})

        if "/api/v1/query" in url_str:
            # Series contains non-dict, pointlist with too short, None, and bad float
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "series": [
                        "not_dict",
                        {"pointlist": "not_list"},
                        {
                            "pointlist": [
                                ["short"],
                                [123, None],
                                [124, "invalid_float"],
                            ]
                        },
                    ],
                },
            )
        return httpx.Response(404)

    clock = FrozenClock(FIXED_NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        dd_client = DatadogClient(client=http_client, clock=clock)
        adapter = DatadogAdapter(datadog_client=dd_client, clock=clock)
        assert await adapter.service_health("ust-prod", "svc") is False


def test_extract_log_message_nested_status_ignored_when_outer_present() -> None:
    evt = {
        "attributes": {
            "status": "error",
            "message": "Outer error",
            "attributes": {"status": "info"},
        }
    }
    is_err, msg, _ = _extract_log_message_and_error(evt)
    assert is_err is True
    assert msg == "Outer error"


@pytest.mark.asyncio
async def test_datadog_adapter_metric_window_fallback_returns_non_list() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "series": "invalid_non_list"})

    clock = FrozenClock(FIXED_NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        dd_client = DatadogClient(client=http_client, clock=clock)
        adapter = DatadogAdapter(datadog_client=dd_client, clock=clock)
        w = await adapter.metric_window(service="svc", since=FIXED_NOW)
        assert w.request_count == 0
        assert w.error_rate == 0.0
        assert w.p99_latency_ms is None


@pytest.mark.asyncio
async def test_datadog_adapter_service_health_empty_series() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/api/v1/monitor/search" in url_str:
            return httpx.Response(200, json={"monitors": []})
        if "/api/v1/query" in url_str:
            return httpx.Response(200, json={"status": "ok", "series": []})
        return httpx.Response(404)

    clock = FrozenClock(FIXED_NOW)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        dd_client = DatadogClient(client=http_client, clock=clock)
        adapter = DatadogAdapter(datadog_client=dd_client, clock=clock)
        assert await adapter.service_health("ust-prod", "svc") is False
