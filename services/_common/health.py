"""Health and readiness endpoint setup for FastAPI demo services."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Response, status


def setup_health_routes(
    app: FastAPI,
    readiness_checks: list[Callable[[], Awaitable[bool]]] | None = None,
) -> None:
    """Register standard /healthz and /readyz endpoints on a FastAPI application."""
    checks = readiness_checks or []

    @app.get("/healthz", tags=["Health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["Health"])
    async def readyz(response: Response) -> dict[str, Any]:
        if not checks:
            return {"status": "ready"}

        results = await asyncio.gather(*(check() for check in checks), return_exceptions=True)
        failures = [
            str(err) if isinstance(err, Exception) else "failed"
            for err in results
            if isinstance(err, Exception) or err is not True
        ]

        if failures:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unready", "failures": failures}

        return {"status": "ready"}
