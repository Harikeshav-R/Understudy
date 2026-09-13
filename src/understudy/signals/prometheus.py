"""Prometheus telemetry client and unified PrometheusLokiAdapter."""

import math
from datetime import UTC, datetime
from typing import Any

import httpx

from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import get_settings
from understudy.common.errors import ObservabilityError
from understudy.common.logging import get_logger
from understudy.contracts.incident import (
    ErrorSignature,
    MetricPoint,
    MetricSeries,
    MetricWindow,
)
from understudy.signals.api import ObservabilityAdapter
from understudy.signals.loki import LokiClient

logger = get_logger(__name__)


def _extract_scalar_or_vector_value(data: dict[str, Any]) -> float | None:
    """Extract a numeric float from a Prometheus instant query response."""
    result_data = data.get("data", {})
    result_type = result_data.get("resultType")
    result = result_data.get("result")

    if not isinstance(result, list | tuple) or not result:
        return None

    if result_type == "scalar" and len(result) >= 2:
        try:
            return float(result[1])
        except (ValueError, TypeError):
            return None

    if result_type == "vector":
        first = result[0]
        if isinstance(first, dict):
            val_pair = first.get("value")
            if isinstance(val_pair, list | tuple) and len(val_pair) >= 2:
                try:
                    return float(val_pair[1])
                except (ValueError, TypeError):
                    return None

    return None


