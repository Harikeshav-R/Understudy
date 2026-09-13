"""PagerDuty webhook receiver, HMAC verification, and AlertSource implementation."""

import asyncio
import contextlib
import hashlib
import hmac
import json
import re
import sys
from datetime import UTC, datetime
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import get_settings
from understudy.common.ids import new_alert_id
from understudy.common.logging import get_logger
from understudy.contracts.incident import Alert
from understudy.signals.api import AlertSource
from understudy.signals.scenarios import create_synthetic_alert

KNOWN_SERVICES = ("edge-gateway", "auth-service", "data-service", "worker")


def verify_pagerduty_signature(
    body: bytes,
    signature_header: str | None,
    secret: str | None,
) -> bool:
    """Verify PagerDuty Webhook v3 HMAC-SHA256 signature in constant time.

    The header format is: v1=<hex_signature> or multiple comma-separated signatures.
    """
    if not secret or not signature_header:
        return False

    clean_secret = secret.strip().removeprefix("v1=").encode("utf-8")
    expected_hex = hmac.new(clean_secret, body, hashlib.sha256).hexdigest()

    signatures = [s.strip() for s in signature_header.split(",") if s.strip()]
    for item in signatures:
        sig = item.removeprefix("v1=").strip()
        if hmac.compare_digest(sig, expected_hex):
            return True

    return False


def _normalize_service_name(raw_service: str) -> str:
    """Map external service string to a canonical demo stack microservice."""
    cleaned = raw_service.strip().lower()
    for known in KNOWN_SERVICES:
        if re.search(rf"\b{re.escape(known)}\b", cleaned):
            return known

    sanitized = "".join(c if c.isalnum() else "-" for c in cleaned).strip("-")
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    return sanitized or "edge-gateway"


def _map_severity(
    urgency: str | None = None,
    priority: str | None = None,
    explicit: str | None = None,
) -> Literal["critical", "error", "warning"]:
    """Map incident urgency/priority/severity to Alert severity."""
    if explicit == "critical":
        return "critical"
    if explicit == "error":
        return "error"
    if explicit == "warning":
        return "warning"

    p_str = (priority or "").upper()
    if p_str in ("P1", "P2") or urgency == "high":
        return "critical"
    if p_str in ("P4", "P5") or urgency == "low":
        return "warning"
    if p_str == "P3":
        return "error"

    return "critical"


def _parse_timestamp(val: str | None, clock: Clock) -> datetime:
    """Parse ISO timestamp with timezone support, defaulting to clock now."""
    if not val:
        return clock.now()
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt
    except Exception:
        return clock.now()


