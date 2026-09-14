"""Unit tests for HttpMirrorRegistry client in src/understudy/mirror/registry.py.

Guarantees 100% line and branch coverage:
- MirrorRegistry protocol conformance
- Twin registration, URL synthesis, and validation
- Idempotent and strict twin unregistration
- Stats fetching and error mapping (TwinNotFoundError, MirrorError)
- Gateway health and readiness checks
- Client lifecycle, settings resolution, and context manager
- In-memory integration against the real FastAPI mirror-gateway app via ASGITransport
"""

from datetime import UTC, datetime

import httpx
import pytest

from services.mirror_gateway.app import app, lifespan
from understudy.common.config import Settings
from understudy.common.errors import MirrorError, TwinNotFoundError
from understudy.contracts.twin import MirrorStats, TwinHandle
from understudy.mirror.api import MirrorRegistry
from understudy.mirror.registry import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_TWIN_BASE_URL_TEMPLATE,
    HttpMirrorRegistry,
)


def _make_sample_twin(
    twin_id: str = "twin_inc_1_0",
    incident_id: str = "inc_1",
    candidate_index: int = 0,
    namespace: str = "ust-twin-inc-1-0",
) -> TwinHandle:
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    return TwinHandle(
        twin_id=twin_id,
        incident_id=incident_id,
        candidate_index=candidate_index,
        namespace=namespace,
        database=f"twin_{incident_id}_{candidate_index}",
        forked_from_snapshot_at=now,
        ready_at=now,
        state="ready",
    )


def test_protocol_conformance() -> None:
    """Verify that HttpMirrorRegistry adheres to the MirrorRegistry Protocol."""
    client = HttpMirrorRegistry()
    assert isinstance(client, MirrorRegistry)
    assert issubclass(HttpMirrorRegistry, MirrorRegistry)


def test_initialization_defaults_and_settings() -> None:
    """Verify base URL resolution and default parameters."""
    reg = HttpMirrorRegistry()
    assert reg.base_url == "http://localhost:8080"
    assert reg.timeout_seconds == DEFAULT_HTTP_TIMEOUT_SECONDS
    assert reg.twin_base_url_template == DEFAULT_TWIN_BASE_URL_TEMPLATE

    custom_settings = Settings.model_validate(
        {"endpoints": {"mirror_gateway": "http://custom-gateway:9999/"}}
    )
    reg2 = HttpMirrorRegistry(settings=custom_settings)
    assert reg2.base_url == "http://custom-gateway:9999"


def test_build_twin_base_url() -> None:
    """Verify URL synthesis from template and TwinHandle."""
    reg = HttpMirrorRegistry()
    twin = _make_sample_twin(namespace="ust-twin-custom-ns")
    url = reg.build_twin_base_url(twin)
    assert url == "http://edge-gateway.ust-twin-custom-ns:8080"

    custom_reg = HttpMirrorRegistry(
        twin_base_url_template="https://{namespace}.internal.corp:{candidate_index}000"
    )
    assert custom_reg.build_twin_base_url(twin) == "https://ust-twin-custom-ns.internal.corp:0000"


@pytest.mark.asyncio
async def test_register_validation_errors() -> None:
    """Verify registration rejects empty twin_id or base_url."""
    reg = HttpMirrorRegistry()
    with pytest.raises(MirrorError, match="twin_id cannot be empty"):
        await reg.register("", "http://target:8080")

    with pytest.raises(MirrorError, match="twin_id cannot be empty"):
        await reg.register("   ", "http://target:8080")

    with pytest.raises(MirrorError, match="base_url cannot be empty"):
        await reg.register("twin-1", "")

    with pytest.raises(MirrorError, match="base_url cannot be empty"):
        await reg.register("twin-1", "   ")


