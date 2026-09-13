"""Datadog telemetry adapter and client conforming to ObservabilityAdapter Protocol.

Implements build-plan step B2.5 and ADR-025:
- All observability logic reads through ObservabilityAdapter Protocol.
- DatadogAdapter queries Datadog metrics (/api/v1/query), logs (/api/v2/logs/events/search),
  and monitor/health checks.
- Zero budget: uses httpx.AsyncClient with explicit timeouts, no proprietary SDKs.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import get_settings
from understudy.common.errors import DatadogError
from understudy.common.logging import get_logger
from understudy.contracts.incident import (
    ErrorSignature,
    MetricPoint,
    MetricSeries,
    MetricWindow,
)
from understudy.signals.api import ObservabilityAdapter
from understudy.signals.loki import (
    ERROR_LEVELS,
    compute_fingerprint,
    normalize_message,
)

logger = get_logger(__name__)


def _parse_datadog_timestamp(raw_ts: float | int) -> datetime:
    """Convert Datadog pointlist timestamp (ms or sec) to timezone-aware UTC datetime."""
    ts_float = float(raw_ts)
    # Datadog API v1 query pointlist timestamps are in milliseconds (> 1e11)
    ts_sec = ts_float / 1000.0 if ts_float > 1e11 else ts_float
    return datetime.fromtimestamp(ts_sec, tz=UTC)


def _parse_log_timestamp(raw_val: Any) -> datetime:
    """Parse ISO or numeric timestamp from Datadog log event into UTC datetime."""
    if isinstance(raw_val, str):
        try:
            cleaned = raw_val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except Exception:
            pass
    elif isinstance(raw_val, int | float):
        try:
            return _parse_datadog_timestamp(raw_val)
        except Exception:
            pass
    return datetime.now(tz=UTC)


def _extract_log_message_and_error(event: dict[str, Any]) -> tuple[bool, str, datetime]:
    """Extract (is_error, normalized_message, timestamp) from a Datadog log event."""
    attrs = event.get("attributes", {})
    if not isinstance(attrs, dict):
        attrs = {}

    status = str(attrs.get("status") or "").lower()
    dt = _parse_log_timestamp(attrs.get("timestamp"))

    raw_msg: str = ""
    # Check standard message attribute
    if "message" in attrs and isinstance(attrs["message"], str):
        raw_msg = attrs["message"].strip()
    elif "event" in attrs and isinstance(attrs["event"], str):
        raw_msg = attrs["event"].strip()

    # Check nested attributes (e.g. structured logs)
    nested_attrs = attrs.get("attributes")
    if isinstance(nested_attrs, dict):
        if not raw_msg and "message" in nested_attrs and isinstance(nested_attrs["message"], str):
            raw_msg = nested_attrs["message"].strip()
        if not raw_msg and "event" in nested_attrs and isinstance(nested_attrs["event"], str):
            raw_msg = nested_attrs["event"].strip()
        if not status and "status" in nested_attrs:
            status = str(nested_attrs["status"]).lower()

    if not raw_msg:
        return False, "", dt

    # Check if raw_msg is a JSON string (e.g. structlog format forwarded to Datadog)
    if raw_msg.startswith("{") and raw_msg.endswith("}"):
        try:
            parsed = json.loads(raw_msg)
            level = str(
                parsed.get("level") or parsed.get("severity") or parsed.get("log_level") or ""
            ).lower()
            status_code = parsed.get("status_code")
            has_exc = bool(parsed.get("exc_info") or parsed.get("exception"))
            is_5xx = isinstance(status_code, int) and status_code >= 500
            if level in ERROR_LEVELS or has_exc or is_5xx:
                status = "error"
            msg_field = parsed.get("event") or parsed.get("message")
            if msg_field and isinstance(msg_field, str):
                raw_msg = msg_field.strip()
        except Exception:
            pass

    is_error = status in ERROR_LEVELS or status in ("error", "err", "critical", "fatal", "warn")
    return is_error, raw_msg, dt


class DatadogClient:
    """HTTP client for querying Datadog Metrics, Logs, and Monitors APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        app_key: str | None = None,
        base_url: str | None = None,
        site: str | None = None,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.secrets.datadog_api_key
        self.app_key = app_key if app_key is not None else settings.secrets.datadog_app_key
        self.timeout = timeout
        self.clock = resolve_clock(clock)

        resolved_site = site or "datadoghq.com"
        if base_url is not None:
            self.base_url = base_url.rstrip("/")
        else:
            self.base_url = f"https://api.{resolved_site}".rstrip("/")

        self._custom_client = client
        self._owned_client: httpx.AsyncClient | None = None

    def _get_headers(self) -> dict[str, str]:
        """Construct required Datadog authentication and content headers."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["DD-API-KEY"] = self.api_key
        if self.app_key:
            headers["DD-APPLICATION-KEY"] = self.app_key
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the active HTTP client, lazily initializing if owned."""
        if self._custom_client is not None:
            return self._custom_client
        if self._owned_client is None or self._owned_client.is_closed:
            self._owned_client = httpx.AsyncClient(timeout=self.timeout)
        return self._owned_client

    async def close(self) -> None:
        """Close the underlying HTTP client if created internally."""
        if self._owned_client is not None and not self._owned_client.is_closed:
            await self._owned_client.aclose()

    async def query_metrics(
        self,
        query: str,
        start_time: datetime,
        end_time: datetime,
    ) -> dict[str, Any]:
        """Execute a metric query against Datadog /api/v1/query endpoint."""
        client = await self._get_client()
        url = f"{self.base_url}/api/v1/query"
        params = {
            "from": str(int(start_time.timestamp())),
            "to": str(int(end_time.timestamp())),
            "query": query,
        }

        try:
            resp = await client.get(
                url,
                params=params,
                headers=self._get_headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise DatadogError(f"Unexpected Datadog response type: {type(data)}")
            if data.get("status") != "ok":
                err_msg = data.get("error", "Unknown Datadog query error")
                raise DatadogError(f"Datadog query error: {err_msg}")
            return data
        except httpx.HTTPError as exc:
            logger.error("datadog_query_failed", url=url, query=query, error=str(exc))
            raise DatadogError(f"Datadog query failed: {exc}") from exc

    async def search_logs(
        self,
        query: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search log events via Datadog Logs API v2 (/api/v2/logs/events/search)."""
        client = await self._get_client()
        url = f"{self.base_url}/api/v2/logs/events/search"
        payload = {
            "filter": {
                "from": start_time.isoformat(),
                "to": end_time.isoformat(),
                "query": query,
            },
            "page": {
                "limit": limit,
            },
            "sort": "-timestamp",
        }

        try:
            resp = await client.post(
                url,
                json=payload,
                headers=self._get_headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise DatadogError(f"Unexpected Datadog logs response type: {type(data)}")
            events = data.get("data", [])
            if not isinstance(events, list):
                return []
            return [e for e in events if isinstance(e, dict)]
        except httpx.HTTPError as exc:
            logger.error("datadog_search_logs_failed", url=url, query=query, error=str(exc))
            raise DatadogError(f"Datadog search logs failed: {exc}") from exc

    async def check_monitors(self, query: str) -> list[dict[str, Any]]:
        """Search monitor statuses via Datadog Monitors API (/api/v1/monitor/search)."""
        client = await self._get_client()
        url = f"{self.base_url}/api/v1/monitor/search"
        params = {"query": query}

        try:
            resp = await client.get(
                url,
                params=params,
                headers=self._get_headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                return []
            monitors = data.get("monitors", [])
            if not isinstance(monitors, list):
                return []
            return [m for m in monitors if isinstance(m, dict)]
        except httpx.HTTPError as exc:
            logger.warning("datadog_check_monitors_failed", url=url, query=query, error=str(exc))
            return []


class DatadogAdapter(ObservabilityAdapter):
    """Unified telemetry adapter implementing ObservabilityAdapter backed by Datadog."""

    def __init__(
        self,
        datadog_client: DatadogClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.clock = resolve_clock(clock)
        self.client = datadog_client or DatadogClient(clock=self.clock)

    async def close(self) -> None:
        """Close the underlying client."""
        await self.client.close()

    async def metric_window(
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

        # 1. Query total requests
        q_requests = (
            f"sum:http_requests_total{{service:{service},namespace:{namespace}}}.as_count()"
        )
        res_requests = await self.client.query_metrics(q_requests, start_time, end_time)
        series_data = res_requests.get("series", [])
        if not isinstance(series_data, list) or not series_data:
            # Fallback to app tag
            q_requests_fb = (
                f"sum:http_requests_total{{app:{service},namespace:{namespace}}}.as_count()"
            )
            res_requests = await self.client.query_metrics(q_requests_fb, start_time, end_time)
            series_data = res_requests.get("series", [])
            if not isinstance(series_data, list):
                series_data = []

        total_requests = 0.0
        series_list: list[MetricSeries] = []

        for item in series_data:
            if not isinstance(item, dict):
                continue
            metric_name = str(item.get("metric") or "http_requests_total")
            tag_set = item.get("tag_set", [])
            labels: dict[str, str] = {}
            if isinstance(tag_set, list):
                for tag in tag_set:
                    if isinstance(tag, str) and ":" in tag:
                        k, v = tag.split(":", 1)
                        labels[k] = v

            raw_points = item.get("pointlist", [])
            points: list[MetricPoint] = []
            if isinstance(raw_points, list):
                for pt in raw_points:
                    if isinstance(pt, list | tuple) and len(pt) >= 2:
                        raw_ts, raw_val = pt[0], pt[1]
                        if raw_val is not None:
                            try:
                                pt_dt = _parse_datadog_timestamp(raw_ts)
                                pt_val = float(raw_val)
                                points.append(MetricPoint(timestamp=pt_dt, value=pt_val))
                                total_requests += pt_val
                            except (ValueError, TypeError, OverflowError):
                                continue
            series_list.append(MetricSeries(metric_name=metric_name, labels=labels, points=points))

        request_count = round(total_requests) if total_requests >= 0 else 0

        # 2. Query 5xx errors
        q_err = (
            f"sum:http_requests_total{{service:{service},namespace:{namespace},"
            f"status_code:5*}}.as_count()"
        )
        res_err = await self.client.query_metrics(q_err, start_time, end_time)
        err_series = res_err.get("series", [])
        if not isinstance(err_series, list) or not err_series:
            q_err_fb = (
                f"sum:http_requests_total{{app:{service},namespace:{namespace},"
                f"status_code:5*}}.as_count()"
            )
            res_err = await self.client.query_metrics(q_err_fb, start_time, end_time)
            err_series = res_err.get("series", [])
            if not isinstance(err_series, list):
                err_series = []

        total_errors = 0.0
        for item in err_series:
            if not isinstance(item, dict):
                continue
            raw_points = item.get("pointlist", [])
            if isinstance(raw_points, list):
                for pt in raw_points:
                    if isinstance(pt, list | tuple) and len(pt) >= 2:
                        raw_val = pt[1]
                        if raw_val is not None:
                            with contextlib.suppress(ValueError, TypeError):
                                total_errors += float(raw_val)

        error_rate: float | None = (
            min(1.0, max(0.0, total_errors / request_count)) if request_count > 0 else 0.0
        )

        # 3. Query p99 latency
        q_p99 = f"p99:http_request_duration_seconds{{service:{service},namespace:{namespace}}}"
        res_p99 = await self.client.query_metrics(q_p99, start_time, end_time)
        p99_series = res_p99.get("series", [])
        if not isinstance(p99_series, list) or not p99_series:
            q_p99_fb = f"p99:http_request_duration_seconds{{app:{service},namespace:{namespace}}}"
            res_p99 = await self.client.query_metrics(q_p99_fb, start_time, end_time)
            p99_series = res_p99.get("series", [])
            if not isinstance(p99_series, list):
                p99_series = []

        p99_latency_ms: float | None = None
        for item in p99_series:
            if not isinstance(item, dict):
                continue
            raw_points = item.get("pointlist", [])
            if isinstance(raw_points, list):
                for pt in raw_points:
                    if isinstance(pt, list | tuple) and len(pt) >= 2:
                        raw_val = pt[1]
                        if raw_val is not None:
                            try:
                                val_float = float(raw_val)
                                # Datadog duration metrics in seconds are scaled to milliseconds
                                ms_val = val_float * 1000.0 if val_float < 100.0 else val_float
                                p99_latency_ms = round(ms_val, 2)
                            except (ValueError, TypeError):
                                pass

        return MetricWindow(
            service=service,
            start_time=start_time,
            end_time=end_time,
            series=series_list,
            p99_latency_ms=p99_latency_ms,
            error_rate=error_rate,
            request_count=request_count,
        )

    async def error_signatures(
        self,
        service: str,
        since: datetime,
        namespace: str = "ust-prod",
        until: datetime | None = None,
    ) -> list[ErrorSignature]:
        """Fetch error logs from Datadog, normalize messages, and aggregate into ErrorSignatures."""
        end_time = until or self.clock.now()
        start_time = since

        if start_time > end_time:
            start_time, end_time = end_time, start_time

        query = (
            f"service:{service} (namespace:{namespace} OR kube_namespace:{namespace}) "
            f"status:(error OR critical OR fatal)"
        )
        events = await self.client.search_logs(
            query=query, start_time=start_time, end_time=end_time
        )

        groups: dict[str, dict[str, Any]] = {}
        for event in events:
            is_err, raw_msg, dt = _extract_log_message_and_error(event)
            if not is_err or not raw_msg:
                continue

            normalized = normalize_message(raw_msg)
            fp = compute_fingerprint(normalized)

            if fp not in groups:
                groups[fp] = {
                    "fingerprint": fp,
                    "message": normalized,
                    "service": service,
                    "count": 1,
                    "first_seen": dt,
                    "last_seen": dt,
                }
            else:
                g = groups[fp]
                g["count"] += 1
                if dt < g["first_seen"]:
                    g["first_seen"] = dt
                if dt > g["last_seen"]:
                    g["last_seen"] = dt

        signatures = [
            ErrorSignature(
                fingerprint=g["fingerprint"],
                message=g["message"],
                service=g["service"],
                count=g["count"],
                first_seen=g["first_seen"],
                last_seen=g["last_seen"],
            )
            for g in groups.values()
        ]

        signatures.sort(key=lambda s: (s.count, s.last_seen), reverse=True)
        return signatures

    async def service_health(self, namespace: str, service: str) -> bool:
        """Check whether a service in a namespace is currently healthy."""
        query = f"service:{service}"
        monitors = await self.client.check_monitors(query=query)

        if monitors:
            has_alert = False
            for m in monitors:
                status = str(m.get("status") or "").lower()
                if status in ("alert", "critical"):
                    has_alert = True
                    break
            return not has_alert

        # Fallback: query service up metric
        now = self.clock.now()
        start = datetime.fromtimestamp(now.timestamp() - 60, tz=UTC)
        q_up = f"avg:up{{service:{service},namespace:{namespace}}}"
        try:
            res = await self.client.query_metrics(q_up, start_time=start, end_time=now)
            series = res.get("series", [])
            if isinstance(series, list) and series:
                for item in series:
                    if isinstance(item, dict):
                        for pt in item.get("pointlist", []):
                            if isinstance(pt, list | tuple) and len(pt) >= 2 and pt[1] is not None:
                                try:
                                    if float(pt[1]) >= 1.0:
                                        return True
                                except (ValueError, TypeError):
                                    pass
        except Exception as exc:
            logger.warning("datadog_health_metric_failed", service=service, error=str(exc))
            return False

        return False
