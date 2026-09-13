"""Unit tests for services/mirror_gateway: proxying, bounded queues, drain workers, and metrics."""

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import status

from services.mirror_gateway.app import (
    app,
    check_prod_reachability,
    get_target_prod_url,
    lifespan,
)
from services.mirror_gateway.core import (
    MirroredRequest,
    MirrorGatewayManager,
    MirrorStats,
    MirrorStatsResponse,
)


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Test client with application lifespan active."""
    async with (
        lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


# ==============================================================================
# Core MirrorGatewayManager Unit Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_mirror_stats_response_model() -> None:
    stats = MirrorStatsResponse(twin_id="twin-1", delivered=10, dropped=2, drop_ratio=2 / 12)
    assert stats.twin_id == "twin-1"
    assert stats.delivered == 10
    assert stats.dropped == 2
    assert pytest.approx(stats.drop_ratio, rel=1e-3) == 2 / 12

    empty_stats = MirrorStatsResponse(twin_id="twin-2")
    assert empty_stats.drop_ratio == 0.0


@pytest.mark.asyncio
async def test_manager_register_and_unregister() -> None:
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    manager = MirrorGatewayManager(client=mock_http, queue_maxsize=10, worker_timeout_seconds=2.0)

    # 1. Register twin
    twin = manager.register_twin("twin-1", "http://twin-1:8000", incident_id="inc-1")
    assert "twin-1" in manager.registered_twins
    assert twin.twin_id == "twin-1"
    assert twin.base_url == "http://twin-1:8000"
    assert twin.incident_id == "inc-1"
    assert twin.worker_task is not None
    assert not twin.worker_task.done()

    # 2. Re-register existing twin (cancels old worker and updates)
    old_task = twin.worker_task
    assert old_task is not None
    twin2 = manager.register_twin("twin-1", "http://twin-1-new:8000", incident_id="inc-1")
    await asyncio.sleep(0.01)
    assert old_task.done() or old_task.cancelled()
    assert twin2.base_url == "http://twin-1-new:8000"

    # 3. Stats for empty activity
    stats = manager.get_stats("twin-1")
    assert stats is not None
    assert stats.delivered == 0
    assert stats.dropped == 0
    assert stats.drop_ratio == 0.0

    # 4. Unregister twin
    assert manager.unregister_twin("twin-1") is True
    assert "twin-1" not in manager.registered_twins
    assert manager.get_stats("twin-1") is None

    # 5. Unregister nonexistent twin
    assert manager.unregister_twin("nonexistent") is False

    await manager.close()


@pytest.mark.asyncio
async def test_manager_unregister_already_done_task() -> None:
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    manager = MirrorGatewayManager(client=mock_http, queue_maxsize=10)
    twin = manager.register_twin("twin-done", "http://twin:8000")

    # Manually cancel and wait for task to be done
    assert twin.worker_task is not None
    twin.worker_task.cancel()
    await asyncio.sleep(0.01)
    assert twin.worker_task.done()

    assert manager.unregister_twin("twin-done") is True
    await manager.close()


@pytest.mark.asyncio
async def test_manager_get_all_stats() -> None:
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    manager = MirrorGatewayManager(client=mock_http)
    manager.register_twin("twin-1", "http://twin-1:8000")
    manager.register_twin("twin-2", "http://twin-2:8000")

    all_stats = manager.get_all_stats()
    assert "twin-1" in all_stats
    assert "twin-2" in all_stats
    assert all_stats["twin-1"].delivered == 0

    await manager.close()


@pytest.mark.asyncio
async def test_manager_close_with_done_tasks() -> None:
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    manager = MirrorGatewayManager(client=mock_http)
    twin = manager.register_twin("twin-1", "http://twin-1:8000")
    assert twin.worker_task is not None
    twin.worker_task.cancel()
    await asyncio.sleep(0.01)

    # Closing manager when task is already done should not error
    await manager.close()
    assert len(manager.registered_twins) == 0


@pytest.mark.asyncio
async def test_manager_bounded_queue_overflow_drop() -> None:
    """When queue is full (maxsize=2), dispatch drops overflow requests and increments dropped."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    # Slow worker mock to let queue fill
    ready_evt = asyncio.Event()

    async def mock_request(*_args: object, **_kwargs: object) -> httpx.Response:
        await ready_evt.wait()
        return httpx.Response(200)

    mock_http.request.side_effect = mock_request

    manager = MirrorGatewayManager(client=mock_http, queue_maxsize=2)
    twin = manager.register_twin("twin-overflow", "http://twin:8000")

    req = MirroredRequest(
        method="GET",
        path="/items",
        query="limit=10",
        headers={"accept": "application/json"},
        body=b"",
    )

    # 1st request goes in-flight to worker
    manager.dispatch_to_twins(req)
    await asyncio.sleep(0.01)
    # 2nd and 3rd fill the queue (maxsize=2)
    manager.dispatch_to_twins(req)
    manager.dispatch_to_twins(req)
    assert twin.queue.qsize() == 2

    # 4th request overflows bounded queue
    manager.dispatch_to_twins(req)
    assert twin.dropped == 1

    # 5th request also overflows
    manager.dispatch_to_twins(req)
    assert twin.dropped == 2

    # Release worker
    ready_evt.set()
    await asyncio.sleep(0.05)

    stats = manager.get_stats("twin-overflow")
    assert stats is not None
    assert stats.dropped == 2
    assert stats.delivered >= 1

    await manager.close()


