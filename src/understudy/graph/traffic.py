"""Telemetry traffic observer querying Prometheus for observed service interactions."""

from typing import Any

import httpx

from understudy.common.config import get_settings
from understudy.common.errors import GraphError
from understudy.common.logging import get_logger
from understudy.graph.models import ObservedTraffic

logger = get_logger(__name__)


class TrafficObserver:
    """Queries Prometheus to inspect observed service calls, request rates, and edge topology."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.endpoints.prometheus).rstrip("/")
        self.timeout = timeout
        self._custom_client = client
        self._owned_client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._custom_client is not None:
            return self._custom_client
        if self._owned_client is None or self._owned_client.is_closed:
            self._owned_client = httpx.AsyncClient(timeout=self.timeout)
        return self._owned_client

    async def close(self) -> None:
        """Close underlying HTTP client if owned."""
        if self._owned_client is not None and not self._owned_client.is_closed:
            await self._owned_client.aclose()

    async def observe(self, namespace: str = "ust-prod") -> ObservedTraffic:
        """Query Prometheus for http_requests_total in namespace and extract observed topology."""
        client = await self._get_client()
        url = f"{self.base_url}/api/v1/query"
        query_expr = f'http_requests_total{{namespace="{namespace}"}}'

        try:
            resp = await client.get(url, params={"query": query_expr}, timeout=self.timeout)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        except Exception as exc:
            raise GraphError(
                f"Failed to query Prometheus traffic metrics: {exc}",
                details={"url": url, "namespace": namespace},
            ) from exc

        if data.get("status") != "success":
            err_msg = data.get("error", "Unknown Prometheus query error")
            raise GraphError(
                f"Prometheus returned error status: {err_msg}",
                details={"data": data},
            )

        results = data.get("data", {}).get("result", [])
        if not isinstance(results, list):
            results = []

        observed_services: set[str] = set()
        observed_edges: set[tuple[str, str]] = set()
        request_counts: dict[str, float] = {}

        for item in results:
            if not isinstance(item, dict):
                continue
            metric = item.get("metric", {})
            if not isinstance(metric, dict):
                continue

            service = metric.get("service") or metric.get("app")
            if not service or not isinstance(service, str):
                continue

            observed_services.add(service)

            # Accumulate request counts
            val_pair = item.get("value")
            val_num = 0.0
            if isinstance(val_pair, list | tuple) and len(val_pair) >= 2:
                try:
                    val_num = float(val_pair[1])
                except (ValueError, TypeError):
                    val_num = 0.0
            request_counts[service] = request_counts.get(service, 0.0) + val_num

            # Extract caller-to-service edges from explicit labels if present
            caller = metric.get("caller") or metric.get("client") or metric.get("source")
            if caller and isinstance(caller, str) and caller != service:
                observed_edges.add((caller, service))

            # Endpoint-based call correlation for demo services
            endpoint = metric.get("endpoint")
            if service == "auth-service" and endpoint == "/validate":
                observed_edges.add(("edge-gateway", "auth-service"))
            elif service == "data-service" and endpoint in (
                "/items",
                "/items/{item_id}",
                "/api/items",
            ):
                observed_edges.add(("edge-gateway", "data-service"))

        # Worker interacts with data-service/postgres; if worker and data-service both active
        if "worker" in observed_services and "data-service" in observed_services:
            observed_edges.add(("worker", "data-service"))

        total_requests = sum(request_counts.values())

        return ObservedTraffic(
            services=observed_services,
            edges=observed_edges,
            request_counts=request_counts,
            total_requests=total_requests,
        )
