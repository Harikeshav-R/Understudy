"""Unit tests for LokiClient, message normalization, and ErrorSignature aggregation."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from understudy.common.clock import FrozenClock
from understudy.common.errors import ObservabilityError
from understudy.signals.loki import (
    LokiClient,
    compute_fingerprint,
    normalize_message,
    parse_log_entry,
)

FIXED_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def test_normalize_message_cases() -> None:
    # Empty string
    assert normalize_message("") == ""

    # UUID replacement
    uuid_str = "Error for request 12345678-1234-5678-1234-567812345678 failed"
    assert normalize_message(uuid_str) == "Error for request <UUID> failed"

    # Hex address
    addr_str = "Pointer fault at 0x7ffd5e2b0c10 in worker"
    assert normalize_message(addr_str) == "Pointer fault at <ADDR> in worker"

    # ISO timestamp
    ts_str = "Failed at 2026-09-13T12:00:00.123456Z during sync"
    assert normalize_message(ts_str) == "Failed at <TIMESTAMP> during sync"

    # IP with and without port
    ip_str = "Connection reset by 192.168.1.50:8000 and 10.42.0.1"
    assert normalize_message(ip_str) == "Connection reset by <IP> and <IP>"

    # Line numbers and numbers
    line_str = 'File "app.py", line 42 in handler: timeout after 500 ms'
    assert (
        normalize_message(line_str)
        == 'File "app.py", line <NUM> in handler: timeout after <NUM> ms'
    )

    # Consecutive whitespace
    ws_str = "  multiple    spaces   and\nlines   "
    assert normalize_message(ws_str) == "multiple spaces and lines"


def test_compute_fingerprint() -> None:
    fp1 = compute_fingerprint("database timeout connecting to prod-postgres:<NUM>")
    fp2 = compute_fingerprint("database timeout connecting to prod-postgres:<NUM>")
    fp3 = compute_fingerprint("different error message")

    assert fp1 == fp2
    assert len(fp1) == 16
    assert fp1 != fp3


def test_parse_log_entry() -> None:
    ts_nano = 1789307936893887000
    expected_dt = datetime.fromtimestamp(1789307936.893887, tz=UTC)

    # Empty line
    is_err, msg, dt = parse_log_entry("   ", ts_nano)
    assert not is_err
    assert msg == ""

    # Valid structlog error JSON
    json_err = json.dumps(
        {
            "timestamp": "2026-09-13T12:00:00Z",
            "level": "error",
            "event": "Database connection failed",
        }
    )
    is_err, msg, dt = parse_log_entry(json_err, ts_nano)
    assert is_err
    assert msg == "Database connection failed"
    assert dt == expected_dt

    # Structlog error with message key
    json_msg = json.dumps(
        {
            "level": "critical",
            "message": "Out of memory in pool",
        }
    )
    is_err, msg, _ = parse_log_entry(json_msg, ts_nano)
    assert is_err
    assert msg == "Out of memory in pool"

    # Structlog error with status_code >= 500
    json_status = json.dumps(
        {
            "level": "info",
            "status_code": 503,
            "detail": "Service unavailable",
        }
    )
    is_err, msg, _ = parse_log_entry(json_status, ts_nano)
    assert is_err
    assert msg == "Service unavailable"

    # Structlog error with exception info
    json_exc = json.dumps(
        {
            "level": "info",
            "exc_info": True,
            "msg": "Unhandled exception",
        }
    )
    is_err, msg, _ = parse_log_entry(json_exc, ts_nano)
    assert is_err
    assert msg == "Unhandled exception"

    # Structlog non-error JSON
    json_info = json.dumps(
        {
            "level": "info",
            "event": "Health check ok",
        }
    )
    is_err, msg, _ = parse_log_entry(json_info, ts_nano)
    assert not is_err

    # Structlog error without any text message keys
    json_no_msg = json.dumps({"level": "error", "code": 500})
    is_err, msg, _ = parse_log_entry(json_no_msg, ts_nano)
    assert is_err
    assert msg == json_no_msg

    # Plaintext line with ERROR
    plain_err = "2026-09-13 12:00:00 [ERROR] Failed to bind socket"
    is_err, msg, _ = parse_log_entry(plain_err, ts_nano)
    assert is_err
    assert msg == plain_err

    # Plaintext line with Exception
    plain_exc = "RuntimeError: Something broke"
    is_err, msg, _ = parse_log_entry(plain_exc, ts_nano)
    assert is_err
    assert msg == plain_exc

    # Plaintext non-error line
    plain_info = "2026-09-13 12:00:00 [INFO] Request completed 200 OK"
    is_err, msg, _ = parse_log_entry(plain_info, ts_nano)
    assert not is_err

    # Bad JSON starting and ending with braces
    is_err, msg, _ = parse_log_entry("{bad json}", ts_nano)
    assert not is_err

    # Bad timestamp fallback
    _, _, bad_dt = parse_log_entry(plain_err, "invalid_timestamp")
    assert bad_dt.tzinfo == UTC


@pytest.mark.asyncio
async def test_loki_client_query_range_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/loki/api/v1/query_range"
        assert "query" in request.url.params
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
                                [
                                    "1789307936893887000",
                                    '{"level": "error", "event": "DB timeout at port 5432"}',
                                ],
                            ],
                        }
                    ],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = LokiClient(base_url="http://localhost:3100", client=http_client)
        res = await client.query_range(
            query='{namespace="ust-prod", app="data-service"}',
            start=FIXED_NOW - timedelta(minutes=10),
            end=FIXED_NOW,
        )
        assert res["status"] == "success"
        assert len(res["data"]["result"]) == 1


@pytest.mark.asyncio
async def test_loki_client_query_range_errors() -> None:
    # 500 error
    def err_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Loki Error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(err_handler)) as http_client:
        client = LokiClient(base_url="http://localhost:3100", client=http_client)
        with pytest.raises(ObservabilityError, match="Loki query failed"):
            await client.query_range(
                query="test",
                start=FIXED_NOW - timedelta(minutes=1),
                end=FIXED_NOW,
            )

    # Non-dict JSON response
    def non_dict_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(non_dict_handler)) as http_client:
        client = LokiClient(base_url="http://localhost:3100", client=http_client)
        with pytest.raises(ObservabilityError, match="Unexpected Loki response type"):
            await client.query_range(
                query="test",
                start=FIXED_NOW - timedelta(minutes=1),
                end=FIXED_NOW,
            )


@pytest.mark.asyncio
async def test_loki_client_error_signatures_aggregation() -> None:
    t1_nano = 1789307000000000000
    t2_nano = 1789307100000000000
    t3_nano = 1789307200000000000

    def handler(_request: httpx.Request) -> httpx.Response:
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
                                [
                                    str(t1_nano),
                                    '{"level": "error", "event": "DB timeout at port 5432"}',
                                ],
                                [
                                    str(t2_nano),
                                    '{"level": "error", "event": "DB timeout at port 5433"}',
                                ],
                                [
                                    str(t3_nano),
                                    (
                                        '{"level": "error", "event": '
                                        '"Unique constraint violation on item 99"}'
                                    ),
                                ],
                                ["1789307300000000000", '{"level": "info", "event": "all good"}'],
                                # Malformed items
                                "not_a_list",
                                ["missing_value"],
                            ],
                        },
                        "not_a_dict_stream",
                        {"stream": {}, "values": "not_a_list"},
                    ],
                },
            },
        )

    clock = FrozenClock(initial_time=FIXED_NOW)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = LokiClient(
            base_url="http://localhost:3100",
            client=http_client,
            clock=clock,
        )

        # Test start_time > end_time swapping
        since = FIXED_NOW + timedelta(minutes=5)
        signatures = await client.error_signatures(
            service="data-service",
            since=since,
            namespace="ust-prod",
            until=FIXED_NOW - timedelta(minutes=5),
        )

        assert len(signatures) == 2
        # Most frequent signature first
        top_sig = signatures[0]
        assert top_sig.service == "data-service"
        assert top_sig.count == 2
        assert top_sig.message == "DB timeout at port <NUM>"
        assert top_sig.first_seen == datetime.fromtimestamp(t1_nano / 1e9, tz=UTC)
        assert top_sig.last_seen == datetime.fromtimestamp(t2_nano / 1e9, tz=UTC)

        second_sig = signatures[1]
        assert second_sig.count == 1
        assert second_sig.message == "Unique constraint violation on item <NUM>"


@pytest.mark.asyncio
async def test_loki_client_error_signatures_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "streams", "result": []}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LokiClient(base_url="http://localhost:3100", client=http_client)
        signatures = await client.error_signatures(
            service="data-service",
            since=FIXED_NOW - timedelta(minutes=10),
            namespace="ust-prod",
            until=FIXED_NOW,
        )
        assert signatures == []


@pytest.mark.asyncio
async def test_loki_client_error_signatures_out_of_order() -> None:
    # First entry t2, then entry t1 (earlier than t2), same fingerprint
    t1_nano = 1789307000000000000
    t2_nano = 1789307100000000000

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": [
                        {
                            "stream": {"app": "worker", "namespace": "ust-prod"},
                            "values": [
                                [str(t2_nano), '{"level": "error", "event": "job worker crash"}'],
                                [str(t1_nano), '{"level": "error", "event": "job worker crash"}'],
                            ],
                        }
                    ],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LokiClient(base_url="http://localhost:3100", client=http_client)
        signatures = await client.error_signatures(
            service="worker",
            since=FIXED_NOW - timedelta(minutes=10),
            until=FIXED_NOW,
        )
        assert len(signatures) == 1
        assert signatures[0].count == 2
        assert signatures[0].first_seen == datetime.fromtimestamp(t1_nano / 1e9, tz=UTC)
        assert signatures[0].last_seen == datetime.fromtimestamp(t2_nano / 1e9, tz=UTC)


@pytest.mark.asyncio
async def test_loki_client_error_signatures_non_list_result() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "success", "data": {"resultType": "streams", "result": "not_a_list"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LokiClient(base_url="http://localhost:3100", client=http_client)
        signatures = await client.error_signatures(
            service="worker",
            since=FIXED_NOW - timedelta(minutes=10),
            until=FIXED_NOW,
        )
        assert signatures == []


@pytest.mark.asyncio
async def test_loki_client_lifecycle_and_defaults() -> None:
    client = LokiClient(base_url="http://localhost:3100")
    # Exercise _get_client initialization and re-use
    http_c1 = await client._get_client()
    http_c2 = await client._get_client()
    assert http_c1 is http_c2
    assert not http_c1.is_closed
    await client.close()
    assert http_c1.is_closed


@pytest.mark.asyncio
async def test_loki_client_close_unopened() -> None:
    # Close when no client was created
    client2 = LokiClient(base_url="http://localhost:3100")
    await client2.close()