@pytest.mark.asyncio
async def test_drain_worker_header_injection_and_delivery() -> None:
    """Drain worker injects shadow headers, strips hop-by-hop headers, and records delivery."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    captured_kwargs: dict[str, object] = {}

    async def fake_request(*_args: object, **kwargs: object) -> httpx.Response:
        captured_kwargs.update(kwargs)
        return httpx.Response(200, json={"ok": True})

    mock_http.request.side_effect = fake_request

    manager = MirrorGatewayManager(client=mock_http, worker_timeout_seconds=2.0)
    twin = manager.register_twin("twin-hdr", "http://twin-hdr:8000", incident_id="inc-test")

    req = MirroredRequest(
        method="POST",
        path="/api/items",
        query="sort=asc",
        headers={
            "Host": "prod.example.com",
            "Content-Length": "15",
            "Connection": "keep-alive",
            "Authorization": "Bearer test-token",
        },
        body=b'{"name": "foo"}',
    )
    manager.dispatch_to_twins(req)

    # Wait for queue to drain
    await asyncio.sleep(0.05)

    assert twin.delivered == 1
    assert twin.dropped == 0
    assert captured_kwargs["method"] == "POST"
    assert captured_kwargs["url"] == "http://twin-hdr:8000/api/items?sort=asc"
    assert captured_kwargs["content"] == b'{"name": "foo"}'
    assert captured_kwargs["timeout"] == 2.0

    headers = captured_kwargs["headers"]
    assert isinstance(headers, dict)
    assert headers["X-Understudy-Shadow"] == "1"
    assert headers["X-Understudy-Twin"] == "twin-hdr"
    assert headers["X-Understudy-Incident"] == "inc-test"
    assert headers["Authorization"] == "Bearer test-token"
    assert "Host" not in headers
    assert "host" not in headers
    assert "Content-Length" not in headers
    assert "Connection" not in headers

    await manager.close()


@pytest.mark.asyncio
async def test_drain_worker_incident_id_fallback() -> None:
    """When incident_id is empty, use incoming header if present, else empty string."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    captured_headers: dict[str, str] = {}

    async def fake_request(*_args: object, **kwargs: object) -> httpx.Response:
        headers = kwargs.get("headers", {})
        if isinstance(headers, dict):
            captured_headers.clear()
            captured_headers.update(headers)
        return httpx.Response(200)

    mock_http.request.side_effect = fake_request

    manager = MirrorGatewayManager(client=mock_http)
    # Case 1: incident_id is empty, incoming has no header -> header becomes ""
    manager.register_twin("twin-no-inc", "http://twin:8000", incident_id="")
    manager.dispatch_to_twins(
        MirroredRequest(method="GET", path="/", query="", headers={}, body=b"")
    )
    await asyncio.sleep(0.05)
    assert captured_headers["X-Understudy-Incident"] == ""

    # Case 2: incident_id is empty, incoming header HAS x-understudy-incident -> preserved
    manager.dispatch_to_twins(
        MirroredRequest(
            method="GET",
            path="/",
            query="",
            headers={"X-Understudy-Incident": "incoming-inc-123"},
            body=b"",
        )
    )
    await asyncio.sleep(0.05)
    assert captured_headers["X-Understudy-Incident"] == "incoming-inc-123"

    await manager.close()


