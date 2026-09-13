"""FastAPI application for the mirror gateway service.

Conforms to ADR-012, ADR-013, and build-plan step A3.1:
- Synchronous proxy to ust-prod edge-gateway
- Per-twin bounded asyncio.Queue(maxsize=1000)
- Dedicated worker task per twin draining its queue with a 2s per-request timeout
- Drop-and-count on full queue and worker error/timeout
- Shadow headers injected:
  X-Understudy-Shadow: 1, X-Understudy-Twin: <twin_id>, X-Understudy-Incident: <id>
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel

from services._common.health import setup_health_routes
from services._common.logging import get_logger
from services._common.metrics import setup_metrics
from services._common.settings import get_services_settings
from services.mirror_gateway.core import (
    MirroredRequest,
    MirrorGatewayManager,
    MirrorStatsResponse,
)

logger = get_logger(__name__)


class TwinRegistrationRequest(BaseModel):
    """Payload for registering a twin with the mirror gateway."""

    twin_id: str
    base_url: str
    incident_id: str = ""


def get_target_prod_url() -> str:
    """Return configured target production URL."""
    return get_services_settings().mirror_gateway.target_prod_url.rstrip("/")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage lifecycle of shared HTTP client and MirrorGatewayManager."""
    settings = get_services_settings().mirror_gateway
    client = httpx.AsyncClient(timeout=settings.http_timeout_seconds)
    manager = MirrorGatewayManager(
        client=client,
        queue_maxsize=settings.queue_maxsize,
        worker_timeout_seconds=settings.worker_timeout_seconds,
    )
    app.state.http_client = client
    app.state.mirror_manager = manager

    try:
        yield
    finally:
        await manager.close()
        await client.aclose()


app = FastAPI(title="mirror-gateway", lifespan=lifespan)
setup_metrics(app, "mirror-gateway")


async def check_prod_reachability() -> bool:
    """Readiness probe checking connectivity to production edge-gateway."""
    try:
        client: httpx.AsyncClient = app.state.http_client
        resp = await client.get(f"{get_target_prod_url()}/healthz")
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


setup_health_routes(app, [check_prod_reachability])


@app.post(
    "/twins",
    status_code=status.HTTP_201_CREATED,
    tags=["Twins"],
    summary="Register a twin for mirrored traffic fan-out",
)
async def register_twin(payload: TwinRegistrationRequest) -> dict[str, str]:
    """Register an active twin destination with the gateway."""
    manager: MirrorGatewayManager = app.state.mirror_manager
    manager.register_twin(
        twin_id=payload.twin_id,
        base_url=payload.base_url,
        incident_id=payload.incident_id,
    )
    return {
        "status": "registered",
        "twin_id": payload.twin_id,
        "base_url": payload.base_url,
    }


@app.delete(
    "/twins/{twin_id}",
    status_code=status.HTTP_200_OK,
    tags=["Twins"],
    summary="Unregister a twin from mirrored traffic fan-out",
)
async def unregister_twin(twin_id: str) -> dict[str, str]:
    """Unregister a twin from the gateway and cancel its drain worker."""
    manager: MirrorGatewayManager = app.state.mirror_manager
    unregistered = manager.unregister_twin(twin_id)
    if not unregistered:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Twin {twin_id} is not registered",
        )
    return {"status": "unregistered", "twin_id": twin_id}


@app.get(
    "/twins/{twin_id}/stats",
    response_model=MirrorStatsResponse,
    tags=["Twins"],
    summary="Fetch delivery and drop metrics for a twin",
)
async def get_twin_stats(twin_id: str) -> MirrorStatsResponse:
    """Fetch delivery and drop metrics for a registered twin."""
    manager: MirrorGatewayManager = app.state.mirror_manager
    stats = manager.get_stats(twin_id)
    if stats is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Twin {twin_id} is not registered",
        )
    return stats


@app.get(
    "/twins",
    response_model=dict[str, MirrorStatsResponse],
    tags=["Twins"],
    summary="Fetch delivery and drop metrics for all registered twins",
)
async def get_all_twins_stats() -> dict[str, MirrorStatsResponse]:
    """Fetch delivery and drop metrics for all registered twins."""
    manager: MirrorGatewayManager = app.state.mirror_manager
    return manager.get_all_stats()


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    tags=["Proxy"],
    summary="Synchronous proxy to production with fan-out to twins",
)
async def proxy_request(request: Request, path: str) -> Response:
    """Proxy request synchronously to production, fanning out asynchronously to twins."""
    body = await request.body()

    # Filter hop-by-hop headers for forwarding
    headers = dict(request.headers)
    for h in ["host", "content-length", "connection", "transfer-encoding"]:
        for k in list(headers.keys()):
            if k.lower() == h:
                del headers[k]

    # Asynchronous fan-out to registered twins (fire-and-forget, never blocks prod)
    mirrored = MirroredRequest(
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        headers=headers,
        body=body,
    )
    manager: MirrorGatewayManager = app.state.mirror_manager
    manager.dispatch_to_twins(mirrored)

    # Synchronous forward to production
    prod_base = get_target_prod_url()
    target_url = f"{prod_base}/{path.lstrip('/')}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    client: httpx.AsyncClient = app.state.http_client
    settings = get_services_settings().mirror_gateway
    try:
        prod_resp = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
            timeout=settings.http_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Production target timed out: {exc}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Production target unreachable: {exc}",
        ) from exc

    # Filter hop-by-hop response headers
    resp_headers = dict(prod_resp.headers)
    for h in ["connection", "transfer-encoding", "content-encoding", "content-length"]:
        for k in list(resp_headers.keys()):
            if k.lower() == h:
                del resp_headers[k]

    return Response(
        content=prod_resp.content,
        status_code=prod_resp.status_code,
        headers=resp_headers,
        media_type=prod_resp.headers.get("content-type"),
    )
