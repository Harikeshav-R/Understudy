"""Edge gateway demo service: public HTTP entry point fanning out to auth and data services."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Response, status

from services._common import (
    FaultManager,
    check_twin_outbound_target,
    setup_fault_middleware,
    setup_fault_routes,
    setup_health_routes,
    setup_metrics,
)

AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://auth-service:8000").rstrip("/")
DATA_SERVICE_URL = os.getenv("DATA_SERVICE_URL", "http://data-service:8000").rstrip("/")
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "5.0"))

fault_manager = FaultManager()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage lifecycle of shared HTTP client."""
    app.state.http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(title="edge-gateway", lifespan=lifespan)
setup_metrics(app, "edge-gateway")
setup_fault_middleware(app, fault_manager)
setup_fault_routes(app, fault_manager)


async def check_auth_reachability() -> bool:
    """Readiness probe for auth-service reachability."""
    check_twin_outbound_target(AUTH_SERVICE_URL)
    try:
        client: httpx.AsyncClient = app.state.http_client
        resp = await client.get(f"{AUTH_SERVICE_URL}/healthz")
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def check_data_reachability() -> bool:
    """Readiness probe for data-service reachability."""
    check_twin_outbound_target(DATA_SERVICE_URL)
    try:
        client: httpx.AsyncClient = app.state.http_client
        resp = await client.get(f"{DATA_SERVICE_URL}/healthz")
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


setup_health_routes(app, [check_auth_reachability, check_data_reachability])


@app.get("/api/items", tags=["Items"])
async def get_items(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Validate user token with auth-service and retrieve item catalogue from data-service."""
    check_twin_outbound_target(AUTH_SERVICE_URL)
    check_twin_outbound_target(DATA_SERVICE_URL)
    client: httpx.AsyncClient = app.state.http_client

    # 1. Authenticate request
    try:
        auth_resp = await client.get(
            f"{AUTH_SERVICE_URL}/validate",
            headers={"Authorization": authorization or ""},
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Auth service unavailable: {exc}",
        ) from exc

    if auth_resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
        )

    # 2. Query items from data service
    try:
        data_resp = await client.get(f"{DATA_SERVICE_URL}/items")
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Data service unavailable: {exc}",
        ) from exc

    if data_resp.status_code != 200:
        raise HTTPException(
            status_code=data_resp.status_code,
            detail=data_resp.text,
        )

    items: dict[str, Any] = data_resp.json()
    return items


@app.post("/api/items", tags=["Items"], status_code=status.HTTP_201_CREATED)
async def create_item(
    payload: dict[str, Any],
    response: Response,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Validate token and forward item creation to data-service."""
    check_twin_outbound_target(AUTH_SERVICE_URL)
    check_twin_outbound_target(DATA_SERVICE_URL)
    client: httpx.AsyncClient = app.state.http_client

    # 1. Authenticate request
    try:
        auth_resp = await client.get(
            f"{AUTH_SERVICE_URL}/validate",
            headers={"Authorization": authorization or ""},
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Auth service unavailable: {exc}",
        ) from exc

    if auth_resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
        )

    # 2. Post item to data service
    try:
        data_resp = await client.post(f"{DATA_SERVICE_URL}/items", json=payload)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Data service unavailable: {exc}",
        ) from exc

    response.status_code = data_resp.status_code
    created_item: dict[str, Any] = data_resp.json()
    return created_item