@pytest.mark.asyncio
async def test_drain_worker_error_and_timeout_drop_counting() -> None:
    """When twin request times out (>2s) or raises HTTPError, it is counted as dropped."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    manager = MirrorGatewayManager(client=mock_http, worker_timeout_seconds=2.0)
    twin = manager.register_twin("twin-err", "http://twin:8000")

    # 1. Connect error
    mock_http.request.side_effect = httpx.ConnectError("Connection refused")
    manager.dispatch_to_twins(
        MirroredRequest(method="GET", path="/api/fail", query="", headers={}, body=b"")
    )
    await asyncio.sleep(0.05)
    assert twin.dropped == 1
    assert twin.delivered == 0

    # 2. Timeout error
    mock_http.request.side_effect = httpx.TimeoutException("Request timed out after 2.0s")
    manager.dispatch_to_twins(
        MirroredRequest(method="GET", path="/api/timeout", query="", headers={}, body=b"")
    )
    await asyncio.sleep(0.05)
    assert twin.dropped == 2
    assert twin.delivered == 0

    # 3. Successful recovery
    mock_http.request.side_effect = None
    mock_http.request.return_value = httpx.Response(200)
    manager.dispatch_to_twins(
        MirroredRequest(method="GET", path="/api/ok", query="", headers={}, body=b"")
    )
    await asyncio.sleep(0.05)
    assert twin.dropped == 2
    assert twin.delivered == 1

    stats = manager.get_stats("twin-err")
    assert stats is not None
    assert stats.dropped == 2
    assert stats.delivered == 1
    assert pytest.approx(stats.drop_ratio, rel=1e-3) == 2 / 3

    await manager.close()


# ==============================================================================
# FastAPI Application Routes & Proxy Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_reachability_probe() -> None:
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    app.state.http_client = mock_http

    # Reachable
    mock_http.get.return_value = httpx.Response(200)
    assert await check_prod_reachability() is True

    # Unreachable status
    mock_http.get.return_value = httpx.Response(500)
    assert await check_prod_reachability() is False

    # Exception
    mock_http.get.side_effect = httpx.ConnectError("failed")
    assert await check_prod_reachability() is False


@pytest.mark.asyncio
async def test_health_routes(client: httpx.AsyncClient) -> None:
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    app.state.http_client = mock_http
    mock_http.get.return_value = httpx.Response(200)

    resp = await client.get("/healthz")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_twins_registration_api(client: httpx.AsyncClient) -> None:
    # 1. Register twin
    resp = await client.post(
        "/twins",
        json={
            "twin_id": "test-twin-1",
            "base_url": "http://test-twin-1:8000",
            "incident_id": "inc",
        },
    )
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.json() == {
        "status": "registered",
        "twin_id": "test-twin-1",
        "base_url": "http://test-twin-1:8000",
    }

    # 2. Get stats
    resp = await client.get("/twins/test-twin-1/stats")
    assert resp.status_code == status.HTTP_200_OK
    stats = resp.json()
    assert stats["twin_id"] == "test-twin-1"
    assert stats["delivered"] == 0
    assert stats["dropped"] == 0
    assert stats["drop_ratio"] == 0.0

    # 3. Get all twins stats
    resp = await client.get("/twins")
    assert resp.status_code == status.HTTP_200_OK
    all_stats = resp.json()
    assert "test-twin-1" in all_stats

    # 4. Get stats for nonexistent twin -> 404
    resp = await client.get("/twins/nonexistent/stats")
    assert resp.status_code == status.HTTP_404_NOT_FOUND

    # 5. Unregister twin
    resp = await client.delete("/twins/test-twin-1")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == {"status": "unregistered", "twin_id": "test-twin-1"}

    # 6. Unregister nonexistent twin -> 404
    resp = await client.delete("/twins/test-twin-1")
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_proxy_synchronous_forward_get(client: httpx.AsyncClient) -> None:
    """Proxy synchronously forwards GET request to production and returns response."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    app.state.http_client = mock_http

    mock_http.request.return_value = httpx.Response(
        200,
        content=b'{"items": [{"id": 1, "name": "Item 1"}]}',
        headers={"content-type": "application/json", "x-custom": "val"},
    )

    resp = await client.get("/api/items", params={"category": "electronics"})
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"id": 1, "name": "Item 1"}]}
    assert resp.headers["x-custom"] == "val"

    call_args = mock_http.request.call_args
    assert call_args.kwargs["method"] == "GET"
    expected_target = f"{get_target_prod_url()}/api/items?category=electronics"
    assert call_args.kwargs["url"] == expected_target