class PrometheusClient:
    """HTTP client for querying metrics and service health from Prometheus."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        settings = get_settings()
        raw_url = base_url or settings.endpoints.prometheus
        self.base_url = raw_url.rstrip("/")
        self.timeout = timeout
        self._custom_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self.clock = resolve_clock(clock)

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the active HTTP client, initializing the owned client if needed."""
        if self._custom_client is not None:
            return self._custom_client
        if self._owned_client is None or self._owned_client.is_closed:
            self._owned_client = httpx.AsyncClient(timeout=self.timeout)
        return self._owned_client

    async def close(self) -> None:
        """Close the underlying HTTP client if owned."""
        if self._owned_client is not None and not self._owned_client.is_closed:
            await self._owned_client.aclose()

    async def query(self, expr: str, time: datetime | None = None) -> dict[str, Any]:
        """Execute a PromQL instant query against /api/v1/query."""
        client = await self._get_client()
        url = f"{self.base_url}/api/v1/query"
        params: dict[str, str] = {"query": expr}
        if time is not None:
            params["time"] = str(time.timestamp())

        try:
            resp = await client.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ObservabilityError(f"Unexpected Prometheus response type: {type(data)}")
            if data.get("status") != "success":
                err_msg = data.get("error", "Unknown Prometheus query error")
                raise ObservabilityError(f"Prometheus query error: {err_msg}")
            return data
        except httpx.HTTPError as exc:
            logger.error("prometheus_query_failed", url=url, expr=expr, error=str(exc))
            raise ObservabilityError(f"Prometheus query failed: {exc}") from exc

    async def query_range(
        self,
        expr: str,
        start: datetime,
        end: datetime,
        step: str = "15s",
    ) -> dict[str, Any]:
        """Execute a PromQL range query against /api/v1/query_range."""
        client = await self._get_client()
        url = f"{self.base_url}/api/v1/query_range"
        params = {
            "query": expr,
            "start": str(start.timestamp()),
            "end": str(end.timestamp()),
            "step": step,
        }

        try:
            resp = await client.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ObservabilityError(f"Unexpected Prometheus response type: {type(data)}")
            if data.get("status") != "success":
                err_msg = data.get("error", "Unknown Prometheus query error")
                raise ObservabilityError(f"Prometheus range query error: {err_msg}")
            return data
        except httpx.HTTPError as exc:
            logger.error("prometheus_range_query_failed", url=url, expr=expr, error=str(exc))
            raise ObservabilityError(f"Prometheus range query failed: {exc}") from exc

    async def get_metric_window(
        self,
        service: str,
        since: datetime,
        namespace: str = "ust-prod",
        until: datetime | None = None,
    ) -> MetricWindow:
        """Fetch telemetry metric window for a service across a time window."""
        start_time = since
        end_time = until or self.clock.now()

        if start_time > end_time:
            start_time, end_time = end_time, start_time

        delta_seconds = max(1, int((end_time - start_time).total_seconds()))
        window_str = f"{delta_seconds}s"

        # 1. Total request count
        q_total = (
            f'sum(increase(http_requests_total{{namespace="{namespace}", service="{service}"}}'
            f"[{window_str}]))"
        )
        res_total = await self.query(q_total, time=end_time)
        val_total = _extract_scalar_or_vector_value(res_total)
        if val_total is None:
            # Fallback to app label if service label was absent in scrape
            q_total_fallback = (
                f'sum(increase(http_requests_total{{namespace="{namespace}", app="{service}"}}'
                f"[{window_str}]))"
            )
            res_total_fb = await self.query(q_total_fallback, time=end_time)
            val_total = _extract_scalar_or_vector_value(res_total_fb)

        request_count = (
            round(val_total)
            if (val_total is not None and not math.isnan(val_total) and val_total >= 0)
            else 0
        )

        # 2. Error rate (5xx status codes)
        q_err = (
            f'sum(increase(http_requests_total{{namespace="{namespace}", service="{service}", '
            f'status_code=~"5.."}}[{window_str}]))'
        )
        res_err = await self.query(q_err, time=end_time)
        val_err = _extract_scalar_or_vector_value(res_err)
        if val_err is None and request_count > 0:
            q_err_fallback = (
                f'sum(increase(http_requests_total{{namespace="{namespace}", app="{service}", '
                f'status_code=~"5.."}}[{window_str}]))'
            )
            res_err_fb = await self.query(q_err_fallback, time=end_time)
            val_err = _extract_scalar_or_vector_value(res_err_fb)

        error_count = (
            float(val_err)
            if (val_err is not None and not math.isnan(val_err) and val_err >= 0)
            else 0.0
        )
        error_rate: float | None = None
        if request_count > 0:
            raw_rate = error_count / request_count
            error_rate = min(1.0, max(0.0, raw_rate))
        else:
            error_rate = 0.0

        # 3. p99 Latency in milliseconds
        q_p99 = (
            "histogram_quantile(0.99, sum(rate("
            f'http_request_duration_seconds_bucket{{namespace="{namespace}", service="{service}"}}'
            f"[{window_str}])) by (le))"
        )
        res_p99 = await self.query(q_p99, time=end_time)
        val_p99 = _extract_scalar_or_vector_value(res_p99)
        if val_p99 is None:
            q_p99_fallback = (
                "histogram_quantile(0.99, sum(rate("
                f'http_request_duration_seconds_bucket{{namespace="{namespace}", app="{service}"}}'
                f"[{window_str}])) by (le))"
            )
            res_p99_fb = await self.query(q_p99_fallback, time=end_time)
            val_p99 = _extract_scalar_or_vector_value(res_p99_fb)

        p99_latency_ms: float | None = None
        if (
            val_p99 is not None
            and not math.isnan(val_p99)
            and not math.isinf(val_p99)
            and val_p99 >= 0
        ):
            p99_latency_ms = round(float(val_p99) * 1000.0, 2)

        # 4. Range time series for http_requests_total
        q_series = f'http_requests_total{{namespace="{namespace}", service="{service}"}}'
        step_sec = max(2, min(60, delta_seconds // 30))
        res_series = await self.query_range(
            q_series,
            start=start_time,
            end=end_time,
            step=f"{step_sec}s",
        )

        series_list: list[MetricSeries] = []
        raw_results = res_series.get("data", {}).get("result")
        if not isinstance(raw_results, list) or not raw_results:
            # Try app label fallback for series
            q_series_fallback = f'http_requests_total{{namespace="{namespace}", app="{service}"}}'
            res_series_fb = await self.query_range(
                q_series_fallback,
                start=start_time,
                end=end_time,
                step=f"{step_sec}s",
            )
            fb_data = res_series_fb.get("data", {}).get("result")
            raw_results = fb_data if isinstance(fb_data, list) else []

        for item in raw_results:
            if not isinstance(item, dict):
                continue
            labels = dict(item.get("metric", {}))
            metric_name = labels.pop("__name__", "http_requests_total")
            raw_values = item.get("values", [])
            points: list[MetricPoint] = []
            if isinstance(raw_values, list):
                for p in raw_values:
                    if isinstance(p, list | tuple) and len(p) >= 2:
                        try:
                            pt_dt = datetime.fromtimestamp(float(p[0]), tz=UTC)
                            pt_val = float(p[1])
                            points.append(MetricPoint(timestamp=pt_dt, value=pt_val))
                        except (ValueError, TypeError, OverflowError):
                            continue
            series_list.append(MetricSeries(metric_name=metric_name, labels=labels, points=points))

        return MetricWindow(
            service=service,
            start_time=start_time,
            end_time=end_time,
            series=series_list,
            p99_latency_ms=p99_latency_ms,
            error_rate=error_rate,
            request_count=request_count,
        )

    async def check_service_health(self, namespace: str, service: str) -> bool:
        """Check whether a service in a namespace is healthy via Prometheus up metric."""
        q_up = f'up{{namespace="{namespace}"}}'
        try:
            data = await self.query(q_up)
        except (ObservabilityError, httpx.HTTPError) as exc:
            logger.warning(
                "prometheus_health_check_failed",
                namespace=namespace,
                service=service,
                error=str(exc),
            )
            return False

        results = data.get("data", {}).get("result", [])
        if not isinstance(results, list):
            return False

        for item in results:
            if not isinstance(item, dict):
                continue
            metric = item.get("metric", {})
            instance = str(metric.get("instance", ""))
            pod = str(metric.get("pod", ""))
            app = str(metric.get("app", ""))
            svc = str(metric.get("service", ""))

            # Match target against the requested service name
            if svc == service or app == service or service in instance or pod.startswith(service):
                val_pair = item.get("value")
                if isinstance(val_pair, list | tuple) and len(val_pair) >= 2:
                    try:
                        if float(val_pair[1]) == 1.0:
                            return True
                    except (ValueError, TypeError):
                        pass

        return False


class PrometheusLokiAdapter(ObservabilityAdapter):
    """Unified telemetry adapter implementing ObservabilityAdapter backed by Prometheus and Loki."""

    def __init__(
        self,
        prometheus_client: PrometheusClient | None = None,
        loki_client: LokiClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.clock = resolve_clock(clock)
        self.prometheus = prometheus_client or PrometheusClient(clock=self.clock)
        self.loki = loki_client or LokiClient(clock=self.clock)

    async def close(self) -> None:
        """Close underlying clients."""
        await self.prometheus.close()
        await self.loki.close()

    async def metric_window(
        self,
        service: str,
        since: datetime,
        namespace: str = "ust-prod",
    ) -> MetricWindow:
        """Fetch telemetry metric window for a service since a given timestamp."""
        return await self.prometheus.get_metric_window(
            service=service,
            since=since,
            namespace=namespace,
            until=self.clock.now(),
        )

    async def error_signatures(
        self,
        service: str,
        since: datetime,
        namespace: str = "ust-prod",
    ) -> list[ErrorSignature]:
        """Aggregate log error signatures for a service since a given timestamp."""
        return await self.loki.error_signatures(
            service=service,
            since=since,
            namespace=namespace,
            until=self.clock.now(),
        )

    async def service_health(self, namespace: str, service: str) -> bool:
        """Check whether a service in a namespace is currently healthy."""
        return await self.prometheus.check_service_health(
            namespace=namespace,
            service=service,
        )