def parse_pagerduty_webhook(payload: dict[str, Any], clock: Clock | None = None) -> Alert | None:
    """Parse and normalize a PagerDuty webhook payload into an Alert.

    Returns None for non-trigger lifecycle events (e.g. acknowledge, resolve).
    """
    resolved_clock = resolve_clock(clock)

    # 1. PagerDuty Webhooks v3 format
    if "event" in payload and isinstance(payload["event"], dict):
        evt = payload["event"]
        event_type = evt.get("event_type", "incident.triggered")
        if event_type not in ("incident.triggered", "incident.reopened"):
            return None

        data = evt.get("data", {})
        raw_id = str(data.get("id") or evt.get("id") or new_alert_id())
        alert_id = raw_id if raw_id.startswith("alt_") else f"alt_pd_{raw_id}"
        title = str(data.get("title") or "PagerDuty incident triggered")

        svc_field = data.get("service")
        if isinstance(svc_field, dict):
            svc_name = str(svc_field.get("summary") or svc_field.get("name") or "edge-gateway")
        else:
            svc_name = str(svc_field or "edge-gateway")
        service = _normalize_service_name(svc_name)

        p_obj = data.get("priority")
        p_summary = p_obj.get("summary") if isinstance(p_obj, dict) else str(p_obj or "")
        severity = _map_severity(
            urgency=data.get("urgency"),
            priority=p_summary,
            explicit=data.get("severity"),
        )

        fired_at = _parse_timestamp(
            evt.get("occurred_at") or data.get("created_at"), resolved_clock
        )

        return Alert(
            alert_id=alert_id,
            source="pagerduty",
            title=title,
            service=service,
            severity=severity,
            fired_at=fired_at,
            raw=payload,
        )

    # 2. PagerDuty Webhooks v2 format
    if "messages" in payload and isinstance(payload["messages"], list):
        for msg in payload["messages"]:
            if not isinstance(msg, dict):
                continue
            event = msg.get("event")
            if event not in ("incident.trigger", "incident.reopen"):
                continue
            inc = msg.get("incident", {})
            raw_id = str(inc.get("id") or new_alert_id())
            alert_id = raw_id if raw_id.startswith("alt_") else f"alt_pd_{raw_id}"
            title = str(inc.get("title") or "PagerDuty incident triggered")

            svc_field = inc.get("service")
            if isinstance(svc_field, dict):
                svc_name = str(svc_field.get("name") or "edge-gateway")
            else:
                svc_name = str(svc_field or "edge-gateway")
            service = _normalize_service_name(svc_name)

            severity = _map_severity(
                urgency=inc.get("urgency"),
                priority=str(inc.get("priority", "")),
            )
            fired_at = _parse_timestamp(msg.get("created_on"), resolved_clock)

            return Alert(
                alert_id=alert_id,
                source="pagerduty",
                title=title,
                service=service,
                severity=severity,
                fired_at=fired_at,
                raw=payload,
            )
        return None

    # 3. Direct normalized Alert payload format
    if "title" in payload and "service" in payload:
        raw_id = str(payload.get("alert_id") or new_alert_id())
        alert_id = raw_id if raw_id.startswith("alt_") else f"alt_{raw_id}"
        severity = _map_severity(explicit=payload.get("severity"))
        fired_at = _parse_timestamp(payload.get("fired_at"), resolved_clock)

        return Alert(
            alert_id=alert_id,
            source="pagerduty",
            title=str(payload["title"]),
            service=_normalize_service_name(str(payload["service"])),
            severity=severity,
            fired_at=fired_at,
            raw=payload,
        )

    return None