@pytest.mark.asyncio
async def test_proxy_synchronous_forward_post(client: httpx.AsyncClient) -> None:
    """Proxy synchronously forwards POST request with body and headers."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    app.state.http_client = mock_http

    mock_http.request.return_value = httpx.Response(
        201,
        content=b'{"id": 2, "name": "New Item"}',
        headers={"content-type": "application/json"},
    )

    resp = await client.post(
        "/api/items",
        json={"name": "New Item"},
        headers={"Authorization": "Bearer valid-token"},
    )
    assert resp.status_code == 201
    assert resp.json() == {"id": 2, "name": "New Item"}

    call_args = mock_http.request.call_args
    assert call_args.kwargs["method"] == "POST"
    assert call_args.kwargs["url"] == f"{get_target_prod_url()}/api/items"
    assert call_args.kwargs["content"] == b'{"name":"New Item"}'
    assert call_args.kwargs["headers"]["authorization"] == "Bearer valid-token"


@pytest.mark.asyncio
async def test_proxy_timeout_returns_504(client: httpx.AsyncClient) -> None:
    """Upstream timeout returns 504 Gateway Timeout."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    app.state.http_client = mock_http

    mock_http.request.side_effect = httpx.TimeoutException("Upstream timeout")

    resp = await client.get("/api/items")
    assert resp.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    assert "timed out" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_proxy_connect_error_returns_502(client: httpx.AsyncClient) -> None:
    """Upstream connection error returns 502 Bad Gateway."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    app.state.http_client = mock_http

    mock_http.request.side_effect = httpx.ConnectError("Connection refused")

    resp = await client.get("/api/items")
    assert resp.status_code == status.HTTP_502_BAD_GATEWAY
    assert "unreachable" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_proxy_fans_out_to_registered_twin(client: httpx.AsyncClient) -> None:
    """Incoming request to proxy fans out to registered twins asynchronously."""
    # Register twin
    manager: MirrorGatewayManager = app.state.mirror_manager
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    app.state.http_client = mock_http
    manager.client = mock_http

    captured_urls: list[str] = []

    async def fake_request(*_args: object, **kwargs: object) -> httpx.Response:
        url = str(kwargs.get("url", ""))
        captured_urls.append(url)
        return httpx.Response(200, json={"ok": True})

    mock_http.request.side_effect = fake_request

    twin = manager.register_twin(
        "twin-proxy-test", "http://twin-service:8000", incident_id="inc-42"
    )

    resp = await client.post("/api/items", json={"item": 1})
    assert resp.status_code == 200

    # Allow worker to drain
    await asyncio.sleep(0.05)

    assert twin.delivered == 1
    assert any("http://twin-service:8000/api/items" in u for u in captured_urls)
    assert any(get_target_prod_url() in u for u in captured_urls)

    manager.unregister_twin("twin-proxy-test")


@pytest.mark.asyncio
async def test_drain_worker_cancellation_mid_request() -> None:
    """Cancelling a drain worker while a twin request is in-flight cleans up gracefully."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    in_flight_event = asyncio.Event()

    async def slow_request(*_args: object, **_kwargs: object) -> httpx.Response:
        in_flight_event.set()
        await asyncio.sleep(10.0)
        return httpx.Response(200)

    mock_http.request.side_effect = slow_request
    manager = MirrorGatewayManager(client=mock_http)
    twin = manager.register_twin("twin-cancel", "http://twin:8000")

    manager.dispatch_to_twins(
        MirroredRequest(method="GET", path="/", query="", headers={}, body=b"")
    )
    await in_flight_event.wait()
    assert twin.worker_task is not None
    twin.worker_task.cancel()
    await asyncio.sleep(0.01)
    assert twin.worker_task.done()
    await manager.close()


@pytest.mark.asyncio
async def test_mirror_stats_type_alias() -> None:
    """MirrorStats and MirrorStatsResponse are aliases for the same class."""
    assert MirrorStats is MirrorStatsResponse
    stats = MirrorStats(twin_id="twin-alias", delivered=5, dropped=1, drop_ratio=1 / 6)
    assert isinstance(stats, MirrorStatsResponse)