@pytest.mark.asyncio
async def test_register_twin_success_and_failures() -> None:
    """Verify register_twin handles success, server errors, and network errors."""
    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        if "network-fail" in str(request.url):
            raise httpx.ConnectError("Connection refused", request=request)
        if "500-fail" in str(request.url):
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(
            201,
            json={"status": "registered", "twin_id": "twin-1", "base_url": "http://edge:8080"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        # 1. Success with default URL synthesis
        reg = HttpMirrorRegistry(base_url="http://mock-gw", client=http_client)
        twin = _make_sample_twin(twin_id="twin_1")
        await reg.register_twin(twin)

        assert len(recorded_requests) == 1
        req = recorded_requests[0]
        assert req.method == "POST"
        assert req.url == "http://mock-gw/twins"
        assert b'"twin_id":"twin_1"' in req.content
        assert b'"base_url":"http://edge-gateway.ust-twin-inc-1-0:8080"' in req.content

        # 2. Success with explicit base_url override
        await reg.register_twin(twin, base_url="http://override-dest:9000")
        assert len(recorded_requests) == 2
        assert b'"base_url":"http://override-dest:9000"' in recorded_requests[1].content

        # 3. Server rejection (500)
        reg_error = HttpMirrorRegistry(base_url="http://mock-gw/500-fail", client=http_client)
        with pytest.raises(MirrorError, match="Mirror gateway rejected registration"):
            await reg_error.register_twin(twin)

        # 4. Connection failure
        reg_net = HttpMirrorRegistry(base_url="http://mock-gw/network-fail", client=http_client)
        with pytest.raises(MirrorError, match="Failed to connect to mirror gateway"):
            await reg_net.register_twin(twin)


@pytest.mark.asyncio
async def test_unregister_twin_scenarios() -> None:
    """Verify unregister_twin handles 200, 404 (idempotent vs strict), 500, and network error."""
    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        path = request.url.path
        if "network-fail" in str(request.url):
            raise httpx.ConnectTimeout("Timed out", request=request)
        if "500-fail" in str(request.url):
            return httpx.Response(500, text="Internal Crash")
        if "not-found" in path:
            return httpx.Response(404, json={"detail": "Not found"})
        return httpx.Response(200, json={"status": "unregistered", "twin_id": "twin-1"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        reg = HttpMirrorRegistry(base_url="http://mock-gw", client=http_client)

        # Validation: empty twin_id
        with pytest.raises(MirrorError, match="twin_id cannot be empty"):
            await reg.unregister_twin("  ")

        # 1. Successful unregister
        await reg.unregister_twin("twin-1")
        assert recorded_requests[-1].method == "DELETE"
        assert recorded_requests[-1].url.path == "/twins/twin-1"

        # 2. 404 response default (idempotent no-op)
        await reg.unregister_twin("not-found-twin")
        assert recorded_requests[-1].url.path == "/twins/not-found-twin"

        # 3. 404 response with raise_if_not_found=True
        with pytest.raises(TwinNotFoundError, match="Twin 'not-found-twin' is not registered"):
            await reg.unregister_twin("not-found-twin", raise_if_not_found=True)

        # 4. 500 server error
        reg_err = HttpMirrorRegistry(base_url="http://mock-gw/500-fail", client=http_client)
        with pytest.raises(MirrorError, match="unregistration failed"):
            await reg_err.unregister_twin("twin-1")

        # 5. Network error
        reg_net = HttpMirrorRegistry(base_url="http://mock-gw/network-fail", client=http_client)
        with pytest.raises(MirrorError, match="Failed to connect to mirror gateway"):
            await reg_net.unregister_twin("twin-1")


@pytest.mark.asyncio
async def test_get_stats_scenarios() -> None:
    """Verify get_stats handles 200, 404, 500, malformed JSON, and network errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "network-fail" in str(request.url):
            raise httpx.ReadError("Socket drop", request=request)
        if "500-fail" in str(request.url):
            return httpx.Response(500, text="Internal Error")
        if "malformed" in str(request.url):
            return httpx.Response(200, text="not json {")
        if "bad-types" in str(request.url):
            return httpx.Response(200, json={"twin_id": "t1", "delivered": "not_int"})
        if "not-found" in str(request.url):
            return httpx.Response(404, json={"detail": "Twin not found"})
        return httpx.Response(
            200,
            json={"twin_id": "twin-1", "delivered": 100, "dropped": 5, "drop_ratio": 5 / 105},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        reg = HttpMirrorRegistry(base_url="http://mock-gw", client=http_client)

        # Validation: empty twin_id
        with pytest.raises(MirrorError, match="twin_id cannot be empty"):
            await reg.get_stats("  ")

        # 1. Successful stats
        stats = await reg.get_stats("twin-1")
        assert isinstance(stats, MirrorStats)
        assert stats.twin_id == "twin-1"
        assert stats.delivered == 100
        assert stats.dropped == 5
        assert pytest.approx(stats.drop_ratio, rel=1e-4) == 5 / 105

        # 2. 404 Not Found
        with pytest.raises(TwinNotFoundError, match="Twin 'not-found' is not registered"):
            await reg.get_stats("not-found")

        # 3. 500 error
        reg_500 = HttpMirrorRegistry(base_url="http://mock-gw/500-fail", client=http_client)
        with pytest.raises(MirrorError, match="Failed to fetch stats"):
            await reg_500.get_stats("twin-1")

        # 4. Malformed JSON or types
        reg_malformed = HttpMirrorRegistry(base_url="http://mock-gw/malformed", client=http_client)
        with pytest.raises(MirrorError, match="Malformed statistics payload"):
            await reg_malformed.get_stats("twin-1")

        reg_bad_types = HttpMirrorRegistry(base_url="http://mock-gw/bad-types", client=http_client)
        with pytest.raises(MirrorError, match="Malformed statistics payload"):
            await reg_bad_types.get_stats("twin-1")

        # 5. Network error
        reg_net = HttpMirrorRegistry(base_url="http://mock-gw/network-fail", client=http_client)
        with pytest.raises(MirrorError, match="Failed to connect to mirror gateway"):
            await reg_net.get_stats("twin-1")


@pytest.mark.asyncio
async def test_get_all_stats_scenarios() -> None:
    """Verify get_all_stats handles batch retrieval, malformed data, and errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "network-fail" in str(request.url):
            raise httpx.RequestError("Network dead", request=request)
        if "500-fail" in str(request.url):
            return httpx.Response(500, text="Gateway failure")
        if "non-dict" in str(request.url):
            return httpx.Response(200, json=["not", "a", "dict"])
        if "corrupt-json" in str(request.url):
            return httpx.Response(200, text="{broken")
        return httpx.Response(
            200,
            json={
                "t1": {"twin_id": "t1", "delivered": 50, "dropped": 2},
                "t2": {"twin_id": "t2", "delivered": 60, "dropped": 0},
                "ignored_non_dict": "not_an_object",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        reg = HttpMirrorRegistry(base_url="http://mock-gw", client=http_client)

        # 1. Success
        all_stats = await reg.get_all_stats()
        assert len(all_stats) == 2
        assert all_stats["t1"].delivered == 50
        assert all_stats["t1"].dropped == 2
        assert all_stats["t2"].delivered == 60
        assert all_stats["t2"].dropped == 0

        # 2. 500 error
        reg_500 = HttpMirrorRegistry(base_url="http://mock-gw/500-fail", client=http_client)
        with pytest.raises(MirrorError, match="Failed to fetch all twin statistics"):
            await reg_500.get_all_stats()

        # 3. Non-dict JSON response
        reg_non_dict = HttpMirrorRegistry(base_url="http://mock-gw/non-dict", client=http_client)
        with pytest.raises(MirrorError, match="Malformed statistics response"):
            await reg_non_dict.get_all_stats()

        # 4. Corrupt JSON
        reg_corrupt = HttpMirrorRegistry(base_url="http://mock-gw/corrupt-json", client=http_client)
        with pytest.raises(MirrorError, match="Malformed statistics response"):
            await reg_corrupt.get_all_stats()

        # 5. Network failure
        reg_net = HttpMirrorRegistry(base_url="http://mock-gw/network-fail", client=http_client)
        with pytest.raises(MirrorError, match="Failed to connect to mirror gateway"):
            await reg_net.get_all_stats()


@pytest.mark.asyncio
async def test_health_and_ready_checks() -> None:
    """Verify health and ready probes."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/healthz" in path:
            if "fail" in str(request.url):
                return httpx.Response(500, text="Health down")
            if "net-error" in str(request.url):
                raise httpx.ConnectError("Down", request=request)
            return httpx.Response(200, text="OK")
        if "/readyz" in path:
            if "unready" in str(request.url):
                return httpx.Response(503, text="Not ready")
            if "net-error" in str(request.url):
                raise httpx.ConnectError("Down", request=request)
            return httpx.Response(200, text="Ready")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        reg_ok = HttpMirrorRegistry(base_url="http://mock-gw", client=http_client)
        assert await reg_ok.health() is True
        assert await reg_ok.ready() is True

        reg_fail = HttpMirrorRegistry(base_url="http://mock-gw/fail", client=http_client)
        assert await reg_fail.health() is False

        reg_unready = HttpMirrorRegistry(base_url="http://mock-gw/unready", client=http_client)
        assert await reg_unready.ready() is False

        reg_net = HttpMirrorRegistry(base_url="http://mock-gw/net-error", client=http_client)
        assert await reg_net.health() is False
        assert await reg_net.ready() is False


@pytest.mark.asyncio
async def test_client_lifecycle_and_context_manager() -> None:
    """Verify client lazy instantiation, context manager, and clean closure."""
    reg = HttpMirrorRegistry(base_url="http://localhost:8080")
    assert reg._client is None

    async with reg as client_reg:
        assert client_reg._client is not None
        assert not client_reg._client.is_closed

    assert reg._client is None

    # Idempotent aclose
    await reg.aclose()
    assert reg._client is None

    # External client should not be closed by aclose
    external = httpx.AsyncClient()
    external_reg = HttpMirrorRegistry(client=external)
    await external_reg.aclose()
    assert not external.is_closed
    await external.aclose()


@pytest.mark.asyncio
async def test_in_memory_integration_against_mirror_gateway_app() -> None:
    """End-to-end contract validation against real FastAPI mirror-gateway app via ASGITransport."""
    async with (
        lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as test_client,
    ):
        registry = HttpMirrorRegistry(base_url="http://test", client=test_client)

        # 1. Health check
        assert await registry.health() is True

        # 2. Register two twins
        twin0 = _make_sample_twin(twin_id="twin-int-0", candidate_index=0)
        twin1 = _make_sample_twin(twin_id="twin-int-1", candidate_index=1)

        await registry.register_twin(twin0, base_url="http://twin-int-0:8000")
        await registry.register_twin(twin1, base_url="http://twin-int-1:8000")

        # 3. Verify get_stats
        s0 = await registry.get_stats("twin-int-0")
        assert s0.twin_id == "twin-int-0"
        assert s0.delivered == 0
        assert s0.dropped == 0
        assert s0.drop_ratio == 0.0

        # 4. Verify get_all_stats
        all_s = await registry.get_all_stats()
        assert "twin-int-0" in all_s
        assert "twin-int-1" in all_s

        # 5. Unregister one twin
        await registry.unregister_twin("twin-int-0")

        # 6. Verify 404 on deleted twin
        with pytest.raises(TwinNotFoundError):
            await registry.get_stats("twin-int-0")

        # 7. Unregister remaining twin
        await registry.unregister_twin("twin-int-1")
        all_after = await registry.get_all_stats()
        assert len(all_after) == 0


@pytest.mark.asyncio
async def test_get_prod_stats_and_error_handling() -> None:
    """Verify get_prod_stats success and error branches."""

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "network-fail" in url_str:
            raise httpx.RequestError("No network", request=request)
        if "500-fail" in url_str:
            return httpx.Response(500, text="Internal Error")
        if "non-dict" in url_str:
            return httpx.Response(200, json="string_not_dict")
        if "corrupt-json" in url_str:
            return httpx.Response(200, text="not-json")
        return httpx.Response(
            200,
            json={"delivered": 100, "paths": {"/api/items": 80, "/healthz": 20}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        reg = HttpMirrorRegistry(base_url="http://mock-gw", client=client)
        stats = await reg.get_prod_stats()
        assert stats["delivered"] == 100
        assert stats["paths"]["/api/items"] == 80

        # Network error
        reg_net = HttpMirrorRegistry(base_url="http://mock-gw/network-fail", client=client)
        with pytest.raises(MirrorError, match="Failed to connect"):
            await reg_net.get_prod_stats()

        # 500 error
        reg_500 = HttpMirrorRegistry(base_url="http://mock-gw/500-fail", client=client)
        with pytest.raises(MirrorError, match="Failed to fetch production statistics"):
            await reg_500.get_prod_stats()

        # Non-dict
        reg_non_dict = HttpMirrorRegistry(base_url="http://mock-gw/non-dict", client=client)
        with pytest.raises(MirrorError, match="Malformed production statistics"):
            await reg_non_dict.get_prod_stats()

        # Corrupt JSON
        reg_corrupt = HttpMirrorRegistry(base_url="http://mock-gw/corrupt-json", client=client)
        with pytest.raises(MirrorError, match="Malformed production statistics"):
            await reg_corrupt.get_prod_stats()


@pytest.mark.asyncio
async def test_get_twin_paths_and_error_handling() -> None:
    """Verify get_twin_paths success, 404, 500, and malformed branches."""

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "network-fail" in url_str:
            raise httpx.RequestError("No network", request=request)
        if "404-fail" in url_str:
            return httpx.Response(404, text="Not Found")
        if "500-fail" in url_str:
            return httpx.Response(500, text="Server Error")
        if "corrupt-json" in url_str:
            return httpx.Response(200, text="[broken")
        return httpx.Response(200, json={"twin_id": "twin-1", "paths": {"/api/items": 50}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        reg = HttpMirrorRegistry(base_url="http://mock-gw", client=client)
        paths = await reg.get_twin_paths("twin-1")
        assert paths["/api/items"] == 50

        # 404
        reg_404 = HttpMirrorRegistry(base_url="http://mock-gw/404-fail", client=client)
        with pytest.raises(TwinNotFoundError):
            await reg_404.get_twin_paths("twin-1")

        # 500
        reg_500 = HttpMirrorRegistry(base_url="http://mock-gw/500-fail", client=client)
        with pytest.raises(MirrorError, match="Failed to fetch twin stats"):
            await reg_500.get_twin_paths("twin-1")

        # Network
        reg_net = HttpMirrorRegistry(base_url="http://mock-gw/network-fail", client=client)
        with pytest.raises(MirrorError, match="Failed to connect"):
            await reg_net.get_twin_paths("twin-1")

        # Corrupt
        reg_corrupt = HttpMirrorRegistry(base_url="http://mock-gw/corrupt-json", client=client)
        with pytest.raises(MirrorError, match="Malformed stats payload"):
            await reg_corrupt.get_twin_paths("twin-1")


@pytest.mark.asyncio
async def test_get_fidelity_reports() -> None:
    """Verify get_fidelity_reports fetches prod and twin paths and computes reports."""

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/prod/stats" in url_str:
            return httpx.Response(200, json={"delivered": 100, "paths": {"/api/items": 100}})
        if request.url.path == "/twins":
            return httpx.Response(
                200,
                json={
                    "twin_inc_1_0": {"twin_id": "twin_inc_1_0", "delivered": 100, "dropped": 0},
                    "twin_other_0": {"twin_id": "twin_other_0", "delivered": 100, "dropped": 0},
                },
            )
        if "/twins/twin_inc_1_0/stats" in url_str:
            return httpx.Response(
                200,
                json={"twin_id": "twin_inc_1_0", "paths": {"/api/items": 100}},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        reg = HttpMirrorRegistry(base_url="http://mock-gw", client=client)
        reports = await reg.get_fidelity_reports("inc_1")
        assert len(reports) == 1
        assert reports[0].twin_id == "twin_inc_1_0"
        assert reports[0].status == "OK"
        assert reports[0].path_distribution_match is True

        # Non-matching incident returns empty list
        empty_reports = await reg.get_fidelity_reports("nonexistent")
        assert empty_reports == []


@pytest.mark.asyncio
async def test_get_fidelity_reports_fallbacks() -> None:
    """Verify get_fidelity_reports falls back gracefully when prod or twin paths fail."""

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/prod/stats" in url_str:
            return httpx.Response(500, text="Internal Error")
        if request.url.path == "/twins":
            return httpx.Response(
                200,
                json={
                    "twin_inc_2_0": {"twin_id": "twin_inc_2_0", "delivered": 100, "dropped": 0},
                },
            )
        if "/twins/twin_inc_2_0/stats" in url_str:
            return httpx.Response(500, text="Internal Error")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        reg = HttpMirrorRegistry(base_url="http://mock-gw", client=client)
        reports = await reg.get_fidelity_reports("inc_2")
        assert len(reports) == 1
        assert reports[0].twin_id == "twin_inc_2_0"
        assert reports[0].status == "OK"