class PagerDutyAlertSource(AlertSource):
    """AlertSource implementation backed by PagerDuty webhook and synthetic injections."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = resolve_clock(clock)
        self._queue: asyncio.Queue[Alert] = asyncio.Queue()

    async def receive_alert(self) -> Alert:
        """Wait for and return the next incoming alert from the queue."""
        return await self._queue.get()

    async def inject_synthetic_alert(self, alert: Alert) -> None:
        """Enqueue a synthetic alert into the source queue."""
        await self.enqueue_alert(alert)

    async def enqueue_alert(self, alert: Alert) -> None:
        """Enqueue an alert and emit structured event and console output."""
        await self._queue.put(alert)
        logger = get_logger(incident_id=f"inc_{alert.alert_id}")
        logger.info(
            "alert_received",
            alert_id=alert.alert_id,
            service=alert.service,
            source=alert.source,
            severity=alert.severity,
            title=alert.title,
        )
        # Emit human-readable expectation line for checkpoint assertion
        sys.stdout.write(f"alert received alert_id={alert.alert_id} service={alert.service}\n")
        sys.stdout.flush()

    def qsize(self) -> int:
        """Return count of alerts waiting in queue."""
        return self._queue.qsize()

    def empty(self) -> bool:
        """Return True if alert queue is empty."""
        return self._queue.empty()


def create_webhook_app(
    alert_source: PagerDutyAlertSource,
    secret: str | None = None,
    clock: Clock | None = None,
    require_signature: bool = True,
) -> FastAPI:
    """Create FastAPI application listening for PagerDuty webhooks."""
    app = FastAPI(title="Understudy PagerDuty Webhook Receiver")
    resolved_clock = resolve_clock(clock)

    webhook_secret = (
        secret if secret is not None else (get_settings().secrets.pagerduty_webhook_secret or "")
    )
    if require_signature and not webhook_secret:
        raise ValueError(
            "Webhook signature verification is required but no secret was configured "
            "(pass --secret or set PAGERDUTY_WEBHOOK_SECRET)"
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def _handle_webhook(request: Request) -> Response:
        body = await request.body()
        sig_header = request.headers.get("x-pagerduty-signature")

        if require_signature and not verify_pagerduty_signature(body, sig_header, webhook_secret):
            get_logger().warning(
                "pagerduty_signature_invalid",
                has_header=sig_header is not None,
            )
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        try:
            payload: dict[str, Any] = json.loads(body.decode("utf-8")) if body else {}
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

        alert = parse_pagerduty_webhook(payload, clock=resolved_clock)
        if alert is None:
            return JSONResponse(
                status_code=200,
                content={"status": "ignored", "reason": "non_trigger_event"},
            )

        await alert_source.enqueue_alert(alert)
        return JSONResponse(
            status_code=200,
            content={"status": "accepted", "alert_id": alert.alert_id},
        )

    @app.post("/webhook")
    async def webhook_post(request: Request) -> Response:
        return await _handle_webhook(request)

    @app.post("/")
    async def root_post(request: Request) -> Response:
        return await _handle_webhook(request)

    @app.post("/api/alerts/inject")
    @app.post("/alert/inject")
    async def inject_alert(request: Request) -> Response:
        try:
            payload: dict[str, Any] = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

        if "scenario" in payload:
            scenario_id = str(payload["scenario"])
            title = payload.get("title")
            service = payload.get("service")
            raw_sev = payload.get("severity")
            severity: Literal["critical", "error", "warning"] | None = (
                raw_sev if raw_sev in ("critical", "error", "warning") else None
            )
            alert = create_synthetic_alert(
                scenario_id=scenario_id,
                clock=resolved_clock,
                title=title,
                service=service,
                severity=severity,
            )
        else:
            alert = Alert(
                alert_id=str(payload.get("alert_id") or new_alert_id()),
                source="synthetic",
                title=str(payload.get("title", "Synthetic incident alert")),
                service=_normalize_service_name(str(payload.get("service", "edge-gateway"))),
                severity=_map_severity(explicit=payload.get("severity")),
                fired_at=_parse_timestamp(payload.get("fired_at"), resolved_clock),
                raw=payload.get("raw", payload),
            )

        await alert_source.enqueue_alert(alert)
        return JSONResponse(
            status_code=200,
            content={"status": "injected", "alert_id": alert.alert_id},
        )

    return app


class WebhookReceiverServer:
    """Asyncio runner for the PagerDuty webhook FastAPI receiver application."""

    def __init__(
        self,
        alert_source: PagerDutyAlertSource,
        host: str = "127.0.0.1",
        port: int = 9108,
        secret: str | None = None,
        require_signature: bool = True,
        clock: Clock | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.alert_source = alert_source
        self.app = create_webhook_app(
            alert_source=alert_source,
            secret=secret,
            clock=clock,
            require_signature=require_signature,
        )
        self.config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(self.config)
        self._serve_task: asyncio.Task[None] | None = None

    async def _serve(self) -> None:
        # uvicorn signals a bind failure with sys.exit(), which raises SystemExit from
        # inside this task. Left uncaught, asyncio re-raises BaseException subclasses
        # like SystemExit out of the event loop's own scheduler, crashing the whole
        # loop instead of just this task — so convert it to a plain exception here,
        # before it ever reaches asyncio's task machinery.
        try:
            await self.server.serve()
        except SystemExit as exc:
            raise RuntimeError(f"Webhook server failed to start: {exc}") from exc

    async def start(self) -> None:
        """Start the ASGI server in background asyncio task."""
        self._serve_task = asyncio.create_task(self._serve())
        for _ in range(50):
            if self.server.started:
                return
            if self._serve_task.done():
                self._serve_task.result()
                raise RuntimeError("Webhook server exited before starting")
            await asyncio.sleep(0.05)
        raise TimeoutError("Webhook server did not start within the expected time")

    async def stop(self) -> None:
        """Signal the ASGI server to stop and await termination."""
        self.server.should_exit = True
        if self._serve_task:
            with contextlib.suppress(BaseException):
                await self._serve_task
            self._serve_task = None

    async def __aenter__(self) -> "WebhookReceiverServer":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()