@pytest.mark.asyncio
async def test_registration_validation_errors(client: httpx.AsyncClient) -> None:
    """Invalid twin_id or base_url payloads are rejected with 422 Unprocessable Entity."""
    # 1. Empty twin_id
    resp = await client.post("/twins", json={"twin_id": "", "base_url": "http://twin:8000"})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    # 1b. Non-string twin_id
    resp = await client.post("/twins", json={"twin_id": 123, "base_url": "http://twin:8000"})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    # 2. Whitespace twin_id
    resp = await client.post("/twins", json={"twin_id": "   ", "base_url": "http://twin:8000"})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    # 3. Invalid characters in twin_id
    resp = await client.post(
        "/twins", json={"twin_id": "twin!invalid", "base_url": "http://twin:8000"}
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    # 4. Empty base_url
    resp = await client.post("/twins", json={"twin_id": "valid-twin", "base_url": ""})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    # 4b. Whitespace base_url
    resp = await client.post("/twins", json={"twin_id": "valid-twin", "base_url": "   "})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    # 4c. Non-string base_url
    resp = await client.post("/twins", json={"twin_id": "valid-twin", "base_url": 123})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    # 5. Invalid scheme in base_url
    resp = await client.post(
        "/twins", json={"twin_id": "valid-twin", "base_url": "ftp://twin:8000"}
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    # 6. Missing host in base_url
    resp = await client.post("/twins", json={"twin_id": "valid-twin", "base_url": "http://"})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    # 6b. Non-string incident_id
    resp = await client.post(
        "/twins",
        json={"twin_id": "valid-twin", "base_url": "http://twin:8000", "incident_id": 123},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    # 7. Valid registration with trailing slashes is normalized
    resp = await client.post(
        "/twins",
        json={
            "twin_id": "norm-twin",
            "base_url": "http://norm-twin:8000///",
            "incident_id": "  inc-norm  ",
        },
    )
    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()
    assert data["status"] == "registered"
    assert data["twin_id"] == "norm-twin"
    assert data["base_url"] == "http://norm-twin:8000"

    # Cleanup
    del_resp = await client.delete("/twins/norm-twin")
    assert del_resp.status_code == status.HTTP_200_OK


@pytest.mark.asyncio
async def test_manager_register_twin_defensive_errors() -> None:
    """MirrorGatewayManager raises ValueError on empty or whitespace twin_id or base_url."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    manager = MirrorGatewayManager(client=mock_http)

    with pytest.raises(ValueError, match="twin_id cannot be empty"):
        manager.register_twin("", "http://twin:8000")

    with pytest.raises(ValueError, match="twin_id cannot be empty"):
        manager.register_twin("   ", "http://twin:8000")

    with pytest.raises(ValueError, match="base_url cannot be empty"):
        manager.register_twin("valid-id", "")

    with pytest.raises(ValueError, match="base_url cannot be empty"):
        manager.register_twin("valid-id", "   ")

    await manager.close()


@pytest.mark.asyncio
async def test_mirror_stats_contract_compatibility(client: httpx.AsyncClient) -> None:
    """Gateway MirrorStats JSON response is deserializable by
    understudy.contracts.twin.MirrorStats.
    """
    from understudy.contracts.twin import MirrorStats as ContractMirrorStats

    reg_resp = await client.post(
        "/twins",
        json={"twin_id": "compat-twin", "base_url": "http://compat:8000"},
    )
    assert reg_resp.status_code == status.HTTP_201_CREATED

    stats_resp = await client.get("/twins/compat-twin/stats")
    assert stats_resp.status_code == status.HTTP_200_OK

    contract_stats = ContractMirrorStats.model_validate_json(stats_resp.text)
    assert contract_stats.twin_id == "compat-twin"
    assert contract_stats.delivered == 0
    assert contract_stats.dropped == 0
    assert contract_stats.drop_ratio == 0.0

    await client.delete("/twins/compat-twin")


@pytest.mark.asyncio
async def test_dispatch_concurrent_with_unregister() -> None:
    """Dispatching to twins while an unregister occurs does not raise dictionary mutation errors."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    manager = MirrorGatewayManager(client=mock_http)
    manager.register_twin("twin-c1", "http://c1:8000")
    manager.register_twin("twin-c2", "http://c2:8000")

    req = MirroredRequest(method="GET", path="/test", query="", headers={}, body=b"")

    # Concurrent unregister and dispatch
    manager.dispatch_to_twins(req)
    manager.unregister_twin("twin-c1")
    manager.dispatch_to_twins(req)

    assert "twin-c1" not in manager.registered_twins
    assert "twin-c2" in manager.registered_twins

    await manager.close()
