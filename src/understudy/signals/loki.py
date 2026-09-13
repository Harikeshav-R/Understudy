"""Loki log query client, log normalization, fingerprinting, and ErrorSignature aggregation."""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import get_settings
from understudy.common.errors import ObservabilityError
from understudy.common.logging import get_logger
from understudy.contracts.incident import ErrorSignature

logger = get_logger(__name__)

# Precompiled regex patterns for message normalization
RE_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
RE_ADDR = re.compile(r"\b0x[0-9a-fA-F]+\b")
RE_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
RE_IP_PORT = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b")
RE_LINE_NUM = re.compile(r"(line )\d+")
RE_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
RE_WHITESPACE = re.compile(r"\s+")
RE_PLAINTEXT_ERROR = re.compile(
    r"\b(ERROR|CRITICAL|FATAL|Exception|Traceback|\w+Error)\b",
    re.IGNORECASE,
)


ERROR_LEVELS = frozenset({"error", "err", "critical", "fatal", "exception"})


def normalize_message(message: str) -> str:
    """Normalize dynamic values in log messages so identical errors produce identical hashes.

    Replaces:
    - UUIDs / GUIDs with <UUID>
    - Memory addresses with <ADDR>
    - ISO timestamps with <TIMESTAMP>
    - IPv4 addresses & ports with <IP>
    - Line numbers with <NUM>
    - Generic numbers and durations with <NUM>
    - Collapses consecutive whitespace characters.
    """
    if not message:
        return ""
    normalized = RE_UUID.sub("<UUID>", message)
    normalized = RE_ADDR.sub("<ADDR>", normalized)
    normalized = RE_TIMESTAMP.sub("<TIMESTAMP>", normalized)
    normalized = RE_IP_PORT.sub("<IP>", normalized)
    normalized = RE_LINE_NUM.sub(r"\1<NUM>", normalized)
    normalized = RE_NUM.sub("<NUM>", normalized)
    normalized = RE_WHITESPACE.sub(" ", normalized).strip()
    return normalized


def compute_fingerprint(normalized_message: str) -> str:
    """Compute deterministic, truncated SHA-256 fingerprint from a normalized message."""
    hasher = hashlib.sha256(normalized_message.encode("utf-8"))
    return hasher.hexdigest()[:16]


def parse_log_entry(
    line: str,
    ts_nano: str | int,
) -> tuple[bool, str, datetime]:
    """Parse a single raw Loki log line and extract (is_error, message, timestamp)."""
    # Parse timestamp from nanoseconds
    try:
        ts_sec = float(ts_nano) / 1e9
        dt = datetime.fromtimestamp(ts_sec, tz=UTC)
    except (ValueError, TypeError, OverflowError):
        dt = datetime.now(tz=UTC)

    cleaned = line.strip()
    if not cleaned:
        return False, "", dt

    # Attempt JSON parsing (structlog format)
    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            payload = json.loads(cleaned)
            raw_level = str(
                payload.get("level") or payload.get("severity") or payload.get("log_level") or ""
            ).lower()
            status_code = payload.get("status_code")
            has_exc = bool(payload.get("exc_info") or payload.get("exception"))
            is_5xx = isinstance(status_code, int) and status_code >= 500

            is_error = raw_level in ERROR_LEVELS or has_exc or is_5xx
            if is_error:
                msg = (
                    payload.get("event")
                    or payload.get("message")
                    or payload.get("msg")
                    or payload.get("error")
                    or payload.get("detail")
                )
                if msg is not None:
                    return True, str(msg), dt
                return True, cleaned, dt
            return False, "", dt
        except (json.JSONDecodeError, AttributeError):
            pass

    # Fallback: plaintext error detection
    if RE_PLAINTEXT_ERROR.search(cleaned):
        return True, cleaned, dt

    return False, "", dt


class LokiClient:
    """HTTP client for querying log streams from Loki."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        settings = get_settings()
        raw_url = base_url or settings.endpoints.loki
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

    async def query_range(
        self,
        query: str,
        start: datetime,
        end: datetime,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Execute a LogQL query against /loki/api/v1/query_range."""
        client = await self._get_client()
        start_ns = str(int(start.timestamp() * 1e9))
        end_ns = str(int(end.timestamp() * 1e9))

        url = f"{self.base_url}/loki/api/v1/query_range"
        params = {
            "query": query,
            "start": start_ns,
            "end": end_ns,
            "limit": str(limit),
            "direction": "FORWARD",
        }

        try:
            resp = await client.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ObservabilityError(f"Unexpected Loki response type: {type(data)}")
            return data
        except httpx.HTTPError as exc:
            logger.error("loki_query_failed", url=url, query=query, error=str(exc))
            raise ObservabilityError(f"Loki query failed: {exc}") from exc

    async def error_signatures(
        self,
        service: str,
        since: datetime,
        namespace: str = "ust-prod",
        until: datetime | None = None,
    ) -> list[ErrorSignature]:
        """Fetch error logs from Loki, normalize messages, and aggregate into ErrorSignatures."""
        end_time = until or self.clock.now()
        start_time = since

        # Ensure start_time <= end_time
        if start_time > end_time:
            start_time, end_time = end_time, start_time

        query = f'{{namespace="{namespace}", app="{service}"}}'
        data = await self.query_range(query=query, start=start_time, end=end_time)

        results = data.get("data", {}).get("result", [])
        if not isinstance(results, list):
            return []

        # Map fingerprint -> aggregation state
        groups: dict[str, dict[str, Any]] = {}

        for stream in results:
            if not isinstance(stream, dict):
                continue
            values = stream.get("values", [])
            if not isinstance(values, list):
                continue

            for item in values:
                if not isinstance(item, list | tuple) or len(item) < 2:
                    continue
                ts_nano, raw_line = item[0], item[1]
                is_err, raw_msg, dt = parse_log_entry(str(raw_line), ts_nano)
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

        # Sort primarily by occurrence count descending, secondarily by last_seen descending
        signatures.sort(key=lambda s: (s.count, s.last_seen), reverse=True)
        return signatures
